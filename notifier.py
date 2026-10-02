"""Уведомления в Telegram. Молча пропускает отправку, если токен не задан."""
import logging

import requests

import config

log = logging.getLogger("tg")


def send(text):
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.info("[TG не настроен] %s", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        log.warning("Telegram: %s", e)


def entry(symbol, side, price, qty, notional, grid_prices):
    arrow = "🟢 LONG" if side == "Buy" else "🔴 SHORT"
    levels = "\n".join(
        f"   {i+1}. {p}  (${n:.0f})" for i, (p, n) in enumerate(grid_prices)
    )
    send(
        f"{arrow} <b>{symbol}</b>\n"
        f"Вход: {price}\n"
        f"Объём: {qty} (${notional:.0f})\n"
        f"Сетка доборов:\n{levels}"
    )


def averaging(symbol, side, step, avg_price, size, notional, new_tp):
    send(
        f"➕ <b>{symbol}</b> усреднение #{step}\n"
        f"Новая средняя: {avg_price}\n"
        f"Позиция: {size} (${notional:.0f})\n"
        f"Тейк переставлен: {new_tp}"
    )


def exit_trade(symbol, side, pnl, steps, duration_min):
    icon = "✅" if pnl >= 0 else "❌"
    send(
        f"{icon} <b>{symbol}</b> закрыта\n"
        f"PnL: <b>{pnl:+.2f} USDT</b>\n"
        f"Усреднений: {steps}\n"
        f"Длительность: {duration_min} мин"
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
        f"<b>Открытые позиции:</b>\n{pos_lines}"
    )


def warn(text):
    send(f"⚠️ {text}")
