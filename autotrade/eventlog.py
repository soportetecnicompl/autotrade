"""Bandeja de señales: registro de cada mensaje y la decisión que se tomó (JSON por línea)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List

MAX_BYTES = 800_000
KEEP_LINES = 1500


class EventLog:
    def __init__(self, path):
        self.path = Path(path)

    def add(self, **rec) -> None:
        rec.setdefault("ts", time.time())
        if isinstance(rec.get("text"), str):
            rec["text"] = rec["text"][:500]
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if self.path.stat().st_size > MAX_BYTES:        # rotación: conservar solo lo último
                lines = self.path.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
                self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass                                              # un fallo de disco no debe afectar al trading

    def tail(self, n: int = 200) -> List[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for l in lines:
            try:
                out.append(json.loads(l))
            except ValueError:
                continue
        return out
