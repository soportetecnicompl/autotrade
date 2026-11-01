"""Pausa del bot (global o por canal), compartida entre el panel, Telegram y el bot.

Fuente única de verdad: control.json. Quien lo cambia (panel o comando de Telegram) lo escribe;
el bot lo lee antes de abrir cada señal.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List


class Control:
    def __init__(self, path):
        self.path = Path(path)

    def read(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            d = {}
        return {"paused": bool(d.get("paused")), "paused_channels": sorted(int(x) for x in d.get("paused_channels", []))}

    def _write(self, d: dict) -> None:
        self.path.write_text(json.dumps(d), encoding="utf-8")

    def is_paused(self, channel_num=None) -> bool:
        d = self.read()
        return d["paused"] or (channel_num is not None and int(channel_num) in d["paused_channels"])

    def set_paused(self, paused: bool, channel_num=None) -> None:
        d = self.read()
        if channel_num is None:
            d["paused"] = paused
            if not paused:
                d["paused_channels"] = []      # «reanudar todo» reanuda también los canales pausados
        else:
            chans = set(d["paused_channels"])
            (chans.add if paused else chans.discard)(int(channel_num))
            d["paused_channels"] = sorted(chans)
        self._write(d)

    def paused_channels(self) -> List[int]:
        return self.read()["paused_channels"]
