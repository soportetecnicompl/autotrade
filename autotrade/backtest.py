"""Simulador de señales sobre velas M1 (puro: sin MT5 ni Telegram).

Reglas (conservadoras):
- Las velas son precios BID; el ASK = bid + spread.
- La señal se ejecuta `delay` segundos después de enviada, con la apertura de
  la primera vela disponible (como el bot en vivo: parse + plan_orders).
- Si en una misma vela se tocan SL y TP, gana el SL.
- En la vela en que se llena una pendiente solo se evalúa el SL (no el TP).
- Con `follow_updates` se aplican los mensajes del canal (BE, close, parcial,
  mover SL, cancelar) igual que en vivo.
"""
from __future__ import annotations

import bisect
import csv
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from .parser import Command, ParseResult, parse
from .planner import OrderPlan, Skip, fill_defaults, plan_orders
from .schedule import TradingSchedule

Bar = Tuple[int, float, float, float, float]  # ts_servidor, o, h, l, c


# ------------------------------------------------------------------ precios
def _nth_sunday(year: int, month: int, n: int) -> datetime:
    d = datetime(year, month, 1, tzinfo=timezone.utc)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def is_us_dst(utc_ts: float) -> bool:
    d = datetime.fromtimestamp(utc_ts, timezone.utc)
    start = _nth_sunday(d.year, 3, 2).replace(hour=7)
    end = _nth_sunday(d.year, 11, 1).replace(hour=6)
    return start <= d < end


def make_offset_fn(hours: float, us_dst: bool) -> Callable[[float], int]:
    """Segundos a sumar a la hora UTC para obtener la hora del servidor del broker."""
    base = int(hours * 3600)
    return (lambda ts: base + (3600 if is_us_dst(ts) else 0)) if us_dst else (lambda ts: base)


class Prices:
    def __init__(self, bars: List[Bar], offset_fn: Callable[[float], int] = lambda ts: 0):
        self.bars = sorted(bars)
        self.ts = [b[0] for b in self.bars]
        self.offset = offset_fn

    def idx(self, utc_ts: float) -> int:
        return bisect.bisect_left(self.ts, int(utc_ts) + self.offset(utc_ts))

    def utc(self, i: int) -> float:
        s = self.ts[min(i, len(self.ts) - 1)]
        return s - self.offset(s)

    def covers(self, utc_ts: float, max_gap: int = 600) -> bool:
        i = self.idx(utc_ts)
        return i < len(self.bars) and self.ts[i] - (int(utc_ts) + self.offset(utc_ts)) <= max_gap


def load_csv(path: str, offset_fn=lambda ts: 0) -> Prices:
    """Acepta `time,open,high,low,close` (ISO o epoch) o el export de MT5
    (`<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE>`, separado por tab)."""
    bars: List[Bar] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        delim = "\t" if "\t" in sample.split("\n")[0] else ","
        for row in csv.reader(f, delimiter=delim):
            if not row or not row[0].strip():
                continue
            try:
                if row[0].startswith("<"):
                    continue
                if len(row) >= 6 and "." in row[0] and ":" in row[1]:     # MT5 export
                    dt = datetime.strptime(row[0] + " " + row[1], "%Y.%m.%d %H:%M:%S" if row[1].count(":") == 2 else "%Y.%m.%d %H:%M")
                    o, h, l, c = map(float, row[2:6])
                else:
                    t = row[0].strip()
                    dt = (datetime.fromtimestamp(float(t), timezone.utc) if t.replace(".", "").isdigit()
                          else datetime.fromisoformat(t))
                    o, h, l, c = map(float, row[1:5])
                bars.append((int(dt.replace(tzinfo=timezone.utc).timestamp()), o, h, l, c))
            except ValueError:
                continue  # cabecera u otra línea no numérica
    if not bars:
        raise ValueError(f"No se leyeron velas de {path}")
    return Prices(bars, offset_fn)


# --------------------------------------------------------------- simulación
@dataclass
class SimConfig:
    lot: float = 0.02
    spread: float = 0.30            # USD
    delay: int = 5                  # segundos
    contract: float = 100.0         # onzas por lote (XAUUSD)
    max_hold_hours: float = 24.0
    pending_expiry_min: int = 60
    be_offset: float = 0.0
    max_tps: int = 2
    market_tolerance: float = 1.5
    max_pending_distance: float = 30.0
    allow_no_tp: bool = False
    volume_min: float = 0.01
    volume_step: float = 0.01
    schedule: Optional[TradingSchedule] = None   # filtro de horario (None = sin filtro)
    # Canales que mandan la dirección primero y el SL después (misma lógica que el bot en vivo)
    entry_then_sl: bool = False
    emergency_sl_pips: float = 100.0
    tp_pips: tuple = ()                          # TP por defecto en pips cuando la señal no trae
    pip: float = 0.10                            # USD por pip
    late_sl_min: int = 30


