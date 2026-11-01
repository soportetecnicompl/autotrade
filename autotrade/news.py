"""Filtro de noticias: no abrir señales nuevas poco antes/después de noticias importantes.

Dos fuentes que se pueden combinar:
- manual: tú escribes las horas («Fri 08:30 NFP; Wed 14:00 FOMC»), en tu zona horaria. Siempre funciona.
- automática: descarga un calendario económico público (JSON) y toma las noticias de impacto alto de
  las monedas elegidas. Es una fuente de terceros: si no responde, se usa la última copia guardada y
  el bot sigue funcionando (nunca bloquea por un error de descarga).
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

from .schedule import _minutes, parse_days

DEFAULT_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_HOURS = 6


@dataclass
class NewsEvent:
    ts: float              # UTC
    title: str
    currency: str
    impact: str = "high"


def parse_iso(raw) -> float:
    s = str(raw).strip().replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def parse_calendar(data, currencies=("USD",), impacts=("high",)) -> List[NewsEvent]:
    """Lector tolerante del calendario: acepta varias formas de nombrar los campos."""
    cur_ok = {c.upper() for c in currencies}
    imp_ok = {i.lower() for i in impacts}
    out: List[NewsEvent] = []
    for e in data if isinstance(data, list) else []:
        try:
            impact = str(e.get("impact", "")).strip().lower()
            cur = str(e.get("country") or e.get("currency") or "").strip().upper()
            title = str(e.get("title") or e.get("event") or "").strip()
            ts = parse_iso(e.get("date") or e.get("datetime"))
        except (AttributeError, ValueError, TypeError):
            continue
        if impact in imp_ok and cur in cur_ok:
            out.append(NewsEvent(ts, title or "(sin título)", cur, impact))
    return sorted(out, key=lambda x: x.ts)


def validate_manual(text: str) -> List[str]:
    errs = []
    for entry in _entries(text):
        try:
            _parse_entry(entry)
        except ValueError as e:
            errs.append(f"«{entry}»: {e}")
    return errs


def _entries(text: str) -> List[str]:
    return [e.strip() for e in re.split(r"[;\n]", text or "") if e.strip()]


def _parse_entry(entry: str):
    m = re.match(r"^(\S+)\s+(\d{1,2}:\d{2})\s*(.*)$", entry)
    if not m:
        raise ValueError("formato esperado: «Fri 08:30 NFP» (días, hora y nombre opcional)")
    days = parse_days(m.group(1))
    return days, _minutes(m.group(2)), m.group(3).strip() or "Noticia"


def manual_events(text: str, tz_hours: float, now_ts: float, horizon_days: int = 2) -> List[NewsEvent]:
    """Convierte las entradas manuales en eventos concretos (ayer, hoy y los próximos días)."""
    out: List[NewsEvent] = []
    local_now = datetime.fromtimestamp(now_ts + tz_hours * 3600, timezone.utc)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    for entry in _entries(text):
        try:
            days, minute, label = _parse_entry(entry)
        except ValueError:
            continue
        for offset in range(-1, horizon_days + 1):
            day = midnight + timedelta(days=offset)
            if day.weekday() in days:
                ts = day.timestamp() + minute * 60 - tz_hours * 3600
                out.append(NewsEvent(ts, label, "—", "manual"))
    return sorted(out, key=lambda x: x.ts)


def find_blackout(events: List[NewsEvent], now: float, before_min: int, after_min: int) -> Optional[NewsEvent]:
    for e in events:
        if e.ts - before_min * 60 <= now <= e.ts + after_min * 60:
            return e
    return None


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AutoTrade"})
    return urllib.request.urlopen(req, timeout=15).read()


class NewsFilter:
    def __init__(self, mode: str = "off", before_min: int = 15, after_min: int = 15, tz_hours: float = 0.0,
                 manual_text: str = "", cache_path: Optional[Path] = None, currencies=("USD",),
                 url: str = DEFAULT_URL, fetch: Callable[[str], bytes] = _http_get):
        self.mode, self.before, self.after, self.tz = mode, before_min, after_min, tz_hours
        self.manual_text, self.currencies, self.url, self.fetch = manual_text, tuple(currencies), url, fetch
        self.cache_path = Path(cache_path) if cache_path else None
        self.calendar: List[NewsEvent] = []
        self.last_error = ""
        self._loaded_at = 0.0
        self._load_cache()

    @property
    def enabled(self) -> bool:
        return self.mode in ("manual", "auto")

    def _load_cache(self) -> None:
        try:
            c = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.calendar = parse_calendar(c["data"], self.currencies)
            self._loaded_at = float(c.get("fetched", 0))
        except Exception:  # noqa: BLE001
            pass

    def refresh(self, now: Optional[float] = None) -> str:
        """Actualiza el calendario automático si la copia es vieja. Devuelve un mensaje de estado."""
        now = now or time.time()
        if self.mode != "auto":
            return ""
        if self.calendar and now - self._loaded_at < CACHE_HOURS * 3600:
            return ""
        try:
            data = json.loads(self.fetch(self.url).decode("utf-8"))
            events = parse_calendar(data, self.currencies)
            self.calendar, self._loaded_at, self.last_error = events, now, ""
            if self.cache_path:
                self.cache_path.write_text(json.dumps({"fetched": now, "data": data}), encoding="utf-8")
            return f"Calendario de noticias actualizado ({len(events)} noticias de impacto alto)."
        except Exception as e:  # noqa: BLE001 - una descarga fallida nunca debe bloquear el trading
            self.last_error = f"{type(e).__name__}: {e}"
            self._loaded_at = now - (CACHE_HOURS - 0.25) * 3600      # reintenta en ~15 min
            return (f"No se pudo actualizar el calendario de noticias ({self.last_error})."
                    + (" Se usa la última copia guardada." if self.calendar else " El filtro automático no está activo."))

    def check(self, now: float) -> Optional[str]:
        """Motivo por el que NO se debe abrir ahora, o None."""
        if not self.enabled:
            return None
        events = list(manual_events(self.manual_text, self.tz, now))
        if self.mode == "auto":
            events += self.calendar
        e = find_blackout(events, now, self.before, self.after)
        if e is None:
            return None
        hhmm = datetime.fromtimestamp(e.ts + self.tz * 3600, timezone.utc).strftime("%H:%M")
        who = f"{e.currency} " if e.currency != "—" else ""
        return f"noticia {who}«{e.title}» a las {hhmm} (±{self.before}/{self.after} min)"
