"""Rendimiento real por canal, a partir del historial de operaciones de MT5.

El deal de ENTRADA conserva nuestro comentario (tg<canal>.<mensaje>:<n>); el de SALIDA lo reemplaza MT5
por «[sl …]» / «[tp …]», así que el canal de cada salida se averigua por el position_id de la entrada.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional

TAG = re.compile(r"^tg(\d+)\.(\d+)")
DAY = 86400


def _net(d: dict) -> float:
    return float(d.get("profit", 0)) + float(d.get("swap", 0)) + float(d.get("commission", 0)) + float(d.get("fee", 0))


def aggregate(deals: List[dict], now_utc: float, tz_hours: float = 0.0, server_offset: float = 0.0) -> List[dict]:
    """deals: [{position_id, entry:'in'|'out'|'inout', time (hora del servidor), profit, swap, commission,
    fee, comment}] ya filtrados por magic. Devuelve una fila por canal (num None = sin etiqueta conocida)."""
    chan_of: Dict[int, int] = {}
    msgs: Dict[int, set] = defaultdict(set)
    for d in deals:
        if d.get("entry") in ("in", "inout"):
            m = TAG.match(d.get("comment", ""))
            if m:
                chan_of[d["position_id"]] = int(m.group(1))
                msgs[int(m.group(1))].add(int(m.group(2)))
    day_start = now_utc - ((now_utc + tz_hours * 3600) % DAY)           # inicio del día local, en UTC
    per_pos: Dict[int, dict] = {}
    for d in deals:
        if d.get("entry") not in ("out", "inout"):
            continue
        t = d["time"] - server_offset
        p = per_pos.setdefault(d["position_id"], {"net": 0.0, "last": 0.0, "today": 0.0, "d7": 0.0, "d30": 0.0})
        n = _net(d)
        p["net"] += n
        p["last"] = max(p["last"], t)
        if t >= day_start:
            p["today"] += n
        if t >= now_utc - 7 * DAY:
            p["d7"] += n
        if t >= now_utc - 30 * DAY:
            p["d30"] += n
    rows: Dict[Optional[int], dict] = {}
    for pid, p in per_pos.items():
        num = chan_of.get(pid)
        r = rows.setdefault(num, {"num": num, "signals": len(msgs.get(num, ())), "closed": 0, "wins": 0,
                                  "pnl_today": 0.0, "pnl_7d": 0.0, "pnl_30d": 0.0, "pnl_total": 0.0})
        r["closed"] += 1
        r["wins"] += p["net"] > 0
        r["pnl_today"] += p["today"]
        r["pnl_7d"] += p["d7"]
        r["pnl_30d"] += p["d30"]
        r["pnl_total"] += p["net"]
    for num in msgs:                                                    # canales con señales pero sin cierres aún
        rows.setdefault(num, {"num": num, "signals": len(msgs[num]), "closed": 0, "wins": 0, "pnl_today": 0.0,
                              "pnl_7d": 0.0, "pnl_30d": 0.0, "pnl_total": 0.0})
    for r in rows.values():
        r["win_rate"] = r["wins"] / r["closed"] * 100 if r["closed"] else 0.0
    return sorted(rows.values(), key=lambda r: (r["num"] is None, r["num"] or 0))
