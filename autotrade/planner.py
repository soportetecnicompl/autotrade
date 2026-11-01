"""Decide qué órdenes abrir a partir de una señal y el precio actual.

Función pura (sin MT5) para poder probarla en cualquier sistema.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .parser import Signal


class Skip(Exception):
    """La señal no se opera (motivo en el mensaje)."""


@dataclass
class OrderPlan:
    side: str            # BUY | SELL
    kind: str            # market | limit | stop
    price: float         # precio de referencia (en market, el actual)
    sl: float
    tp: float            # 0.0 = sin TP


def plan_orders(
    sig: Signal,
    bid: float,
    ask: float,
    *,
    market_tolerance: float = 1.5,
    max_pending_distance: float = 30.0,
    max_tps: int = 3,
    allow_no_tp: bool = False,
) -> List[OrderPlan]:
    buy = sig.side == "BUY"
    cur = ask if buy else bid

    if sig.sl is None:
        raise Skip("la señal no trae SL")

    lo, hi = sig.entry_low, sig.entry_high
    kind: str
    target: Optional[float] = None

    if lo is None:
        kind = "market"
    else:
        hi = hi if hi is not None else lo
        target = lo if cur < lo else hi if cur > hi else cur
        in_zone = lo - market_tolerance <= cur <= hi + market_tolerance
        # ¿en qué lado del precio actual quedaría la orden pendiente?
        pending_kind = "limit" if (target < cur) == buy else "stop"

        if sig.kind is None:
            kind = "market" if in_zone else pending_kind
        elif in_zone:
            kind = "market"
        elif sig.kind != pending_kind:
            raise Skip(
                f"{sig.side} {sig.kind.upper()} en {target} queda del lado "
                f"equivocado del precio actual ({cur})"
            )
        else:
            kind = sig.kind

        if kind != "market" and abs(target - cur) > max_pending_distance:
            raise Skip(f"entrada {target} muy lejos del precio {cur}")

    ref = cur if kind == "market" else target
    assert ref is not None

    if buy and not sig.sl < ref:
        raise Skip(f"SL {sig.sl} inválido para BUY en {ref}")
    if not buy and not sig.sl > ref:
        raise Skip(f"SL {sig.sl} inválido para SELL en {ref}")

    tps = sorted({tp for tp in sig.tps if (tp > ref if buy else tp < ref)}, reverse=not buy)
    if sig.tps and not tps:
        raise Skip("ningún TP está del lado correcto de la entrada")
    tps = tps[:max_tps]
    if not tps:
        if not allow_no_tp:
            raise Skip("la señal no trae TP (allow_no_tp=false)")
        tps = [0.0]

    return [OrderPlan(sig.side, kind, ref, sig.sl, tp) for tp in tps]


def apply_stops_level(
    plans: List[OrderPlan],
    bid: float,
    ask: float,
    min_dist: float,
    policy: str = "skip",
    point: float = 0.01,
) -> "tuple[List[OrderPlan], List[str]]":
    """Revisa la distancia mínima (stops level) que exige el broker.

    Se mide como lo hace MT5: en mercado, SL y TP contra el precio de CIERRE
    (BUY: bid, SELL: ask); en pendientes, contra el precio de la orden, y la
    orden misma debe quedar a >= min_dist del precio actual.
    policy="skip": lanza Skip si algo no cumple. policy="adjust": aleja SL/TP
    hasta el mínimo (las pendientes demasiado cerca siempre se omiten).
    Devuelve (planes, avisos).
    """
    if min_dist <= 0:
        return plans, []
    out, notes = [], []
    margin = min_dist + point
    for p in plans:
        buy = p.side == "BUY"
        if p.kind == "market":
            ref = bid if buy else ask
        else:
            cur = ask if buy else bid
            if abs(p.price - cur) < min_dist:
                raise Skip(f"orden pendiente en {p.price} demasiado cerca del precio {cur} "
                           f"(distancia mínima del broker: {min_dist:.2f})")
            ref = p.price
        sl_d = (ref - p.sl) if buy else (p.sl - ref)
        tp_d = ((p.tp - ref) if buy else (ref - p.tp)) if p.tp else None
        sl, tp = p.sl, p.tp
        if sl_d < min_dist:
            if policy != "adjust":
                raise Skip(f"SL {p.sl} demasiado cerca ({sl_d:.2f} < mínimo del broker {min_dist:.2f})")
            sl = ref - margin if buy else ref + margin
            notes.append(f"SL {p.sl} -> {sl:.2f} (mínimo del broker {min_dist:.2f})")
        if tp_d is not None and tp_d < min_dist:
            if policy != "adjust":
                raise Skip(f"TP {p.tp} demasiado cerca ({tp_d:.2f} < mínimo del broker {min_dist:.2f})")
            tp = ref + margin if buy else ref - margin
            notes.append(f"TP {p.tp} -> {tp:.2f} (mínimo del broker {min_dist:.2f})")
        out.append(OrderPlan(p.side, p.kind, p.price, sl, tp))
    return out, notes
