"""Тонкая обёртка над Bybit v5 (pybit) с округлением под шаг инструмента."""
import logging
import time
from decimal import Decimal, ROUND_DOWN

from pybit.unified_trading import HTTP

import config

log = logging.getLogger("bybit")


class BybitClient:
    def __init__(self):
        self.http = HTTP(
            demo=config.DEMO,
            api_key=config.BYBIT_API_KEY,
            api_secret=config.BYBIT_API_SECRET,
        )
        self._instruments = {}
        self._leverage_set = set()

    # ---------- справочники ----------

    def load_instruments(self):
        """Кэш параметров инструментов: шаг объёма, шаг цены, минимальный объём, дата листинга."""
        cursor = ""
        loaded = {}
        while True:
            r = self.http.get_instruments_info(category="linear", limit=1000, cursor=cursor)
            res = r.get("result", {})
            for it in res.get("list", []):
                if it.get("quoteCoin") != "USDT" or it.get("status") != "Trading":
                    continue
                lot = it["lotSizeFilter"]
                pf = it["priceFilter"]
                loaded[it["symbol"]] = {
                    "qty_step": Decimal(lot["qtyStep"]),
                    "min_qty": Decimal(lot["minOrderQty"]),
                    "tick_size": Decimal(pf["tickSize"]),
                    "launch_time": int(it.get("launchTime") or 0),
                }
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
        self._instruments = loaded
        log.info("Инструментов загружено: %d", len(loaded))
        return loaded

    def instrument(self, symbol):
        return self._instruments.get(symbol)

    def tickers(self):
        r = self.http.get_tickers(category="linear")
        return {t["symbol"]: t for t in r["result"]["list"]}

    def klines(self, symbol, interval=config.TIMEFRAME, limit=200):
        """Возвращает свечи от старых к новым: (ts, open, high, low, close, volume)."""
        r = self.http.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
        rows = r["result"]["list"]
        rows = sorted(rows, key=lambda x: int(x[0]))
        return [
            (int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]))
            for x in rows
        ]

    # ---------- счёт ----------

    def equity(self):
        r = self.http.get_wallet_balance(accountType="UNIFIED")
        lst = r["result"]["list"]
        if not lst:
            return 0.0
        return float(lst[0].get("totalEquity") or 0)

    def positions(self):
        """Только непустые позиции."""
        out = []
        cursor = ""
        while True:
            r = self.http.get_positions(category="linear", settleCoin="USDT", limit=200, cursor=cursor)
            res = r.get("result", {})
            for p in res.get("list", []):
                if float(p.get("size") or 0) > 0:
                    out.append(p)
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
        return out

    def open_orders(self):
        """Все активные ордера, сгруппированные по символу."""
        grouped = {}
        cursor = ""
        while True:
            r = self.http.get_open_orders(category="linear", settleCoin="USDT", limit=50, cursor=cursor)
            res = r.get("result", {})
            for o in res.get("list", []):
                grouped.setdefault(o["symbol"], []).append(o)
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
        return grouped

    def closed_pnl(self, limit=50):
        r = self.http.get_closed_pnl(category="linear", limit=limit)
        return r["result"]["list"]

    # ---------- торговля ----------

    def ensure_leverage(self, symbol):
        if symbol in self._leverage_set:
            return
        try:
            self.http.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(config.LEVERAGE),
                sellLeverage=str(config.LEVERAGE),
            )
        except Exception as e:
            # 110043 = leverage not modified, это нормально
            if "110043" not in str(e):
                log.warning("set_leverage %s: %s", symbol, e)
        self._leverage_set.add(symbol)

    def qty_from_notional(self, symbol, notional, price):
        inst = self.instrument(symbol)
        if not inst:
            return None
        raw = Decimal(str(notional)) / Decimal(str(price))
        step = inst["qty_step"]
        qty = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
        if qty < inst["min_qty"]:
            return None
        return qty

    def round_price(self, symbol, price):
        inst = self.instrument(symbol)
        tick = inst["tick_size"]
        p = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return p

    def market_order(self, symbol, side, qty):
        return self.http.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            positionIdx=0,
        )

    def limit_order(self, symbol, side, qty, price, reduce_only=False):
        return self.http.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Limit",
            qty=str(qty),
            price=str(price),
            timeInForce="GTC",
            reduceOnly=reduce_only,
            positionIdx=0,
        )

    def cancel_order(self, symbol, order_id):
        try:
            self.http.cancel_order(category="linear", symbol=symbol, orderId=order_id)
        except Exception as e:
            log.warning("cancel_order %s %s: %s", symbol, order_id, e)

    def cancel_all(self, symbol):
        try:
            self.http.cancel_all_orders(category="linear", symbol=symbol)
        except Exception as e:
            log.warning("cancel_all %s: %s", symbol, e)


def retry(fn, attempts=3, delay=1.0, label=""):
    """Простой ретрай для сетевых сбоев."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if i == attempts - 1:
                log.error("%s: отказ после %d попыток: %s", label or fn.__name__, attempts, e)
                raise
            time.sleep(delay * (i + 1))