@dataclass
class Msg:
    id: int
    ts: float                       # UTC epoch
    text: str
    reply_to: Optional[int] = None


@dataclass
class Trade:
    mode: str
    msg_id: int
    time_utc: float
    side: str
    kind: str
    tp_n: int
    lot: float
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    exit_utc: float = 0.0
    exit_price: float = 0.0
    outcome: str = ""               # TP | SL | BE | CLOSED | EXPIRED | CANCELLED | TIMEOUT | OPEN
    pnl: float = 0.0                # USD


def split_lots(total: float, n: int, cfg: SimConfig) -> List[float]:
    import math
    n = max(1, min(n, int(math.floor(total / cfg.volume_min + 1e-9))))
    per = math.floor(total / n / cfg.volume_step + 1e-9) * cfg.volume_step
    vols = [round(per, 8)] * n
    vols[0] = round(vols[0] + math.floor((total - per * n) / cfg.volume_step + 1e-9) * cfg.volume_step, 8)
    return vols


def _sim_order(plan: OrderPlan, lot: float, i0: int, i_end: int, P: Prices,
               cmds: List[Tuple[int, Command]], cfg: SimConfig, tr: Trade) -> None:
    sp, buy = cfg.spread, plan.side == "BUY"
    sgn = 1 if buy else -1
    bars = P.bars
    filled = plan.kind == "market"
    entry = plan.price
    sl, tp = plan.sl, plan.tp
    remaining = 1.0
    expiry = bars[i0][0] + cfg.pending_expiry_min * 60 if cfg.pending_expiry_min > 0 else None
    q = list(cmds)

    def realize(exit_price: float, frac: float) -> None:
        nonlocal remaining
        tr.pnl += sgn * (exit_price - entry) * lot * frac * cfg.contract
        remaining -= frac

    def finish(i: int, price: float, outcome: str) -> None:
        tr.exit_utc, tr.exit_price, tr.outcome = P.utc(i), price, outcome

    for i in range(i0, i_end):
        ts, o, h, l, c = bars[i]
        px_close = o if buy else o + sp          # precio al que se cierra a mercado
        px_open_enter = o + sp if buy else o     # precio al que se abriría a mercado
        while q and q[0][0] <= i:
            _, cmd = q.pop(0)
            if cmd.kind == "cancel" and not filled:
                finish(i, 0.0, "CANCELLED")
                return
            if not filled:
                continue
            if cmd.kind == "close":
                realize(px_close, remaining)
                finish(i, px_close, "CLOSED")
                return
            if cmd.kind == "partial" and cmd.value:
                realize(px_close, remaining * cmd.value)
            elif cmd.kind == "move_sl" and cmd.value:
                sl = cmd.value
            elif cmd.kind == "set_sl" and cmd.value:              # SL que manda el canal después de la entrada
                new = (entry - sgn * cmd.value * cfg.pip) if cmd.pips else cmd.value
                if (buy and px_close <= new) or (not buy and px_close >= new):
                    realize(px_close, remaining)                  # el precio ya superó ese SL: se cierra al instante
                    finish(i, px_close, "SL")
                    return
                sl = new
            elif cmd.kind == "breakeven":
                new_sl = entry + sgn * cfg.be_offset
                if (buy and o > new_sl) or (not buy and o + sp < new_sl):
                    sl = new_sl

        just_filled = False
        if not filled:
            if expiry is not None and ts >= expiry:
                finish(i, 0.0, "EXPIRED")
                return
            p = plan.price
            if plan.kind == "limit":
                hit = (l + sp <= p) if buy else (h >= p)
                fill = p
            else:  # stop
                hit = (h + sp >= p) if buy else (l <= p)
                fill = max(p, o + sp) if buy else min(p, o)
            if not hit:
                continue
            filled, just_filled, entry = True, True, fill
            tr.entry = entry

        # SL primero (conservador); en la vela de llenado no se evalúa TP
        if buy:
            if l <= sl:
                px = min(sl, o)
                realize(px, remaining)
                finish(i, px, "BE" if sl >= entry - 1e-9 else "SL")
                return
            if tp and not just_filled and h >= tp:
                realize(tp, remaining)
                finish(i, tp, "TP")
                return
        else:
            if h + sp >= sl:
                px = max(sl, o + sp)
                realize(px, remaining)
                finish(i, px, "BE" if sl <= entry + 1e-9 else "SL")
                return
            if tp and not just_filled and l + sp <= tp:
                realize(tp, remaining)
                finish(i, tp, "TP")
                return

    if not filled:
        finish(min(i_end, len(bars) - 1), 0.0, "EXPIRED")
        return
    last = min(i_end, len(bars) - 1)
    ts, o, h, l, c = bars[last]
    px = o if buy else o + sp
    realize(px, remaining)
    finish(last, px, "TIMEOUT" if i_end < len(bars) else "OPEN")


