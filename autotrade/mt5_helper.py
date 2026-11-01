"""Comprobaciones de MetaTrader 5 para el asistente (solo Windows con MT5 instalado)."""
from __future__ import annotations

from typing import List, Optional


def fetch_deals(path: str = "", login: str = "", password: str = "", server: str = "", symbol: str = "",
                magic: int = 0, days: int = 90) -> dict:
    """Operaciones cerradas del bot (por magic) de los últimos `days` días, para el rendimiento por canal.
    Devuelve {ok, error, deals:[{position_id, entry, time, profit, swap, commission, fee, comment}], server_offset}."""
    import sys
    import time

    from .mt5_import import import_mt5
    from .setup_logic import estimate_server_offset
    try:
        mt5 = import_mt5()
    except ImportError as e:
        return {"ok": False, "error": ("La librería de MetaTrader 5 solo funciona en Windows." if sys.platform != "win32"
                                       else f"No se pudo cargar la librería de MetaTrader 5 ({type(e).__name__}: {e}).")}
    kw = {}
    if path:
        kw["path"] = path
    if login:
        kw.update(login=int(login), password=password, server=server)
    if not mt5.initialize(**kw):
        code, msg = mt5.last_error()
        return {"ok": False, "error": f"No se pudo conectar a MT5 ({code}: {msg}). Abre MetaTrader 5 e inicia sesión."}
    try:
        if symbol:
            mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol) if symbol else None
        srv_now = float(tick.time) if tick is not None else time.time()
        offset = estimate_server_offset(srv_now, time.time()) if tick is not None else 0
        raw = mt5.history_deals_get(int(srv_now - days * 86400), int(srv_now + 3600))
        if raw is None:
            return {"ok": False, "error": f"MT5 no devolvió historial ({mt5.last_error()})."}
        kinds = {mt5.DEAL_ENTRY_IN: "in", mt5.DEAL_ENTRY_OUT: "out", mt5.DEAL_ENTRY_INOUT: "inout",
                 getattr(mt5, "DEAL_ENTRY_OUT_BY", 3): "out"}
        deals = [{"position_id": d.position_id, "entry": kinds.get(d.entry, "other"), "time": d.time,
                  "profit": d.profit, "swap": d.swap, "commission": d.commission,
                  "fee": getattr(d, "fee", 0.0), "comment": d.comment}
                 for d in raw if (not magic or d.magic == magic)]
        return {"ok": True, "error": "", "deals": deals, "server_offset": (offset or 0) * 3600}
    finally:
        mt5.shutdown()


def probe(path: str = "", login: str = "", password: str = "", server: str = "",
          symbol: str = "") -> dict:
    """Conecta a MT5, lee la cuenta, busca símbolos de oro y cierra la conexión.

    Devuelve {ok, error, account{...}, gold_symbols[...], symbol_ok, tick_time}.
    """
    import sys

    from .mt5_import import import_mt5
    try:
        mt5 = import_mt5()
    except ImportError as e:
        if sys.platform != "win32":
            return {"ok": False, "error": "La librería de MetaTrader 5 solo funciona en Windows "
                                          "(y con MetaTrader 5 instalado)."}
        return {"ok": False, "error": (
            f"No se pudo cargar la librería de MetaTrader 5 dentro de AutoTrade ({type(e).__name__}: {e}). "
            "Es un fallo de la instalación del programa, no de tu cuenta ni de MT5: instala la versión "
            "más reciente de AutoTrade o envía este texto completo para que lo corrijan.")}
    kw = {}
    if path:
        kw["path"] = path
    if login:
        kw.update(login=int(login), password=password, server=server)
    if not mt5.initialize(**kw):
        code, msg = mt5.last_error()
        hint = ""
        if code == -6 or "authorization" in str(msg).lower():
            hint = " Revisa número de cuenta, contraseña y servidor."
        elif code in (-10003, -10004, -10005):
            hint = " Abre MetaTrader 5 una vez, inicia sesión en tu cuenta y vuelve a intentar."
        return {"ok": False, "error": f"No se pudo conectar a MT5 ({code}: {msg}).{hint}"}
    try:
        acc = mt5.account_info()
        if acc is None:
            return {"ok": False, "error": "MT5 abrió pero no hay una cuenta iniciada. Inicia sesión en MT5."}
        term = mt5.terminal_info()
        syms = mt5.symbols_get() or []
        gold = sorted(s.name for s in syms if any(k in s.name.upper() for k in ("XAU", "GOLD")))
        out = {
            "ok": True, "error": "",
            "account": {"login": acc.login, "server": acc.server, "balance": acc.balance,
                        "currency": acc.currency,
                        "mode": "DEMO" if acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "REAL",
                        "algo_trading": bool(term and term.trade_allowed)},
            "gold_symbols": gold, "symbol_ok": False, "tick_time": None, "bid": None, "ask": None,
        }
        if symbol and mt5.symbol_info(symbol) is not None:
            mt5.symbol_select(symbol, True)
            out["symbol_ok"] = True
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None:
                out.update(tick_time=tick.time, bid=tick.bid, ask=tick.ask)
        return out
    finally:
        mt5.shutdown()
