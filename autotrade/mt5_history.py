"""Descarga de velas M1 desde MetaTrader 5 de forma robusta.

MT5 sincroniza el historial en segundo plano: la primera petición suele devolver vacío mientras el
terminal lo descarga, y una petición enorme (meses de M1) puede superar «Máx. de barras en el gráfico».
Por eso se pide por tramos (del más nuevo al más antiguo), con reintentos y esperas, y se acepta
historial PARCIAL (se avisa desde qué fecha hay datos) en vez de fallar del todo.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Tuple

Bar = Tuple[int, float, float, float, float]
WAIT_SECONDS = 3.0           # espera entre reintentos mientras MT5 descarga el historial
BARS_PER_DAY = 1000          # el oro opera ~23 h × 5 días/semana ≈ 1000 velas M1 por día natural


def to_bars(rates) -> List[Bar]:
    return [(int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])) for r in rates]


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def fetch_m1(mt5, symbol: str, s0: int, s1: int, chunk_days: int = 10, retries: int = 4, wait=None,
             sleep=None, log: Callable[[str], None] = print) -> List[Bar]:
    """Velas M1 de [s0, s1) (segundos en hora del servidor), ordenadas y sin duplicados.
    Devuelve lo que haya, aunque sea parcial; lista vacía si MT5 no entrega nada."""
    sleep = sleep or time.sleep
    wait = WAIT_SECONDS if wait is None else wait
    chunk = chunk_days * 86400
    total = max(1, math.ceil((s1 - s0) / chunk))
    bars: Dict[int, Bar] = {}
    end, i, empty_run = s1, 0, 0
    while end > s0:
        start = max(s0, end - chunk)
        i += 1
        log(f"Descargando velas M1 de MT5: tramo {i}/{total} ({_fmt(start)} a {_fmt(end)})…")
        rates = None
        for attempt in range(retries):
            rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, int(start), int(end))
            if rates is not None and len(rates) > 0:
                break
            if attempt < retries - 1:
                log(f"  MT5 aún no entrega este tramo (intento {attempt + 1}/{retries}); esperando que lo descargue…")
                sleep(wait)
        if rates is not None and len(rates) > 0:
            for b in to_bars(rates):
                bars[b[0]] = b
            empty_run = 0
        else:
            empty_run += 1
            if empty_run >= 2:
                log("  MT5 no tiene más historial M1 anterior a esta fecha.")
                break
        end = start
    return [bars[k] for k in sorted(bars)]


def advice(symbol: str, last_error, days: int) -> str:
    return (f"MT5 no devolvió velas M1 de «{symbol}» (último mensaje de MT5: {last_error}).\n"
            "Qué hacer, en este orden:\n"
            f"  1) En MT5 abre el gráfico {symbol} en M1 y pulsa la tecla «Inicio» (Home) varias veces para que descargue historial.\n"
            "  2) Herramientas → Opciones → Gráficos → «Máx. de barras en el gráfico» = Ilimitado, y reinicia MT5.\n"
            f"  3) Vuelve a intentarlo con menos días (prueba 30; ahora pides {days}).\n"
            f"  4) Comprueba que «{symbol}» es el nombre exacto del símbolo en tu broker.")


def maxbars_warning(mt5, days: float) -> str:
    """Aviso si «Máx. de barras en el gráfico» no alcanza para el periodo pedido; '' si todo bien o no se sabe."""
    try:
        maxbars = int(getattr(mt5.terminal_info(), "maxbars", 0) or 0)
    except Exception:  # noqa: BLE001
        return ""
    need = int(days * BARS_PER_DAY)
    if maxbars and maxbars < need:
        return (f"⚠ Tu MT5 limita los gráficos a {maxbars:,} barras y {days:.0f} días de M1 necesitan unas {need:,}. "
                "Sube «Máx. de barras en el gráfico» (Herramientas → Opciones → Gráficos) o pide menos días.")
    return ""
