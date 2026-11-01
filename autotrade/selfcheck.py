"""Autoverificación: ¿las librerías necesarias quedaron realmente dentro del programa?

`AutoTrade.exe --selfcheck` la ejecuta (el build de Windows la usa para fallar si algo
falta) y el asistente muestra el motivo exacto cuando un componente no carga.
"""
from __future__ import annotations

import sys
from typing import List, Tuple

Result = Tuple[str, bool, str, bool]      # (módulo, ok, detalle, obligatorio)


def _try(fn) -> Tuple[bool, str]:
    try:
        mod = fn()
        return True, str(getattr(mod, "__version__", "ok"))
    except Exception as e:  # noqa: BLE001 - queremos el motivo exacto, sea cual sea
        return False, f"{type(e).__name__}: {e}"


def check_imports(windows: bool = sys.platform == "win32") -> List[Result]:
    from .mt5_import import import_mt5

    def numpy_():
        import numpy
        return numpy

    def telethon_():
        import telethon
        return telethon

    def tkinter_():
        import tkinter
        return tkinter

    checks = [("telethon", telethon_, True), ("tkinter", tkinter_, True),
              ("numpy", numpy_, windows), ("MetaTrader5", import_mt5, windows)]
    out = []
    for name, fn, required in checks:
        ok, detail = _try(fn)
        out.append((name, ok, detail, required))
    return out


def format_report(results: List[Result]) -> str:
    lines = []
    for name, ok, detail, required in results:
        mark = "OK " if ok else ("FALLO" if required else "n/a  ")
        lines.append(f"{mark:<6}{name:<12}{detail}")
    return "\n".join(lines)


def failed_required(results: List[Result]) -> List[Result]:
    return [r for r in results if r[3] and not r[1]]
