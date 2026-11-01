"""Comprobaciones de MetaTrader 5 para el asistente (solo Windows con MT5 instalado)."""
from __future__ import annotations

from typing import List, Optional


def probe(path: str = "", login: str = "", password: str = "", server: str = "",
          symbol: str = "") -> dict:
    """Conecta a MT5, lee la cuenta, busca símbolos de oro y cierra la conexión.

    Devuelve {ok, error, account{...}, gold_symbols[...], symbol_ok, tick_time}.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"ok": False, "error": "La librería de MetaTrader 5 solo funciona en Windows "
                                      "(y con MetaTrader 5 instalado)."}
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
