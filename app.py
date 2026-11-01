"""AutoTrade: punto de entrada único.

  AutoTrade.exe                  asistente de instalación y panel de control (interfaz gráfica)
  AutoTrade.exe --bot            bot con supervisor (se reinicia solo si se cae)
  AutoTrade.exe --bot-once       bot una sola vez (lo usa el supervisor)
  AutoTrade.exe --backtest ...   backtester (mismos argumentos que backtest.py)
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    from autotrade.paths import data_dir

    args = sys.argv[1:]
    out = os.environ.get("AUTOTRADE_OUTFILE")
    if out:                               # el asistente lee este archivo en vez de una tubería
        sys.stdout = sys.stderr = open(out, "a", encoding="utf-8", buffering=1)
    for name in ("stdout", "stderr"):     # .exe de ventanas: std* puede ser None
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    # Todo se ejecuta desde la carpeta de datos del usuario (config.ini, sesión, logs)
    os.chdir(data_dir())

    if args[:1] == ["--bot"]:
        from autotrade.supervisor import run_supervisor
        return run_supervisor()
    if args[:1] == ["--bot-once"]:
        sys.argv = ["main.py", *args[1:]]
        import main as bot_main
        bot_main.main()
        return 0
    if args[:1] == ["--backtest"]:
        sys.argv = ["backtest.py", *args[1:]]
        import backtest
        backtest.main()
        return 0

    from autotrade.gui import run_gui
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
