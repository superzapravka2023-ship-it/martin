"""Уведомления в Telegram. Молча пропускает отправку, если токен не задан."""
import logging

import requests

import config

log = logging.getLogger("tg")

API = "https://api.telegram.org/bot{token}/{method}"


def api(method, payload=None, timeout=30):
    """Низкоуровневый вызов Bot API. Возвращает result или None."""
    if not config.TELEGRAM_TOKEN:
        return None
    try:
        r = requests.post(
            API.format(token=config.TELEGRAM_TOKEN, method=method),
            json=payload or {},
            timeout=timeout,
        )
        data = r.json()
        if not data.get("ok"):
            log.warning("Telegram %s: %s", method, data.get("description"))
            return None
        return data.get("result")
    except Exception as e:
        log.warning("Telegram %s: %s", method, e)
        return None


def send(text, keyboard=None):
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.info("[TG не настроен] %s", text)
        return
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    api("sendMessage", payload)


MENU_BTN = [[{"text": "📱 Меню", "callback_data": "menu"}]]


def entry(symbol, side, price, qty, notional, grid_prices):
    arrow = "🟢 LONG" if side == "Buy" else "🔴 SHORT"
    levels = "\n".join(
        f"   {i+1}. {p}  (${n:.0f})" for i, (p, n) in enumerate(grid_prices)
    )
    send(
        f"{arrow} <b>{symbol}</b>\n"
        f"Вход: {price}\n"
        f"Объём: {qty} (${notional:.0f})\n"
        f"Сетка доборов:\n{levels}",
        keyboard=[[{"text": "📂 Позиции", "callback_data": "pos"},
                   {"text": "📱 Меню", "callback_data": "menu"}]],
    )


def averaging(symbol, side, step, avg_price, size, notional, new_tp):
    bar = "▰" * step + "▱" * (4 - step)
    send(
        f"➕ <b>{symbol}</b> усреднение #{step}/4  {bar}\n"
        f"Новая средняя: {avg_price}\n"
        f"Позиция: {size} (${notional:.0f})\n"
        f"Тейк переставлен: {new_tp}",
        keyboard=[[{"text": "📂 Позиции", "callback_data": "pos"}]],
    )


def exit_trade(symbol, side, pnl, steps, duration_min):
    icon = "✅" if pnl >= 0 else "❌"
    send(
        f"{icon} <b>{symbol}</b> закрыта\n"
        f"PnL: <b>{pnl:+.2f} USDT</b>\n"
        f"Усреднений: {steps}\n"
        f"Длительность: {duration_min} мин",
        keyboard=[[{"text": "📊 Статистика", "callback_data": "stats"},
                   {"text": "📱 Меню", "callback_data": "menu"}]],
    )


def report(title, day, month, equity, open_positions):
    def block(label, s):
        n = s["n"] or 0
        wins = s["wins"] or 0
        wr = (wins / n * 100) if n else 0
        return (
            f"<b>{label}</b>\n"
            f"  Сделок: {n}, винрейт: {wr:.0f}%\n"
            f"  PnL: {s['pnl']:+.2f} USDT\n"
            f"  Среднее усреднений: {s['avg_steps']:.1f}"
        )

    pos_lines = "\n".join(
        f"  {p['symbol']} {p['side']}: {float(p['unrealisedPnl']):+.2f} "
        f"(вход {p['avgPrice']})"
        for p in open_positions
    ) or "  нет"

    send(
        f"📊 <b>{title}</b>\n\n"
        f"{block('За сутки', day)}\n\n"
        f"{block('За 30 дней', month)}\n\n"
        f"<b>Эквити:</b> {equity:.2f} USDT\n"
        f"<b>Открытые позиции:</b>\n{pos_lines}",
        keyboard=MENU_BTN,
    )


def warn(text):
    send(f"⚠️ {text}", keyboard=MENU_BTN)
