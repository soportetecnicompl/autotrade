"""Copia de seguridad de la configuración (zip) y restauración."""
from __future__ import annotations

import shutil
import time
import zipfile
from pathlib import Path
from typing import List

ALWAYS = ("config.ini",)
OPTIONAL_STATE = ("control.json", "risk_state.json")


def export_zip(src_dir, dest, include_session: bool = False) -> List[str]:
    """Guarda config.ini (y el estado). La sesión de Telegram da acceso a tu cuenta: solo si lo pides."""
    src = Path(src_dir)
    names = [n for n in ALWAYS + OPTIONAL_STATE if (src / n).exists()]
    if include_session:
        names += sorted(p.name for p in src.glob("*.session"))
    if "config.ini" not in names:
        raise FileNotFoundError("No hay config.ini que guardar todavía.")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.write(src / n, n)
    return names


def _allowed(name: str) -> bool:
    return (name in ALWAYS + OPTIONAL_STATE) or (name.endswith(".session") and "/" not in name and "\\" not in name
                                                 and not name.startswith("."))


def import_zip(zip_path, dest_dir) -> List[str]:
    """Restaura desde un zip creado con export_zip. Solo acepta nombres conocidos (sin rutas) y
    guarda copia del config.ini actual."""
    dest = Path(dest_dir)
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        bad = [n for n in names if not _allowed(n)]
        if bad:
            raise ValueError("El archivo contiene elementos no válidos: " + ", ".join(bad[:3]))
        if "config.ini" not in names:
            raise ValueError("Este zip no parece una copia de AutoTrade (falta config.ini).")
        cur = dest / "config.ini"
        if cur.exists():
            shutil.copy2(cur, dest / f"config.ini.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        for n in names:
            (dest / n).write_bytes(z.read(n))
    return names
