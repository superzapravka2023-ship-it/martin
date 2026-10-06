"""Конфигурация DCA-бота. Все значения переопределяются через переменные окружения."""
import os


def _f(name, default):
    return float(os.getenv(name, default))


def _i(name, default):
    return int(os.getenv(name, default))


# ---------- API ----------
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
DEMO = os.getenv("DEMO", "true").lower() == "true"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------- Капитал и сетка ----------
BASE_NOTIONAL = _f("BASE_NOTIONAL", 200)      # номинал первого ордера, USDT
LEVERAGE = _i("LEVERAGE", 20)                  # кросс-маржа, 20x
MAX_POSITIONS = _i("MAX_POSITIONS", 3)

# Шаги усреднения: (отклонение от цены входа в %, множитель к BASE_NOTIONAL).
# Множители подобраны под депозит $1000 при базе $200 — тогда суммы выходят
# 260 / 340 / 440 / 580, а полная сетка даёт среднюю на 2.85% ниже входа.
# Привязка к базе, а не абсолютные суммы, нужна чтобы весь размер
# масштабировался одной переменной BASE_NOTIONAL под любой депозит.
GRID_MULTIPLIERS = [
    (0.8, 1.3),
    (1.8, 1.7),
    (3.2, 2.2),
    (5.0, 2.9),
]

GRID_STEPS = [(off, round(BASE_NOTIONAL * k, 2)) for off, k in GRID_MULTIPLIERS]

# Полный размер одной залитой позиции и всех трёх вместе — для проверок.
FULL_GRID_NOTIONAL = BASE_NOTIONAL + sum(n for _, n in GRID_STEPS)
TOTAL_EXPOSURE = FULL_GRID_NOTIONAL * MAX_POSITIONS

TAKE_PROFIT_PCT = _f("TAKE_PROFIT_PCT", 0.6)   # % от средней цены позиции

# ---------- Фильтры вселенной ----------
MIN_TURNOVER_24H = _f("MIN_TURNOVER_24H", 10_000_000)   # $10M
MIN_LISTING_DAYS = _i("MIN_LISTING_DAYS", 30)
MAX_SCAN_SYMBOLS = _i("MAX_SCAN_SYMBOLS", 120)          # топ-N по обороту
BLACKLIST = {s.strip().upper() for s in os.getenv("BLACKLIST", "").split(",") if s.strip()}

# ---------- Сигналы ----------
# RSI(7), а не RSI(14): на 5m откат, достаточный чтобы RSI(14) ушёл под 30,
# почти всегда уже уводит цену под EMA50, и связка EMA+RSI14(30/70) не даёт
# сигналов вообще. RSI(7) достигает экстремума, пока цена ещё выше EMA50.
# Замер на модели (120 монет): RSI14 30/70 — 0 сигналов в день, RSI14 35/65 — ~4,
# RSI14 40/60 — ~94, RSI7 30/70 — ~228, RSI7 35/65 — ~643.
TIMEFRAME = "5"
EMA_PERIOD = _i("EMA_PERIOD", 50)
RSI_PERIOD = _i("RSI_PERIOD", 7)
RSI_OVERSOLD = _f("RSI_OVERSOLD", 30)          # ослабить до 35 — больше сделок
RSI_OVERBOUGHT = _f("RSI_OVERBOUGHT", 70)      # ослабить до 65 — больше сделок
ATR_PERIOD = _i("ATR_PERIOD", 14)
MIN_ATR_PCT = _f("MIN_ATR_PCT", 0.4)           # ATR не меньше 0.4% от цены

MAX_HOUR_MOVE_PCT = _f("MAX_HOUR_MOVE_PCT", 8.0)   # пропуск, если за час ушла дальше
BTC_CRASH_PCT = _f("BTC_CRASH_PCT", -3.0)          # BTC за час — запрет новых лонгов

COOLDOWN_MINUTES = _i("COOLDOWN_MINUTES", 60)

# ---------- Защита счёта ----------
EQUITY_FLOOR = _f("EQUITY_FLOOR", 600)   # ниже — новых позиций не открываем

# ---------- Тайминги ----------
RECONCILE_SEC = _i("RECONCILE_SEC", 10)
SCAN_SEC = _i("SCAN_SEC", 60)
DAILY_REPORT_HOUR = _i("DAILY_REPORT_HOUR", 9)   # час по Europe/Minsk
TZ_OFFSET_HOURS = _i("TZ_OFFSET_HOURS", 3)

DB_PATH = os.getenv("DB_PATH", "bot.db")
