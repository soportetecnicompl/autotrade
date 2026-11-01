"""Backtester de canales de señales.

Paso 1 - descargar el historial del canal (Telegram):
  python backtest.py fetch --days 90 --out history.json

Paso 2 - simular contra precios históricos de XAUUSD (M1):
  python backtest.py run --history history.json --prices mt5 --server-offset 2 --us-dst
  python backtest.py run --history history.json --prices xauusd_m1.csv --server-offset 0

--server-offset = horas del servidor del broker respecto a UTC (invierno).
Con --us-dst se suma 1 h durante el horario de verano de EE. UU. (típico de brokers GMT+2/+3).
Un offset mal puesto desalinea señales y precios: verifícalo.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from autotrade.analysis import analyze, render_html, render_text
from autotrade.schedule import TradingSchedule
from autotrade.backtest import (Msg, Prices, SimConfig, is_us_dst, load_csv, make_offset_fn,
                                report, simulate, write_trades_csv)


# ------------------------------------------------------------------- fetch
async def fetch(a) -> None:
    from telethon import TelegramClient

    from main import load_config

    cfg = load_config(a.config)
    since = (datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc) if a.since
             else datetime.now(timezone.utc) - timedelta(days=a.days))
    client = TelegramClient(cfg["session_name"], cfg["api_id"], cfg["api_hash"])
    await client.start(phone=cfg["phone"] or None)
    ref = cfg["channel"].strip()
    entity = await client.get_entity(int(ref) if ref.lstrip("-").isdigit() else ref)
    out = []
    async for m in client.iter_messages(entity, reverse=True, offset_date=since):
        if m.raw_text:
            out.append({"id": m.id, "ts": m.date.timestamp(), "text": m.raw_text,
                        "reply_to": m.reply_to_msg_id})
    await client.disconnect()
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"{len(out)} mensajes guardados en {a.out} (desde {since:%Y-%m-%d})")


# ------------------------------------------------------------------- precios
def load_mt5(symbol: str, t0: float, t1: float, base_h: float, us_dst: bool) -> Prices:
    import MetaTrader5 as mt5  # solo Windows

    if not mt5.initialize():
        raise SystemExit(f"mt5.initialize falló: {mt5.last_error()}")
    mt5.symbol_select(symbol, True)
    off = make_offset_fn(base_h, us_dst)
    s0, s1 = int(t0 + off(t0)) - 86400, int(t1 + off(t1)) + 2 * 86400
    # MT5 interpreta los datetime como UTC y devuelve la hora del servidor tal cual.
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1,
                                 datetime.fromtimestamp(s0, timezone.utc),
                                 datetime.fromtimestamp(s1, timezone.utc))
    mt5.shutdown()
    if rates is None or len(rates) == 0:
        raise SystemExit("MT5 no devolvió velas M1. Abre el gráfico M1 del símbolo, sube "
                         "'Max bars in chart' y desplázate atrás para descargar historia; "
                         "o usa un CSV con --prices archivo.csv")
    bars = [(int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
            for r in rates]
    print(f"MT5: {len(bars)} velas M1 de {datetime.fromtimestamp(bars[0][0], timezone.utc):%Y-%m-%d} "
          f"a {datetime.fromtimestamp(bars[-1][0], timezone.utc):%Y-%m-%d}")
    return Prices(bars, off)


# ------------------------------------------------------------------- run
def run(a) -> None:
    with open(a.history, encoding="utf-8") as f:
        msgs = [Msg(d["id"], d["ts"], d["text"], d.get("reply_to")) for d in json.load(f)]
    if not msgs:
        raise SystemExit("El historial está vacío")
    t0, t1 = min(m.ts for m in msgs), max(m.ts for m in msgs)

    if a.prices == "mt5":
        P = load_mt5(a.symbol, t0, t1, a.server_offset, a.us_dst)
    else:
        P = load_csv(a.prices, make_offset_fn(a.server_offset, a.us_dst))
        print(f"CSV: {len(P.bars)} velas")

    cfg = SimConfig(lot=a.lot, spread=a.spread, delay=a.delay, max_hold_hours=a.max_hold_hours,
                    pending_expiry_min=a.pending_expiry_min, max_tps=a.max_tps,
                    market_tolerance=a.market_tolerance, volume_min=a.volume_min,
                    volume_step=a.volume_step,
                    schedule=TradingSchedule.from_text(a.trading_days, a.trading_hours, a.report_tz))
    if cfg.schedule and not cfg.schedule.always:
        print("Filtro de horario:", cfg.schedule.describe())
    A = analyze(msgs, P, cfg, balance=a.balance, tz=a.report_tz, min_n=a.min_hour_signals,
                sweep=not a.no_sweep)
    print()
    print(report({"static": A["static_trades"], "follow": A["follow_trades"]}, A["skipped"], cfg, a.report_tz))
    print(render_text(A))
    write_trades_csv(a.out, A["static_trades"] + A["follow_trades"])
    with open(a.report, "w", encoding="utf-8") as f:
        f.write(render_html(A))
    print(f"\nInforme HTML (ábrelo en el navegador): {a.report}")
    print(f"Detalle por orden (CSV): {a.out}")
    print("static = solo SL/TP originales | follow = además obedece los mensajes del canal (BE, close all...)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="descarga el historial del canal")
    f.add_argument("--config", default="config.ini")
    f.add_argument("--days", type=int, default=90)
    f.add_argument("--since", help="YYYY-MM-DD (UTC); reemplaza --days")
    f.add_argument("--out", default="history.json")

    r = sub.add_parser("run", help="simula las señales")
    r.add_argument("--history", default="history.json")
    r.add_argument("--prices", required=True, help="'mt5' o ruta a CSV de velas M1")
    r.add_argument("--symbol", default="XAUUSD")
    r.add_argument("--server-offset", type=float, default=0.0, help="horas del servidor vs UTC (invierno)")
    r.add_argument("--us-dst", action="store_true", help="+1h en horario de verano de EE. UU.")
    r.add_argument("--lot", type=float, default=0.02, help="lote TOTAL por señal")
    r.add_argument("--spread", type=float, default=0.30, help="spread en USD")
    r.add_argument("--delay", type=int, default=5, help="segundos de retraso hasta ejecutar")
    r.add_argument("--max-hold-hours", type=float, default=24.0)
    r.add_argument("--pending-expiry-min", type=int, default=60)
    r.add_argument("--max-tps", type=int, default=2)
    r.add_argument("--market-tolerance", type=float, default=1.5)
    r.add_argument("--volume-min", type=float, default=0.01)
    r.add_argument("--volume-step", type=float, default=0.01)
    r.add_argument("--report-tz", type=float, default=-6.0,
                   help="zona horaria de tablas y filtros de horario (Honduras = -6)")
    r.add_argument("--trading-hours", default="", help='ej. "08:00-12:00,14:00-18:00" (en --report-tz)')
    r.add_argument("--trading-days", default="", help='ej. "Mon-Fri"')
    r.add_argument("--out", default="backtest_trades.csv")
    r.add_argument("--report", default="report.html", help="informe HTML de salida")
    r.add_argument("--balance", type=float, default=1000.0, help="saldo de referencia para calcular % de retorno y drawdown")
    r.add_argument("--min-hour-signals", type=int, default=3, help="mín. señales por hora/día para proponerlo en el filtro")
    r.add_argument("--no-sweep", action="store_true", help="omite la comparación de variantes y de retraso (más rápido)")

    a = ap.parse_args()
    if a.cmd == "fetch":
        asyncio.run(fetch(a))
    else:
        run(a)


if __name__ == "__main__":
    main()
