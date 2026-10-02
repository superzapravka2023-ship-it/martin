"""
DCA-скальпер для Bybit (демо).
Вход по EMA50 + RSI14 на 5m, до 4 усреднений, выход по тейку от средней цены.
Стопов нет; защита — порог эквити, ниже которого новые позиции не открываются.
"""
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import config
import notifier
from bybit_client import BybitClient
from scanner import Scanner
from storage import Storage
from telegram_ui import TelegramUI
from trader import Trader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")

TZ = timezone(timedelta(hours=config.TZ_OFFSET_HOURS))


class Bot:
    def __init__(self):
        self.client = BybitClient()
        self.db = Storage()
        self.scanner = Scanner(self.client, self.db)
        self.trader = Trader(self.client, self.db)
        self.last_scan = 0.0
        self.halted = False
        self.paused = False                 # ставится кнопкой в Telegram
        self.lock = threading.RLock()       # общий с потоком интерфейса

    # ------------------------------------------------------------------

    def start(self):
        self.client.load_instruments()
        equity = self.client.equity()
        mode = "ДЕМО" if config.DEMO else "РЕАЛ"

        TelegramUI(self).start()

        notifier.send(
            f"🤖 <b>DCA-скальпер запущен</b> ({mode})\n"
            f"Эквити: {equity:.2f} USDT\n"
            f"База ${config.BASE_NOTIONAL:.0f} × {config.LEVERAGE}x, "
            f"до {len(config.GRID_STEPS)} усреднений, "
            f"максимум {config.MAX_POSITIONS} позиции\n"
            f"Тейк {config.TAKE_PROFIT_PCT}% от средней, стопов нет\n"
            f"Порог остановки: {config.EQUITY_FLOOR:.0f} USDT",
            keyboard=[[{"text": "📱 Открыть меню", "callback_data": "menu"}]],
        )
        self.loop()

    def loop(self):
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                notifier.send("⏹ Бот остановлен вручную")
                return
            except Exception as e:
                log.exception("Ошибка цикла: %s", e)
                notifier.warn(f"Ошибка цикла: {e}")
            time.sleep(config.RECONCILE_SEC)

    # ------------------------------------------------------------------

    def tick(self):
        # 1. сверка с биржей: доборы, тейки, уборка ордеров, закрытия
        with self.lock:
            self.trader.reconcile()

        # 2. защита счёта
        equity = self.client.equity()
        if equity < config.EQUITY_FLOOR:
            if not self.halted:
                self.halted = True
                notifier.warn(
                    f"Эквити {equity:.2f} USDT ниже порога {config.EQUITY_FLOOR:.0f}. "
                    f"Новые позиции не открываются. Открытые остаются под управлением."
                )
            self.daily_report_if_due(equity)
            return
        if self.halted and equity > config.EQUITY_FLOOR * 1.05:
            self.halted = False
            notifier.send(f"✅ Эквити восстановилось: {equity:.2f} USDT. Торговля возобновлена.")

        # 3. поиск новых входов
        if not self.paused:
            busy = self.trader.open_symbols()
            slots = config.MAX_POSITIONS - len(busy)
            if slots > 0 and time.time() - self.last_scan >= config.SCAN_SEC:
                self.last_scan = time.time()
                for symbol, side, price in self.scanner.find_signals(busy, slots):
                    with self.lock:
                        opened = self.trader.open_position(symbol, side, price)
                    if opened:
                        busy.add(symbol)
                        if len(busy) >= config.MAX_POSITIONS:
                            break

        # 4. суточный отчёт
        self.daily_report_if_due(equity)

    # ------------------------------------------------------------------

    def daily_report_if_due(self, equity):
        now = datetime.now(TZ)
        if now.hour != config.DAILY_REPORT_HOUR:
            return
        stamp = now.strftime("%Y-%m-%d")
        if self.db.get_meta("last_report") == stamp:
            return
        self.db.set_meta("last_report", stamp)

        day = self.db.stats(int(time.time()) - 86_400)
        month = self.db.stats(int(time.time()) - 30 * 86_400)
        try:
            positions = self.client.positions()
        except Exception:
            positions = []
        notifier.report(f"Отчёт за {stamp}", day, month, equity, positions)


if __name__ == "__main__":
    if not config.BYBIT_API_KEY or not config.BYBIT_API_SECRET:
        log.error("Не заданы BYBIT_API_KEY / BYBIT_API_SECRET")
        sys.exit(1)
    Bot().start()
