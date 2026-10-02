"""
Интерактивное меню бота в Telegram: инлайн-кнопки, экраны, подтверждения.

Работает в отдельном потоке на long polling. Все действия, которые трогают
биржу, берут общий замок с главным циклом, чтобы не пересечься с reconcile.
Команды принимаются только от TELEGRAM_CHAT_ID из конфигурации.
"""
import logging
import threading
import time
from decimal import Decimal

import config
import notifier

log = logging.getLogger("ui")

HELP = (
    "<b>Что умеет бот</b>\n\n"
    "📊 Статистика — сделки и PnL за сутки и за 30 дней\n"
    "📂 Позиции — что открыто сейчас, с плавающим PnL\n"
    "⏸ Пауза — перестать открывать новое (открытое продолжает вестись)\n"
    "❌ Закрыть — принудительно закрыть позицию по рынку\n"
    "⚙️ Параметры — текущие настройки бота\n\n"
    "Команды: /menu /stats /positions /help"
)


def _fmt_money(x):
    return f"{x:+.2f}"


def _bar(steps):
    return "▰" * steps + "▱" * (4 - steps)


class TelegramUI:
    """
    Не наследуется от threading.Thread намеренно: у Thread есть служебные
    атрибуты вроде _handle, и методы с такими же именами молча их перекрывают.
    Поток держим внутри.
    """

    def __init__(self, bot):
        self.bot = bot          # Bot из main.py: .client .db .trader .lock .paused .halted
        self.offset = None
        self.running = True
        self._thread = None

    # ------------------------------------------------------------------
    # запуск
    # ------------------------------------------------------------------

    def start(self):
        if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
            log.info("Telegram не настроен — интерактив выключен")
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="telegram-ui")
        self._thread.start()

    def _run(self):
        self._register_commands()
        log.info("Интерактив Telegram запущен")
        while self.running:
            try:
                self._poll()
            except Exception as e:
                log.warning("Цикл интерактива: %s", e)
                time.sleep(5)

    def _register_commands(self):
        notifier.api("setMyCommands", {"commands": [
            {"command": "menu", "description": "Главное меню"},
            {"command": "stats", "description": "Статистика"},
            {"command": "positions", "description": "Открытые позиции"},
            {"command": "help", "description": "Справка"},
        ]})

    def _poll(self):
        updates = notifier.api(
            "getUpdates",
            {"offset": self.offset, "timeout": 25,
             "allowed_updates": ["message", "callback_query"]},
            timeout=35,
        )
        if not updates:
            return
        for u in updates:
            self.offset = u["update_id"] + 1
            try:
                self._dispatch(u)
            except Exception as e:
                log.exception("Обработка апдейта: %s", e)

    # ------------------------------------------------------------------
    # маршрутизация
    # ------------------------------------------------------------------

    def _authorized(self, chat_id):
        return str(chat_id) == str(config.TELEGRAM_CHAT_ID)

    def _dispatch(self, u):
        if "callback_query" in u:
            cq = u["callback_query"]
            chat_id = cq["message"]["chat"]["id"]
            if not self._authorized(chat_id):
                return
            notifier.api("answerCallbackQuery", {"callback_query_id": cq["id"]})
            self._route(cq["data"], cq["message"]["message_id"])
            return

        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip().lower()
        if not self._authorized(msg.get("chat", {}).get("id")):
            return
        route = {
            "/start": "menu", "/menu": "menu",
            "/stats": "stats", "/positions": "pos", "/help": "help",
        }.get(text.split("@")[0])
        if route:
            self._route(route, None)

    def _route(self, data, message_id):
        if data == "menu":
            text, kb = self._screen_menu()
        elif data == "stats":
            text, kb = self._screen_stats()
        elif data == "pos":
            text, kb = self._screen_positions()
        elif data == "cfg":
            text, kb = self._screen_config()
        elif data == "help":
            text, kb = HELP, [[{"text": "◀️ Назад", "callback_data": "menu"}]]
        elif data in ("pause", "resume"):
            self.bot.paused = (data == "pause")
            text, kb = self._screen_menu()
        elif data.startswith("ask:"):
            text, kb = self._screen_confirm(data.split(":", 1)[1])
        elif data.startswith("kill:"):
            text, kb = self._do_close(data.split(":", 1)[1])
        else:
            text, kb = self._screen_menu()
        self._render(text, kb, message_id)

    def _render(self, text, keyboard, message_id):
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": keyboard},
        }
        if message_id:
            payload["message_id"] = message_id
            if notifier.api("editMessageText", payload) is not None:
                return
            payload.pop("message_id")      # нечего менять — шлём новое
        notifier.api("sendMessage", payload)

    # ------------------------------------------------------------------
    # экраны
    # ------------------------------------------------------------------

    def _screen_menu(self):
        try:
            equity = self.bot.client.equity()
            positions = self.bot.client.positions()
        except Exception as e:
            return f"⚠️ Биржа не отвечает: {e}", [[{"text": "🔄 Ещё раз", "callback_data": "menu"}]]

        upnl = sum(float(p.get("unrealisedPnl") or 0) for p in positions)
        day = self.bot.db.stats(int(time.time()) - 86_400)
        busy = len(positions)
        free = max(0, config.MAX_POSITIONS - busy)

        if self.bot.halted:
            state = "🛑 остановлен по порогу эквити"
        elif self.bot.paused:
            state = "⏸ на паузе"
        else:
            state = "🟢 торгует"

        text = (
            f"<b>DCA-скальпер</b>  ·  {state}\n"
            f"{'─'*22}\n"
            f"💰 Эквити: <b>{equity:.2f}</b> USDT\n"
            f"📉 Плавающий PnL: <b>{_fmt_money(upnl)}</b> USDT\n"
            f"📂 Слоты: <b>{busy}/{config.MAX_POSITIONS}</b> занято, {free} свободно\n"
            f"📊 За сутки: {day['n']} сделок, <b>{_fmt_money(day['pnl'])}</b> USDT\n"
        )
        pause_btn = ({"text": "▶️ Продолжить", "callback_data": "resume"}
                     if self.bot.paused else
                     {"text": "⏸ Пауза", "callback_data": "pause"})
        kb = [
            [{"text": "📊 Статистика", "callback_data": "stats"},
             {"text": "📂 Позиции", "callback_data": "pos"}],
            [pause_btn,
             {"text": "⚙️ Параметры", "callback_data": "cfg"}],
            [{"text": "🔄 Обновить", "callback_data": "menu"},
             {"text": "❔ Справка", "callback_data": "help"}],
        ]
        return text, kb

    def _screen_stats(self):
        day = self.bot.db.stats(int(time.time()) - 86_400)
        week = self.bot.db.stats(int(time.time()) - 7 * 86_400)
        month = self.bot.db.stats(int(time.time()) - 30 * 86_400)

        def block(label, s):
            n = s["n"] or 0
            wins = s["wins"] or 0
            wr = (wins / n * 100) if n else 0
            avg = (s["pnl"] / n) if n else 0
            return (f"<b>{label}</b>\n"
                    f"  сделок {n} · винрейт {wr:.0f}%\n"
                    f"  PnL <b>{_fmt_money(s['pnl'])}</b> USDT · средняя {_fmt_money(avg)}\n"
                    f"  усреднений в среднем {s['avg_steps']:.1f}  {_bar(round(s['avg_steps']))}")

        text = ("📊 <b>Статистика</b>\n" + "─" * 22 + "\n\n"
                + block("Сутки", day) + "\n\n"
                + block("7 дней", week) + "\n\n"
                + block("30 дней", month)
                + "\n\n<i>Винрейт у схемы без стопов всегда высокий — "
                  "смотри на плавающий PnL в «Позициях».</i>")
        kb = [[{"text": "📂 Позиции", "callback_data": "pos"},
               {"text": "🔄 Обновить", "callback_data": "stats"}],
              [{"text": "◀️ Меню", "callback_data": "menu"}]]
        return text, kb

    def _screen_positions(self):
        try:
            positions = self.bot.client.positions()
        except Exception as e:
            return f"⚠️ Биржа не отвечает: {e}", [[{"text": "◀️ Меню", "callback_data": "menu"}]]

        if not positions:
            return ("📂 <b>Позиции</b>\n" + "─" * 22 + "\n\nОткрытых позиций нет.",
                    [[{"text": "🔄 Обновить", "callback_data": "pos"},
                      {"text": "◀️ Меню", "callback_data": "menu"}]])

        tracked = {t["symbol"]: t for t in self.bot.db.all_trades()}
        lines = []
        kb = []
        total = 0.0
        for p in positions:
            sym = p["symbol"]
            side = "🟢 LONG" if p["side"] == "Buy" else "🔴 SHORT"
            upnl = float(p.get("unrealisedPnl") or 0)
            total += upnl
            size = float(p["size"])
            avg = float(p["avgPrice"])
            notional = size * avg
            t = tracked.get(sym)
            steps = t["steps_filled"] if t else 0
            age = int((time.time() - t["opened_at"]) / 60) if t else 0
            age_s = f"{age} мин" if age < 90 else f"{age/60:.1f} ч"
            lines.append(
                f"{side} <b>{sym}</b>\n"
                f"   средняя {avg:g} · ${notional:.0f}\n"
                f"   доборы {steps}/4 {_bar(steps)} · {age_s}\n"
                f"   PnL <b>{_fmt_money(upnl)}</b> USDT"
            )
            kb.append([{"text": f"❌ Закрыть {sym}", "callback_data": f"ask:{sym}"}])

        text = ("📂 <b>Позиции</b>\n" + "─" * 22 + "\n\n"
                + "\n\n".join(lines)
                + f"\n\n{'─'*22}\nИтого плавающий: <b>{_fmt_money(total)}</b> USDT")
        kb.append([{"text": "🔄 Обновить", "callback_data": "pos"},
                   {"text": "◀️ Меню", "callback_data": "menu"}])
        return text, kb

    def _screen_config(self):
        g = "\n".join(f"   {i+1}. −{off}% → ${n:.0f}"
                      for i, (off, n) in enumerate(config.GRID_STEPS))
        text = (
            "⚙️ <b>Параметры</b>\n" + "─" * 22 + "\n"
            f"Режим: {'ДЕМО' if config.DEMO else '<b>РЕАЛ</b>'}\n"
            f"База: ${config.BASE_NOTIONAL:.0f} · плечо {config.LEVERAGE}x · кросс\n"
            f"Слотов: {config.MAX_POSITIONS}\n"
            f"Тейк: {config.TAKE_PROFIT_PCT}% от средней\n"
            f"Стоп-лосс: нет\n"
            f"Порог остановки: {config.EQUITY_FLOOR:.0f} USDT\n\n"
            f"<b>Сетка доборов</b>\n{g}\n\n"
            f"<b>Сигнал</b>\n"
            f"   EMA({config.EMA_PERIOD}) + RSI({config.RSI_PERIOD}) "
            f"{config.RSI_OVERSOLD:.0f}/{config.RSI_OVERBOUGHT:.0f}\n"
            f"   ATR ≥ {config.MIN_ATR_PCT}% · ТФ {config.TIMEFRAME}м\n"
            f"   оборот ≥ ${config.MIN_TURNOVER_24H/1e6:.0f}M · "
            f"листинг ≥ {config.MIN_LISTING_DAYS} дн\n"
            f"   кулдаун {config.COOLDOWN_MINUTES} мин\n\n"
            "<i>Меняется переменными окружения в Railway, не отсюда.</i>"
        )
        return text, [[{"text": "◀️ Меню", "callback_data": "menu"}]]

    def _screen_confirm(self, symbol):
        text = (f"❌ <b>Закрыть {symbol} по рынку?</b>\n\n"
                f"Позиция закроется целиком, остаток сетки снимется. "
                f"Если она в минусе, убыток зафиксируется.\n\n"
                f"Слот освободится сразу.")
        kb = [[{"text": "✅ Да, закрыть", "callback_data": f"kill:{symbol}"}],
              [{"text": "◀️ Отмена", "callback_data": "pos"}]]
        return text, kb

    # ------------------------------------------------------------------
    # действие
    # ------------------------------------------------------------------

    def _do_close(self, symbol):
        back = [[{"text": "📂 Позиции", "callback_data": "pos"},
                 {"text": "◀️ Меню", "callback_data": "menu"}]]
        try:
            with self.bot.lock:
                pos = next((p for p in self.bot.client.positions()
                            if p["symbol"] == symbol), None)
                if not pos:
                    return f"ℹ️ {symbol}: позиции уже нет.", back
                self.bot.client.cancel_all(symbol)
                close_side = "Sell" if pos["side"] == "Buy" else "Buy"
                self.bot.client.market_order(
                    symbol, close_side, Decimal(pos["size"]), reduce_only=True
                )
            log.info("%s закрыт вручную из Telegram", symbol)
            return (f"✅ <b>{symbol}</b> закрыт по рынку.\n"
                    f"Итог придёт отдельным сообщением, когда сделка "
                    f"появится в истории биржи."), back
        except Exception as e:
            log.exception("Ручное закрытие %s: %s", symbol, e)
            return f"⚠️ Не вышло закрыть {symbol}: {e}", back