def simulate(msgs: List[Msg], P: Prices, cfg: SimConfig, follow_updates: bool,
             skipped: Optional[Counter] = None) -> List[Trade]:
    mode = "follow" if follow_updates else "static"
    parsed = [(m, parse(m.text, entry_then_sl=cfg.entry_then_sl)) for m in sorted(msgs, key=lambda m: m.ts)]
    all_cmds = [(m, c) for m, r in parsed for c in r.commands]
    # SL tardío de cada señal: el mensaje que responde a la entrada o, si no, la entrada más reciente
    # del canal dentro de la ventana. Forma parte de la señal, así que se aplica también en modo «static».
    late: Dict[int, List[Tuple[Msg, Command]]] = {}
    if cfg.entry_then_sl:
        entries = [m for m, r in parsed if r.signal]
        by_id = {m.id: m for m in entries}
        for cm, c in all_cmds:
            if c.kind != "set_sl":
                continue
            target = by_id.get(cm.reply_to) if cm.reply_to is not None else None
            if target is None:
                target = next((e for e in reversed(entries) if e.ts < cm.ts), None)
            if target is not None and 0 < cm.ts - target.ts <= cfg.late_sl_min * 60:
                late.setdefault(target.id, []).append((cm, c))
    trades: List[Trade] = []

    for m, r in parsed:
        sig = r.signal
        if not sig:
            continue
        if cfg.schedule is not None and not cfg.schedule.allows(m.ts):
            if skipped is not None:
                skipped["fuera de horario"] += 1
            continue
        if not P.covers(m.ts + cfg.delay):
            if skipped is not None:
                skipped["sin datos de precio"] += 1
            continue
        i0 = P.idx(m.ts + cfg.delay)
        o = P.bars[i0][1]
        sig = fill_defaults(sig, o, o + cfg.spread, cfg.emergency_sl_pips if cfg.entry_then_sl else 0.0,
                            cfg.tp_pips, cfg.pip)
        try:
            plans = plan_orders(sig, o, o + cfg.spread, market_tolerance=cfg.market_tolerance,
                                max_pending_distance=cfg.max_pending_distance,
                                max_tps=cfg.max_tps, allow_no_tp=cfg.allow_no_tp)
        except Skip as e:
            if skipped is not None:
                skipped[str(e).split(" (")[0][:60]] += 1
            continue
        lots = split_lots(cfg.lot, len(plans), cfg)
        plans = plans[:len(lots)]
        i_end = P.idx(m.ts + cfg.max_hold_hours * 3600)
        cmds = [(P.idx(cm.ts + cfg.delay), c) for cm, c in late.get(m.id, [])]
        if follow_updates:
            for cm, c in all_cmds:
                if c.kind == "set_sl" or cm.ts <= m.ts or cm.ts > m.ts + cfg.max_hold_hours * 3600:
                    continue
                if cm.reply_to is not None and cm.reply_to != m.id:
                    continue  # respuesta a OTRA señal
                cmds.append((P.idx(cm.ts + cfg.delay), c))
        cmds.sort(key=lambda x: x[0])
        for n, (p, lot) in enumerate(zip(plans, lots), 1):
            tr = Trade(mode, m.id, m.ts, p.side, p.kind, n, lot, entry=p.price, sl=p.sl, tp=p.tp)
            _sim_order(p, lot, i0, min(i_end, len(P.bars)), P, cmds, cfg, tr)
            trades.append(tr)
    return trades


# ------------------------------------------------------------- estadísticas
def _stats(trades: List[Trade]) -> dict:
    by_sig: Dict[int, List[Trade]] = defaultdict(list)
    for t in trades:
        by_sig[t.msg_id].append(t)
    sigs = []
    for mid, ts in by_sig.items():
        filled = [t for t in ts if t.outcome not in ("EXPIRED", "CANCELLED")]
        if not filled:
            sigs.append((ts[0].time_utc, mid, None, ts))
            continue
        sigs.append((ts[0].time_utc, mid, sum(t.pnl for t in filled), ts))
    sigs.sort()
    traded = [s for s in sigs if s[2] is not None]
    pnls = [s[2] for s in traded]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    # drawdown y racha perdedora
    eq = peak = dd = 0.0
    streak = worst = 0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if p < 0 else 0
        worst = max(worst, streak)
    tp1 = [t for t in trades if t.tp_n == 1 and t.outcome not in ("EXPIRED", "CANCELLED")]
    return {
        "signals": len(sigs), "traded": len(traded), "not_filled": len(sigs) - len(traded),
        "wins": len(wins), "losses": len(losses), "breakeven": len(pnls) - len(wins) - len(losses),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else 0.0,
        "pnl": sum(pnls), "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean(losses) if losses else 0.0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses else float("inf") if wins else 0.0,
        "expectancy": statistics.mean(pnls) if pnls else 0.0,
        "max_dd": dd, "worst_streak": worst,
        "tp1_rate": sum(t.outcome == "TP" for t in tp1) / len(tp1) * 100 if tp1 else 0.0,
        "outcomes": Counter(t.outcome for t in trades), "sigs": traded,
    }


