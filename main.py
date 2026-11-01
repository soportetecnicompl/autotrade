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
import os
import sys
import urllib.request
from datetime import datetime, timezone

from autotrade.parser import looks_like_signal, parse
from autotrade.planner import Skip, plan_orders
from autotrade.channels import parse_channels
from autotrade.schedule import TradingSchedule

log = logging.getLogger("main")


def load_config(path: str) -> dict:
    cp = configparser.ConfigParser(interpolation=None)
    if not cp.read(path, encoding="utf-8"):
        raise SystemExit(f"No encuentro {path}. Copia config.example.ini a config.ini y edítalo.")
    t, m, r = cp["telegram"], cp["mt5"], cp["trading"]
    if r.get("stops_policy", "skip").strip().lower() not in ("skip", "adjust"):
        raise SystemExit("[trading] stops_policy debe ser 'skip' o 'adjust'")
    f = cp["filters"] if cp.has_section("filters") else {}
    try:
        schedule = TradingSchedule.from_text(
            f.get("trading_days", ""), f.get("trading_hours", ""),
            float(f.get("tz_offset_hours", 0) or 0))
    except ValueError as e:
        raise SystemExit(f"[filters] en {path}: {e}")
    rk = cp["risk"] if cp.has_section("risk") else {}
    al = cp["alerts"] if cp.has_section("alerts") else {}
    yes = lambda k, d: str(al.get(k, d)).strip().lower() in ("1", "true", "yes", "si", "sí", "on")
    return {
        "schedule": schedule,
        "alerts_enabled": yes("enabled", "true"), "alerts_target": al.get("target", "me").strip() or "me",
        "notify_signals": yes("notify_signals", "true"), "notify_skips": yes("notify_skips", "true"),
        "heartbeat_url": al.get("heartbeat_url", "").strip(),
        "heartbeat_minutes": float(al.get("heartbeat_minutes", 5) or 5),
        "mt5_check_seconds": float(al.get("mt5_check_seconds", 30) or 30),
        "api_id": t.getint("api_id"), "api_hash": t["api_hash"],
        "phone": t.get("phone", ""),
        "channels": [c.to_dict() for c in parse_channels(cp)],
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
        "requote_retries": r.getint("requote_retries", 3),
        "retry_delay_ms": r.getint("retry_delay_ms", 200),
        "retry_max_slippage": r.getfloat("retry_max_slippage", 1.0),
        "stops_policy": r.get("stops_policy", "skip").strip().lower(),
        "auto_be": r.getboolean("auto_be", False),
        "tz_offset_hours": float(f.get("tz_offset_hours", 0) or 0),
        "daily_loss_limit_pct": float(rk.get("daily_loss_limit_pct", 0) or 0),
        "max_signals_per_day": int(float(rk.get("max_signals_per_day", 0) or 0)),
        "close_on_limit": str(rk.get("close_on_limit", "false")).strip().lower() in ("1", "true", "yes", "si", "sí", "on"),
        "max_spread_usd": float(f.get("max_spread_usd", 0) or 0),
        "news_mode": (f.get("news_mode", "off") or "off").strip().lower(),
        "news_before": int(float(f.get("news_minutes_before", 15) or 0)),
        "news_after": int(float(f.get("news_minutes_after", 15) or 0)),
        "news_manual": f.get("news_manual", "") or "",
        "news_currencies": [c.strip().upper() for c in (f.get("news_currencies", "USD") or "USD").split(",") if c.strip()],
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

    # connection_retries=-1: reintenta sin límite si se corta internet
    client = TelegramClient(cfg["session_name"], cfg["api_id"], cfg["api_hash"],
                            connection_retries=-1)
    await client.start(phone=cfg["phone"] or None)  # pide código la primera vez

    if list_dialogs:
        async for d in client.iter_dialogs():
            print(f"{d.id:>16}  {d.name}")
        await client.disconnect()
        return

    from autotrade.notifier import Notifier
    from autotrade.trader import Trader

    mode = "DRY-RUN (simulación)" if cfg["dry_run"] else "OPERANDO"
    notifier = Notifier(client, cfg["alerts_target"], cfg["alerts_enabled"],
                        cfg["notify_signals"], cfg["notify_skips"],
                        prefix="[DRY-RUN] " if cfg["dry_run"] else "")
    trader = Trader(cfg)
    trader.notify = notifier.notify
    # --- protecciones y control (los archivos viven en la carpeta de trabajo = carpeta de datos)
    from autotrade.control import Control
    from autotrade.eventlog import EventLog
    from autotrade.news import NewsFilter
    from autotrade.remote import RemoteHandler
    from autotrade.risk import RiskGuard
    control = Control("control.json")
    guard = RiskGuard("risk_state.json", cfg["daily_loss_limit_pct"], cfg["max_signals_per_day"],
                      cfg["tz_offset_hours"])
    news_filter = NewsFilter(cfg["news_mode"], cfg["news_before"], cfg["news_after"], cfg["tz_offset_hours"],
                             cfg["news_manual"], "news_cache.json", cfg["news_currencies"])
    trader.control, trader.guard, trader.news = control, guard, news_filter
    inbox = EventLog("signals.jsonl")
    tasks = []
    try:
        trader.connect()

        from telethon import utils as tg_utils

        by_peer = {}                      # id de Telegram -> configuración del canal
        for ch in (c for c in cfg["channels"] if c["enabled"]):
            ref = ch["ref"].strip()
            try:
                ent = await client.get_entity(int(ref) if ref.lstrip("-").isdigit() else ref)
            except Exception as e:  # noqa: BLE001 - un canal inaccesible no debe tumbar a los demás
                log.error("No se pudo acceder al canal %s: %s", ref, e)
                notifier.notify(f"❌ No se pudo acceder al canal «{ch['title'] or ref}»: {e}. "
                                "¿Sigues unido a él?", "error")
                continue
            ch = {**ch, "title": ch["title"] or getattr(ent, "title", ref)}
            by_peer[tg_utils.get_peer_id(ent)] = ch
        if not by_peer:
            raise SystemExit("No hay ningún canal activo y accesible. Abre el asistente y elige al menos uno.")
        titles = ", ".join(c["title"] for c in by_peer.values())
        sched = cfg["schedule"]
        log.info("Escuchando %d canal(es): %s. %s", len(by_peer), titles, mode)
        log.info("Filtro de horario: %s", "sin restricción" if sched.always else sched.describe())

        @client.on(events.NewMessage(chats=list(by_peer)))
        async def on_message(event):
            ch = by_peer.get(event.chat_id)
            if ch is None:
                return
            who = f"[{ch['title']}] "
            text = event.raw_text or ""
            msg_id = event.message.id
            log.info("%sMensaje %s: %r", who, msg_id, text[:200])
            rec = dict(channel=ch["num"], title=ch["title"], msg_id=msg_id, text=text)
            res = parse(text)
            if not res:
                if looks_like_signal(text):          # parece una señal pero no se entendió: no ignorarla en silencio
                    inbox.add(status="revisar", detail="parece una señal pero no la entendí", **rec)
                    notifier.notify(f"🔎 {who}El mensaje {msg_id} parece una señal pero no lo entendí: "
                                    f"«{text[:140]}». Revísalo.", "warn")
                return
            try:
                if res.signal:
                    age = (datetime.now(timezone.utc) - event.message.date).total_seconds()
                    if age > cfg["max_signal_age_sec"]:
                        log.warning("%sSeñal %s ignorada: tiene %.0fs de antigüedad", who, msg_id, age)
                        inbox.add(status="ignorada", detail=f"llegó con {age:.0f}s de retraso", **rec)
                        notifier.notify(f"⚠️ {who}Señal {msg_id} ignorada: llegó con {age:.0f}s de retraso", "warn")
                        return
                    if not sched.allows(event.message.date.timestamp()):
                        log.info("%sSeñal %s ignorada: fuera de horario (%s)", who, msg_id, sched.describe())
                        inbox.add(status="ignorada", detail="fuera de horario", **rec)
                        notifier.notify(f"⚠️ {who}Señal {msg_id} ignorada: fuera de horario", "warn")
                        return
                    trader.open_signal(res.signal, msg_id, ch)
                    status, detail = trader.outcome
                    inbox.add(status=status or "operada", detail=detail, **rec)
                elif cfg["enable_commands"]:
                    # Si el comando responde a una señal, solo afecta a esa señal; si no, solo a las
                    # operaciones de ESTE canal (nunca a las de otros canales).
                    scope = event.message.reply_to_msg_id
                    for c in res.commands:
                        trader.handle_command(c, scope, ch)
                    inbox.add(status="comando", detail=", ".join(c.kind for c in res.commands)
                              + (f" (señal {scope})" if scope else " (todo el canal)"), **rec)
                else:
                    inbox.add(status="ignorada", detail="comandos del canal desactivados", **rec)
            except Exception as e:
                log.exception("%sError procesando mensaje %s", who, msg_id)
                notifier.notify(f"💥 {who}Error procesando el mensaje {msg_id}: {type(e).__name__}: {e}", "error")

        async def mt5_watchdog():
            ok_prev = True
            while True:
                await asyncio.sleep(cfg["mt5_check_seconds"])
                try:
                    ok, why, fixable = trader.health()
                    if not ok and fixable:
                        trader.reconnect()
                        ok, why, fixable = trader.health()
                except Exception as e:  # noqa: BLE001
                    ok, why = False, f"error revisando MT5: {e}"
                if not ok and ok_prev:
                    log.error("MT5: %s", why)
                    notifier.notify(f"🔴 {why}. No se podrán operar señales hasta que se recupere.", "error")
                elif ok and not ok_prev:
                    log.info("MT5 recuperado")
                    notifier.notify("🟢 MT5 recuperado.", "error")
                ok_prev = ok

        async def housekeeping():
            """Cada 30 s: límite diario (aunque no lleguen señales), break-even automático y calendario."""
            loop = asyncio.get_running_loop()
            news_warned = False
            while True:
                await asyncio.sleep(30)
                try:
                    msg = await loop.run_in_executor(None, news_filter.refresh)   # red: fuera del hilo principal
                    if msg.startswith("No se pudo") and not news_warned:
                        news_warned = True
                        notifier.notify(f"⚠️ {msg}", "warn")
                    elif msg and not msg.startswith("No se pudo"):
                        news_warned = False
                    trader.risk_tick()
                    trader.auto_be_pass()
                except Exception:  # noqa: BLE001 - una tarea de mantenimiento nunca debe tumbar al bot
                    log.exception("Error en las tareas periódicas")

        remote = RemoteHandler(control, cfg["channels"], lambda: trader.status_text(cfg["channels"]),
                               trader.close_all, guard.resume)

        @client.on(events.NewMessage(chats="me", outgoing=True))
        async def on_saved_message(event):
            """Comandos que te escribes en «Mensajes guardados» (/estado, /pausa, /reanudar, /cerrar…)."""
            try:
                reply = remote.handle(event.raw_text or "")
            except Exception as e:  # noqa: BLE001
                log.exception("Error en un comando de Telegram")
                reply = f"❌ Error ejecutando el comando: {type(e).__name__}: {e}"
            if reply:
                log.info("Comando de Telegram: %r", (event.raw_text or "")[:60])
                await notifier.send(reply, "error")

        async def heartbeat():
            """Ping periódico a un servicio externo (p. ej. healthchecks.io): si el
            equipo se apaga o se queda sin internet, el servicio te avisa por su cuenta.
            Solo hace ping si Telegram y MT5 están sanos."""
            loop = asyncio.get_running_loop()
            ping = lambda: urllib.request.urlopen(cfg["heartbeat_url"], timeout=10).read()
            while True:
                try:
                    if client.is_connected() and trader.health()[0]:
                        await loop.run_in_executor(None, ping)
                except Exception as e:  # noqa: BLE001
                    log.warning("Latido falló: %s", e)
                await asyncio.sleep(cfg["heartbeat_minutes"] * 60)

        tasks.append(asyncio.create_task(mt5_watchdog()))
        tasks.append(asyncio.create_task(housekeeping()))
        if cfg["heartbeat_url"]:
            tasks.append(asyncio.create_task(heartbeat()))

        prot = []
        if cfg["daily_loss_limit_pct"]:
            prot.append(f"pérdida diaria máx. {cfg['daily_loss_limit_pct']:g}%")
        if cfg["max_signals_per_day"]:
            prot.append(f"máx. {cfg['max_signals_per_day']} señales/día")
        if cfg["max_spread_usd"]:
            prot.append(f"spread ≤ {cfg['max_spread_usd']:g} USD")
        if cfg["news_mode"] in ("manual", "auto"):
            prot.append(f"noticias ({cfg['news_mode']})")
        if cfg["auto_be"]:
            prot.append("break-even automático")
        restarts = int(os.environ.get("AUTOTRADE_RESTARTS", "0") or 0)
        notifier.notify(
            ("🔁 Bot REINICIADO automáticamente (reinicio #%d: se había detenido o el equipo se reinició)\n" % restarts
             if restarts else "🟢 Bot iniciado\n")
            + f"Modo: {mode}\nCanales ({len(by_peer)}): {titles}\nMT5: {trader.account_desc}\n"
            f"Símbolo: {cfg['symbol']} | lote total {cfg['lot_size']} | máx. TPs {cfg['max_tps']}\n"
            f"Horario: {'sin restricción' if sched.always else sched.describe()}\n"
            + "Protecciones: " + (", ".join(prot) or "ninguna") + "\n"
            "Control: escribe /ayuda en «Mensajes guardados» para pausar, ver el estado o cerrar todo.", "error")

        await client.run_until_disconnected()
        notifier.notify("🔴 Telegram se desconectó; el bot se reiniciará.", "error")
        await asyncio.sleep(2)  # deja salir el aviso
    except Exception as e:
        log.exception("El bot se detuvo por un error")
        await notifier.send(f"💥 El bot se detuvo por un error: {type(e).__name__}: {e}\n"
                            "Intentará reiniciarse solo si usas run_bot.bat.")
        raise
    finally:
        for t in tasks:
            t.cancel()
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
        sys.exit(0)           # parada intencional: run_bot.bat NO reinicia
    if not a.list_dialogs:
        sys.exit(2)           # terminó sin Ctrl+C (p. ej. desconexión): run_bot.bat reinicia


if __name__ == "__main__":
    main()
