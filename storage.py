"""SQLite: активные сделки, журнал закрытых, кулдауны. Переживает рестарт Railway."""
import json
import logging
import os
import sqlite3
import time

import config

log = logging.getLogger("storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS active_trades (
    symbol       TEXT PRIMARY KEY,
    side         TEXT NOT NULL,
    entry_price  REAL NOT NULL,
    opened_at    INTEGER NOT NULL,
    grid         TEXT NOT NULL,
    grid_placed  INTEGER NOT NULL DEFAULT 0,
    last_size    REAL NOT NULL DEFAULT 0,
    steps_filled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS closed_trades (
    exec_key   TEXT PRIMARY KEY,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    qty        REAL,
    entry      REAL,
    exit_price REAL,
    pnl        REAL NOT NULL,
    steps      INTEGER DEFAULT 0,
    closed_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cooldowns (
    symbol  TEXT PRIMARY KEY,
    until   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Storage:
    def __init__(self, path=None):
        path = path or config.DB_PATH
        self._ensure_dir(path)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @staticmethod
    def _ensure_dir(path):
        """
        Создаёт папку под базу, если её нет.

        На Railway DB_PATH=/data/bot.db работает только когда к сервису подключён
        том с mount path /data. Без тома папки нет, sqlite падает с
        'unable to open database file'. Здесь папка создаётся, бот продолжает
        работать — но база окажется внутри контейнера и сотрётся при редеплое,
        поэтому пишем громкое предупреждение.
        """
        d = os.path.dirname(os.path.abspath(path))
        if os.path.isdir(d):
            return
        try:
            os.makedirs(d, exist_ok=True)
            log.warning(
                "Папки %s не было, создал её. Похоже, том Railway не подключён: "
                "журнал сделок и кулдауны сотрутся при следующем редеплое. "
                "Settings -> Volumes -> Add Volume, mount path %s",
                d, d,
            )
        except OSError as e:
            raise RuntimeError(
                f"Не могу создать папку для базы: {d} ({e}). "
                f"На Railway подключи том с mount path {d} "
                f"или убери переменную DB_PATH, тогда база ляжет рядом с кодом."
            ) from e

    # ---------- активные сделки ----------

    def open_trade(self, symbol, side, entry_price, grid):
        self.conn.execute(
            "INSERT OR REPLACE INTO active_trades "
            "(symbol, side, entry_price, opened_at, grid, grid_placed, last_size, steps_filled) "
            "VALUES (?,?,?,?,?,0,0,0)",
            (symbol, side, entry_price, int(time.time()), json.dumps(grid)),
        )
        self.conn.commit()

    def get_trade(self, symbol):
        row = self.conn.execute(
            "SELECT * FROM active_trades WHERE symbol=?", (symbol,)
        ).fetchone()
        return dict(row) if row else None

    def all_trades(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM active_trades")]

    def update_trade(self, symbol, **fields):
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE active_trades SET {sets} WHERE symbol=?",
            (*fields.values(), symbol),
        )
        self.conn.commit()

    def close_trade(self, symbol):
        self.conn.execute("DELETE FROM active_trades WHERE symbol=?", (symbol,))
        self.conn.commit()

    # ---------- журнал ----------

    def log_closed(self, exec_key, symbol, side, qty, entry, exit_price, pnl, steps, closed_at):
        try:
            self.conn.execute(
                "INSERT INTO closed_trades "
                "(exec_key, symbol, side, qty, entry, exit_price, pnl, steps, closed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (exec_key, symbol, side, qty, entry, exit_price, pnl, steps, closed_at),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # уже записано

    def stats(self, since_ts):
        row = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins, "
            "COALESCE(AVG(steps),0) avg_steps "
            "FROM closed_trades WHERE closed_at>=?",
            (since_ts,),
        ).fetchone()
        return dict(row)

    # ---------- кулдауны ----------

    def set_cooldown(self, symbol, minutes):
        self.conn.execute(
            "INSERT OR REPLACE INTO cooldowns (symbol, until) VALUES (?,?)",
            (symbol, int(time.time()) + minutes * 60),
        )
        self.conn.commit()

    def in_cooldown(self, symbol):
        row = self.conn.execute(
            "SELECT until FROM cooldowns WHERE symbol=?", (symbol,)
        ).fetchone()
        return bool(row and row["until"] > time.time())

    # ---------- meta ----------

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, str(value))
        )
        self.conn.commit()
