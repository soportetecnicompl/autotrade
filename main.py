"""Copiador de señales de Telegram (XAUUSD) -> MetaTrader 5.

Uso:
  python main.py                      # escucha el canal y opera
  python main.py --list-dialogs       # muestra tus chats/canales con su ID
  python main.py --test "texto" [--price 2650.2]   # prueba el parser (sin Telegram ni MT5)
"""
from __future__ import annotations

import argparse
import asyncio
import configparser
import logging
import sys
from datetime import datetime, timezone

from autotrade.parser import parse
from autotrade.planner import Skip, plan_orders
from autotrade.schedule import TradingSchedule

log = logging.getLogger("main")


def load_config(path: str) -> dict:
    cp = configparser.ConfigParser(interpolation=None)
    if not cp.read(path, encoding="utf-8"):
        raise SystemExit(f"No encuentro {path}. Copia config.example.ini a config.ini y edítalo.")
    t, m, r = cp["telegram"], cp["mt5"], cp["trading"]
    f = cp["filters"] if cp.has_section("filters") else {}
    try:
        schedule = TradingSchedule.from_text(
            f.get("trading_days", ""), f.get("trading_hours", ""),
            float(f.get("tz_offset_hours", 0) or 0))
    except ValueError as e:
        raise SystemExit(f"[filters] en {path}: {e}")
    return {
        "schedule": schedule,
        "api_id": t.getint("api_id"), "api_hash": t["api_hash"],
        "phone": t.get("phone", ""), "channel": t["channel"],
        "session_name": t.get("session_name", "autotrade"),
        "path": m.get("path", ""), "login": m.get("login", ""),
        "password": m.get("password", ""), "server": m.get("server", ""),
        "symbol": m.get("symbol", "XAUUSD"), "magic": m.getint("magic", 20260101),
        "deviation": m.getint("deviation", 30),
        "dry_run": r.getboolean("dry_run", True),
        "lot_size": r.getfloat("lot_size", 0.01),
        "risk_percent": r.getfloat("risk_percent", 0.0),
        "max_tps": r.getint("max_tps", 2),
        "max_open_positions": r.getint("max_open_positions", 6),
        "market_tolerance": r.getfloat("market_tolerance", 1.5),
        "max_pending_distance": r.getfloat("max_pending_distance", 30.0),
        "pending_expiry_min": r.getint("pending_expiry_min", 60),
        "max_signal_age_sec": r.getint("max_signal_age_sec", 60),
        "allow_no_tp": r.getboolean("allow_no_tp", False),
        "enable_commands": r.getboolean("enable_commands", True),
        "be_offset": r.getfloat("be_offset", 0.0),
    }


def cmd_test(text: str, price: float | None) -> None:
    res = parse(text)
    if not res:
        print("→ Mensaje ignorado (no es señal ni comando)")
        return
    if res.signal:
        print("→ SEÑAL:", res.signal)
        if price is not None:
            try:
                for p in plan_orders(res.signal, price - 0.15, price + 0.15):
                    print("   orden:", p)
            except Skip as e:
                print("   se ignoraría:", e)
    for c in res.commands:
        print("→ COMANDO:", c)


async def run(cfg: dict, list_dialogs: bool) -> None:
    from telethon import TelegramClient, events

    client = TelegramClient(cfg["session_name"], cfg["api_id"], cfg["api_hash"])
    await client.start(phone=cfg["phone"] or None)  # pide código la primera vez

    if list_dialogs:
        async for d in client.iter_dialogs():
            print(f"{d.id:>16}  {d.name}")
        await client.disconnect()
        return

    from autotrade.trader import Trader

    trader = Trader(cfg)
    trader.connect()

    ref = cfg["channel"].strip()
    entity = await client.get_entity(int(ref) if ref.lstrip("-").isdigit() else ref)
    log.info("Escuchando '%s'. dry_run=%s", getattr(entity, "title", ref), cfg["dry_run"])
    sched = cfg["schedule"]
    log.info("Filtro de horario: %s", "sin restricción" if sched.always else sched.describe())

    @client.on(events.NewMessage(chats=entity))
    async def on_message(event):
        text = event.raw_text or ""
        msg_id = event.message.id
        log.info("Mensaje %s: %r", msg_id, text[:200])
        res = parse(text)
        if not res:
            return
        try:
            if res.signal:
                age = (datetime.now(timezone.utc) - event.message.date).total_seconds()
                if age > cfg["max_signal_age_sec"]:
                    log.warning("Señal %s ignorada: tiene %.0fs de antigüedad", msg_id, age)
                    return
                if not sched.allows(event.message.date.timestamp()):
                    log.info("Señal %s ignorada: fuera de horario (%s)", msg_id, sched.describe())
                    return
                trader.open_signal(res.signal, msg_id)
            elif cfg["enable_commands"]:
                # Si el comando responde a una señal, solo afecta a esa señal.
                scope = event.message.reply_to_msg_id
                for c in res.commands:
                    trader.handle_command(c, scope)
        except Exception:
            log.exception("Error procesando mensaje %s", msg_id)

    try:
        await client.run_until_disconnected()
    finally:
        trader.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--list-dialogs", action="store_true")
    ap.add_argument("--test", metavar="TEXTO")
    ap.add_argument("--price", type=float, help="precio actual para --test")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("autotrade.log", encoding="utf-8")],
    )
    if a.test:
        return cmd_test(a.test, a.price)
    cfg = load_config(a.config)
    try:
        asyncio.run(run(cfg, a.list_dialogs))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
