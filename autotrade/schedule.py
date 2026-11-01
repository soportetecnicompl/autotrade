"""Filtro de horario para operar señales nuevas (puro, sin MT5 ni Telegram).

- days:  "Mon-Fri", "Mon,Wed,Fri", "Lun-Vie" ... (vacío = todos los días)
- hours: "08:00-12:00, 14:00-18:00" (vacío = todo el día). Admite cruzar la
         medianoche ("22:00-02:00") y "24:00" como fin de día.
- tz_offset_hours: zona horaria fija en la que están escritos días y horas
         (Honduras = -6). El día se evalúa en esa zona al momento del mensaje.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Set, Tuple

_DAYS = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
    "LUN": 0, "MAR": 1, "MIE": 2, "MIÉ": 2, "JUE": 3, "VIE": 4, "SAB": 5, "SÁB": 5, "DOM": 6,
}


def _day(token: str) -> int:
    k = token.strip().upper()[:3]
    if k not in _DAYS:
        raise ValueError(f"Día no reconocido: '{token}' (usa Mon, Tue, Wed, Thu, Fri, Sat, Sun)")
    return _DAYS[k]


def parse_days(text: str) -> Set[int]:
    text = (text or "").strip()
    if not text:
        return set(range(7))
    out: Set[int] = set()
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if "-" in part:
            a, b = (_day(x) for x in part.split("-", 1))
            d = a
            out.add(d)
            while d != b:                      # permite rangos como Fri-Mon
                d = (d + 1) % 7
                out.add(d)
        else:
            out.add(_day(part))
    return out


def _minutes(token: str) -> int:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", token)
    if not m:
        raise ValueError(f"Hora no válida: '{token}' (formato HH:MM)")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= mi < 60 and 0 <= h <= 24 and (h < 24 or mi == 0)):
        raise ValueError(f"Hora fuera de rango: '{token}'")
    return h * 60 + mi


def parse_hours(text: str) -> List[Tuple[int, int]]:
    text = (text or "").strip()
    if not text:
        return [(0, 1440)]
    wins = []
    for part in text.split(","):
        if not part.strip():
            continue
        if "-" not in part:
            raise ValueError(f"Rango horario inválido: '{part}' (usa HH:MM-HH:MM)")
        a, b = part.split("-", 1)
        wins.append((_minutes(a), _minutes(b)))
    return wins


@dataclass
class TradingSchedule:
    days: Set[int] = field(default_factory=lambda: set(range(7)))
    windows: List[Tuple[int, int]] = field(default_factory=lambda: [(0, 1440)])
    tz_offset_hours: float = 0.0

    @classmethod
    def from_text(cls, days: str = "", hours: str = "", tz_offset_hours: float = 0.0):
        return cls(parse_days(days), parse_hours(hours), tz_offset_hours)

    @property
    def always(self) -> bool:
        return len(self.days) == 7 and self.windows == [(0, 1440)]

    def allows(self, utc_ts: float) -> bool:
        local = datetime.fromtimestamp(utc_ts, timezone.utc) + timedelta(hours=self.tz_offset_hours)
        if local.weekday() not in self.days:
            return False
        t = local.hour * 60 + local.minute
        for a, b in self.windows:
            if (a <= t < b) if a < b else (t >= a or t < b):   # a == b => ventana vacía
                return True
        return False

    def describe(self) -> str:
        names = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
        d = ",".join(names[i] for i in sorted(self.days))
        w = ", ".join(f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}" for a, b in self.windows)
        return f"{d} | {w} | UTC{self.tz_offset_hours:+g}"
