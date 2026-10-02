"""Отбор вселенной и поиск сигналов: EMA(50) + RSI(14) + ATR(14) на 5m."""
import logging
import time

import config
from indicators import atr, ema, rsi_series

log = logging.getLogger("scanner")

MS_PER_DAY = 86_400_000


class Scanner:
    def __init__(self, client, storage):
        self.c = client
        self.db = storage

    # ---------- вселенная ----------

    def universe(self, tickers):
        """Монеты: листинг >= 30 дней, оборот >= $10M, не в блэклисте. Топ-N по обороту."""
        now_ms = int(time.time() * 1000)
        rows = []
        for symbol, t in tickers.items():
            if not symbol.endswith("USDT") or symbol in config.BLACKLIST:
                continue
            inst = self.c.instrument(symbol)
            if not inst:
                continue
            if inst["launch_time"] and (now_ms - inst["launch_time"]) < config.MIN_LISTING_DAYS * MS_PER_DAY:
                continue
            turnover = float(t.get("turnover24h") or 0)
            if turnover < config.MIN_TURNOVER_24H:
                continue
            rows.append((symbol, turnover))

        rows.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in rows[: config.MAX_SCAN_SYMBOLS]]

    # ---------- рыночный фильтр ----------

    def btc_hour_change(self):
        """Изменение BTC за последний час в %, по 5-минуткам."""
        try:
            k = self.c.klines("BTCUSDT", limit=20)
            if len(k) < 14:
                return 0.0
            closed = k[:-1]
            return (closed[-1][4] / closed[-13][4] - 1) * 100
        except Exception as e:
            log.warning("BTC-фильтр недоступен: %s", e)
            return 0.0

    # ---------- сигнал ----------

    def signal(self, symbol, allow_long=True):
        """Возвращает 'Buy' / 'Sell' / None. Работает только по закрытым свечам."""
        try:
            k = self.c.klines(symbol, limit=200)
        except Exception as e:
            log.debug("klines %s: %s", symbol, e)
            return None

        if len(k) < config.EMA_PERIOD + 20:
            return None

        bars = k[:-1]  # отбрасываем незакрытую свечу
        highs = [b[2] for b in bars]
        lows = [b[3] for b in bars]
        closes = [b[4] for b in bars]

        price = closes[-1]

        e = ema(closes, config.EMA_PERIOD)
        a = atr(highs, lows, closes, config.ATR_PERIOD)
        r = rsi_series(closes, config.RSI_PERIOD)
        if e is None or a is None or r[-1] is None or r[-2] is None:
            return None

        # фильтр волатильности: тейк 0.6% должен быть достижим
        if a / price * 100 < config.MIN_ATR_PCT:
            return None

        # фильтр события: за последний час ушла слишком далеко
        if len(closes) > 13:
            hour_move = abs(closes[-1] / closes[-13] - 1) * 100
            if hour_move > config.MAX_HOUR_MOVE_PCT:
                return None

        prev, cur = r[-2], r[-1]

        # лонг: цена выше EMA50, RSI вышел вверх из перепроданности
        if allow_long and price > e and prev < config.RSI_OVERSOLD <= cur:
            return "Buy"

        # шорт: цена ниже EMA50, RSI вышел вниз из перекупленности
        if price < e and prev > config.RSI_OVERBOUGHT >= cur:
            return "Sell"

        return None

    def find_signals(self, busy_symbols, slots):
        """Ищет до `slots` сигналов среди свободных монет."""
        tickers = self.c.tickers()
        symbols = self.universe(tickers)

        btc_move = self.btc_hour_change()
        allow_long = btc_move > config.BTC_CRASH_PCT
        if not allow_long:
            log.info("BTC за час %.2f%% — лонги заблокированы", btc_move)

        found = []
        for symbol in symbols:
            if len(found) >= slots:
                break
            if symbol in busy_symbols or self.db.in_cooldown(symbol):
                continue
            side = self.signal(symbol, allow_long=allow_long)
            if side:
                price = float(tickers[symbol]["lastPrice"])
                found.append((symbol, side, price))
                log.info("Сигнал: %s %s @ %s", symbol, side, price)
        return found
