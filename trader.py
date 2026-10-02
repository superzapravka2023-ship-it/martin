"""Торговое ядро: вход, сетка доборов, перестановка тейка, уборка ордеров, журнал."""
import json
import logging
import time
from decimal import Decimal

import config
import notifier
from bybit_client import retry

log = logging.getLogger("trader")


class Trader:
    def __init__(self, client, storage):
        self.c = client
        self.db = storage

    # ------------------------------------------------------------------
    # ВХОД
    # ------------------------------------------------------------------

    def open_position(self, symbol, side, price):
        """Маркет-вход базовым ордером + сразу вся сетка лимиток на добор."""
        self.c.ensure_leverage(symbol)

        qty = self.c.qty_from_notional(symbol, config.BASE_NOTIONAL, price)
        if qty is None:
            log.info("%s: объём меньше минимального лота, пропуск", symbol)
            return False

        try:
            retry(lambda: self.c.market_order(symbol, side, qty), label=f"entry {symbol}")
        except Exception:
            return False

        # цена входа по факту — берём из позиции, с небольшой паузой на применение
        time.sleep(1.5)
        pos = self._position(symbol)
        entry = float(pos["avgPrice"]) if pos else price

        grid = self._grid_levels(symbol, side, entry)
        self.db.open_trade(symbol, side, entry, grid)

        placed = self._place_grid(symbol, side, grid)
        self.db.update_trade(symbol, grid_placed=1, last_size=float(pos["size"]) if pos else float(qty))

        self._sync_take_profit(symbol, side, entry, Decimal(str(pos["size"])) if pos else qty)

        notifier.entry(
            symbol, side, entry, qty, config.BASE_NOTIONAL,
            [(g["price"], g["notional"]) for g in placed],
        )
        return True

    def _grid_levels(self, symbol, side, entry):
        """Уровни доборов: для лонга ниже входа, для шорта выше."""
        grid = []
        for i, (offset_pct, notional) in enumerate(config.GRID_STEPS, start=1):
            if side == "Buy":
                raw = entry * (1 - offset_pct / 100)
            else:
                raw = entry * (1 + offset_pct / 100)
            price = self.c.round_price(symbol, raw)
            grid.append({
                "step": i,
                "offset_pct": offset_pct,
                "notional": notional,
                "price": str(price),
            })
        return grid

    def _place_grid(self, symbol, side, grid):
        placed = []
        for g in grid:
            price = Decimal(g["price"])
            qty = self.c.qty_from_notional(symbol, g["notional"], float(price))
            if qty is None:
                log.warning("%s: шаг %d — объём ниже минимального, пропущен", symbol, g["step"])
                continue
            try:
                retry(
                    lambda: self.c.limit_order(symbol, side, qty, price, reduce_only=False),
                    label=f"grid {symbol} #{g['step']}",
                )
                placed.append(g)
            except Exception:
                log.error("%s: не удалось выставить добор #%d", symbol, g["step"])
        return placed

    # ------------------------------------------------------------------
    # ТЕЙК-ПРОФИТ
    # ------------------------------------------------------------------

    def _tp_price(self, symbol, side, avg_price):
        if side == "Buy":
            raw = avg_price * (1 + config.TAKE_PROFIT_PCT / 100)
        else:
            raw = avg_price * (1 - config.TAKE_PROFIT_PCT / 100)
        return self.c.round_price(symbol, raw)

    def _sync_take_profit(self, symbol, side, avg_price, size, open_orders=None):
        """Держит ровно один reduceOnly-лимитник на весь объём позиции по актуальной средней."""
        target = self._tp_price(symbol, side, avg_price)
        close_side = "Sell" if side == "Buy" else "Buy"

        if open_orders is None:
            open_orders = self.c.open_orders().get(symbol, [])

        existing = [o for o in open_orders if o.get("reduceOnly")]

        for o in existing:
            same_price = Decimal(o["price"]) == target
            same_qty = Decimal(o["qty"]) == Decimal(str(size))
            if same_price and same_qty:
                return False  # уже корректный
            self.c.cancel_order(symbol, o["orderId"])

        try:
            retry(
                lambda: self.c.limit_order(symbol, close_side, size, target, reduce_only=True),
                label=f"tp {symbol}",
            )
            log.info("%s: тейк на %s (объём %s)", symbol, target, size)
            return True
        except Exception:
            log.error("%s: не удалось выставить тейк", symbol)
            return False

    # ------------------------------------------------------------------
    # СВЕРКА С БИРЖЕЙ
    # ------------------------------------------------------------------

    def reconcile(self):
        """
        Главный цикл сверки. Делает три вещи:
          1. позиция выросла  -> сработало усреднение, переставляем тейк
          2. позиция исчезла  -> снимаем остаток сетки, журнал, кулдаун
          3. ордера по чужому символу -> не трогаем, только лог
        """
        positions = {p["symbol"]: p for p in self.c.positions()}
        orders = self.c.open_orders()
        tracked = {t["symbol"]: t for t in self.db.all_trades()}

        # --- 1. живые позиции ---
        for symbol, pos in positions.items():
            size = float(pos["size"])
            avg = float(pos["avgPrice"])
            side = "Buy" if pos["side"] == "Buy" else "Sell"
            trade = tracked.get(symbol)

            if trade is None:
                # позиция без записи (ручной вход или потеря базы) — подхватываем
                log.warning("%s: позиция без записи в базе, беру под управление", symbol)
                self.db.open_trade(symbol, side, avg, self._grid_levels(symbol, side, avg))
                self.db.update_trade(symbol, grid_placed=1, last_size=size)
                self._sync_take_profit(symbol, side, avg, Decimal(pos["size"]), orders.get(symbol))
                continue

            if size > trade["last_size"] * 1.0001:
                steps = trade["steps_filled"] + 1
                self.db.update_trade(symbol, last_size=size, steps_filled=steps)
                self._sync_take_profit(symbol, side, avg, Decimal(pos["size"]), orders.get(symbol))
                notifier.averaging(
                    symbol, side, steps,
                    self.c.round_price(symbol, avg),   # до шага цены, без хвоста float
                    size, size * avg,
                    self._tp_price(symbol, side, avg),
                )
            else:
                # страховка: тейк мог не выставиться или быть снят
                self._sync_take_profit(symbol, side, avg, Decimal(pos["size"]), orders.get(symbol))

        # --- 2. закрывшиеся сделки: снимаем недоисполненную сетку, пишем журнал ---
        # Трогаем ТОЛЬКО свои символы из базы: на счёте могут работать другие боты,
        # и слепой cancel_all по чужому символу снял бы их ордера.
        for symbol, trade in tracked.items():
            if symbol in positions:
                continue
            leftovers = len(orders.get(symbol, []))
            if leftovers:
                log.info("%s: позиция закрыта, снимаю %d неисполненных ордеров", symbol, leftovers)
            self.c.cancel_all(symbol)
            self.db.close_trade(symbol)
            self.db.set_cooldown(symbol, config.COOLDOWN_MINUTES)
            self._log_closed(symbol, trade)

        # --- 3. чужие висяки: только предупреждение, ничего не отменяем ---
        for symbol in orders:
            if symbol not in positions and symbol not in tracked:
                log.warning("%s: ордера без позиции и без записи в базе — не мои, не трогаю", symbol)

    def _log_closed(self, symbol, trade):
        """Достаёт реальный PnL из closed-pnl и шлёт отчёт по сделке."""
        try:
            rows = self.c.closed_pnl(limit=50)
        except Exception as e:
            log.warning("closed_pnl: %s", e)
            rows = []

        total = 0.0
        logged = False
        opened_ms = trade["opened_at"] * 1000

        for r in rows:
            if r["symbol"] != symbol:
                continue
            if int(r["updatedTime"]) < opened_ms:
                continue
            pnl = float(r["closedPnl"])
            ok = self.db.log_closed(
                exec_key=r["orderId"],
                symbol=symbol,
                side=trade["side"],
                qty=float(r.get("qty") or 0),
                entry=trade["entry_price"],
                exit_price=float(r.get("avgExitPrice") or 0),
                pnl=pnl,
                steps=trade["steps_filled"],
                closed_at=int(int(r["updatedTime"]) / 1000),
            )
            if ok:
                total += pnl
                logged = True

        duration = int((time.time() - trade["opened_at"]) / 60)
        if not logged:
            log.warning("%s: закрытие не нашлось в closed-pnl, PnL в отчёте будет нулевым", symbol)
        notifier.exit_trade(symbol, trade["side"], total, trade["steps_filled"], duration)

    # ------------------------------------------------------------------

    def _position(self, symbol):
        for p in self.c.positions():
            if p["symbol"] == symbol:
                return p
        return None

    def open_symbols(self):
        return {t["symbol"] for t in self.db.all_trades()}