def _bucket_table(traded, key: Callable[[float], str], tz_h: float) -> List[str]:
    b: Dict[str, List[float]] = defaultdict(list)
    for ts, _, pnl, _ in traded:
        b[key(ts + tz_h * 3600)].append(pnl)
    rows = []
    for k in sorted(b):
        v = b[k]
        rows.append(f"  {k:<10}{len(v):>6}{sum(x > 0 for x in v) / len(v) * 100:>9.0f}%{sum(v):>12.2f}")
    return rows


def report(trades_by_mode: Dict[str, List[Trade]], skipped: Counter, cfg: SimConfig,
           tz_h: float = -6.0) -> str:
    S = {m: _stats(t) for m, t in trades_by_mode.items()}
    out = [f"Lote total por señal: {cfg.lot}  |  spread {cfg.spread}  |  retraso {cfg.delay}s  |  máx. tenencia {cfg.max_hold_hours}h", ""]
    modes = list(S)
    out.append(f"{'':<34}" + "".join(f"{m:>14}" for m in modes))

    def row(label, fmt, k):
        out.append(f"{label:<34}" + "".join(fmt.format(S[m][k]) for m in modes))

    row("Señales simuladas", "{:>14d}", "signals")
    row("  operadas (se llenaron)", "{:>14d}", "traded")
    row("  no llenadas (pendiente expiró)", "{:>14d}", "not_filled")
    row("Ganadoras", "{:>14d}", "wins")
    row("Perdedoras", "{:>14d}", "losses")
    row("Break-even", "{:>14d}", "breakeven")
    row("% acierto (por señal)", "{:>13.1f}%", "win_rate")
    row("% TP1 alcanzado (por orden)", "{:>13.1f}%", "tp1_rate")
    row("Resultado total (USD)", "{:>14.2f}", "pnl")
    row("Ganancia media (USD)", "{:>14.2f}", "avg_win")
    row("Pérdida media (USD)", "{:>14.2f}", "avg_loss")
    row("Factor de beneficio", "{:>14.2f}", "profit_factor")
    row("Expectativa por señal (USD)", "{:>14.2f}", "expectancy")
    row("Drawdown máximo (USD)", "{:>14.2f}", "max_dd")
    row("Peor racha de pérdidas", "{:>14d}", "worst_streak")
    out.append("")
    for m in modes:
        out.append(f"Resultados por orden [{m}]: " + ", ".join(f"{k}={v}" for k, v in sorted(S[m]["outcomes"].items())))
    if skipped:
        out += ["", "Señales descartadas (misma lógica que en vivo):"]
        out += [f"  {v:>4}  {k}" for k, v in skipped.most_common()]
    ref = modes[-1]
    traded = S[ref]["sigs"]
    if traded:
        out += ["", f"Por hora del día (UTC{tz_h:+g}) [{ref}]", f"  {'hora':<10}{'señales':>6}{'acierto':>10}{'USD':>12}"]
        out += _bucket_table(traded, lambda t: f"{datetime.fromtimestamp(t, timezone.utc).hour:02d}h", tz_h)
        names = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
        out += ["", f"Por día de la semana [{ref}]", f"  {'día':<10}{'señales':>6}{'acierto':>10}{'USD':>12}"]
        out += _bucket_table(traded, lambda t: f"{datetime.fromtimestamp(t, timezone.utc).weekday()}-{names[datetime.fromtimestamp(t, timezone.utc).weekday()]}", tz_h)
    return "\n".join(out)


def write_trades_csv(path: str, trades: List[Trade]) -> None:
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mode", "msg_id", "time_utc", "side", "kind", "tp_n", "lot", "entry", "sl", "tp",
                    "exit_utc", "exit_price", "outcome", "pnl_usd"])
        for t in trades:
            w.writerow([t.mode, t.msg_id, iso(t.time_utc), t.side, t.kind, t.tp_n, t.lot,
                        round(t.entry, 2), t.sl, t.tp, iso(t.exit_utc), round(t.exit_price, 2),
                        t.outcome, round(t.pnl, 2)])
