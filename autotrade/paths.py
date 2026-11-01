"""Rutas y lanzamiento de procesos (modo script y modo .exe empaquetado)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List

APP_NAME = "AutoTrade"
IS_WINDOWS = sys.platform == "win32"
FROZEN = bool(getattr(sys, "frozen", False))


def data_dir() -> Path:
    """Carpeta de datos del usuario (config.ini, sesión, logs, informes)."""
    env = os.environ.get("AUTOTRADE_HOME")
    if env:
        base = Path(env)
    elif IS_WINDOWS:
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    else:
        base = Path.home() / ".autotrade"
    base.mkdir(parents=True, exist_ok=True)
    return base


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def launcher_cmd(args: List[str]) -> List[str]:
    """Comando para relanzar este mismo programa con otros argumentos."""
    if FROZEN:
        return [sys.executable, *args]
    return [sys.executable, str(repo_root() / "app.py"), *args]


def popen_kwargs(detached: bool = False) -> dict:
    """Opciones de subprocess: sin ventana de consola en Windows; sesión propia en POSIX."""
    kw: dict = {}
    if IS_WINDOWS:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if detached:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kw["creationflags"] = flags
    elif detached:
        kw["start_new_session"] = True
    return kw


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                                 text=True, timeout=10, **popen_kwargs()).stdout
            return str(pid) in out
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # En POSIX un proceso zombi sigue "vivo" para kill(0): comprobar su estado
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
        return stat != "Z"
    except OSError:
        return True


def stop_tree(pid: int) -> None:
    """Detiene un proceso y todos sus hijos (el supervisor y el bot)."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       timeout=15, **popen_kwargs())
        return
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
