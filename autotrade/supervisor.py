"""Supervisor: ejecuta el bot y lo reinicia si se cae (equivale a scripts/run_bot.bat).

Salida con código 0 = parada intencional (no se reinicia); cualquier otro código
(error, desconexión) se reinicia a los `delay` segundos.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .paths import data_dir, launcher_cmd, popen_kwargs

log = logging.getLogger("supervisor")
PID_FILE = "bot.pid"


def supervise(run_once: Callable[[int], int], delay: float = 15.0,
              sleep: Callable[[float], None] = time.sleep,
              max_restarts: Optional[int] = None) -> int:
    restarts = 0
    while True:
        code = run_once(restarts)
        if code == 0:
            log.info("Bot detenido manualmente (código 0): no se reinicia.")
            return 0
        log.warning("El bot terminó con código %s. Reinicio en %ss.", code, delay)
        if max_restarts is not None and restarts >= max_restarts:
            return code
        sleep(delay)
        restarts += 1


def _run_child(restarts: int) -> int:
    env = {**os.environ, "AUTOTRADE_RESTARTS": str(restarts)}
    return subprocess.call(launcher_cmd(["--bot-once"]), env=env, cwd=str(data_dir()),
                           **popen_kwargs())


def run_supervisor() -> int:
    pid_file = data_dir() / PID_FILE
    pid_file.write_text(str(os.getpid()))
    try:
        return supervise(_run_child)
    finally:
        try:
            pid_file.unlink()
        except OSError:
            pass


def read_pid(directory: Optional[Path] = None) -> int:
    try:
        return int((Path(directory or data_dir()) / PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return 0
