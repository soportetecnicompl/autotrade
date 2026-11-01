"""Corte de seguridad diario: límite de pérdida y de señales por día.

El día se cuenta en la zona horaria del usuario. El estado se guarda en un JSON para que
sobreviva a reinicios del bot. Una vez que salta el límite, queda activo hasta el día
siguiente o hasta que el usuario lo reanude a mano (override).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def day_key(utc_ts: float, tz_hours: float) -> str:
    return datetime.fromtimestamp(utc_ts + tz_hours * 3600, timezone.utc).strftime("%Y-%m-%d")


def seconds_into_day(utc_ts: float, tz_hours: float) -> int:
    return int((utc_ts + tz_hours * 3600) % 86400)


@dataclass
class DayState:
    day: str = ""
    start_balance: float = 0.0
    signals: int = 0
    tripped: bool = False
    reason: str = ""
    override: bool = False        # el usuario reanudó a mano: no se vuelve a cortar hoy


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    just_tripped: bool = False    # True solo la primera vez que salta (para avisar una vez)


class RiskGuard:
    def __init__(self, path: Optional[Path] = None, loss_pct: float = 0.0, max_signals: int = 0,
                 tz_hours: float = 0.0):
        self.path = Path(path) if path else None
        self.loss_pct, self.max_signals, self.tz = loss_pct, max_signals, tz_hours
        self.state = self._load()

    @property
    def enabled(self) -> bool:
        return self.loss_pct > 0 or self.max_signals > 0

    # ---- persistencia
    def _load(self) -> DayState:
        try:
            return DayState(**json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - sin archivo o dañado: empieza limpio
            return DayState()

    def _save(self) -> None:
        if self.path:
            try:
                self.path.write_text(json.dumps(asdict(self.state)), encoding="utf-8")
            except OSError:
                pass

    # ---- lógica
    def _roll(self, now: float, balance: float, realized: float) -> DayState:
        key = day_key(now, self.tz)
        if self.state.day != key:
            # saldo al inicio del día = saldo actual menos lo ya realizado hoy por el bot
            self.state = DayState(day=key, start_balance=max(balance - realized, 0.0))
            self._save()
        return self.state

    def _adopt_external_override(self) -> None:
        """El panel puede decir «seguir hoy» escribiendo el archivo desde otro proceso."""
        if self.state.override or not self.path:
            return
        disk = self._load()
        if disk.override and disk.day == self.state.day:
            self.state.override, self.state.tripped = True, False

    def check(self, now: float, balance: float, realized: float, floating: float) -> Verdict:
        st = self._roll(now, balance, realized)
        if st.tripped:
            self._adopt_external_override()
        if st.override:
            return Verdict(True)
        if not st.tripped:
            reason = ""
            loss = -(realized + floating)
            limit = st.start_balance * self.loss_pct / 100
            if self.loss_pct > 0 and limit > 0 and loss >= limit:
                reason = (f"pérdida del día {loss:.2f} USD ≥ límite {limit:.2f} USD "
                          f"({self.loss_pct:g}% de {st.start_balance:,.2f})")
            elif self.max_signals > 0 and st.signals >= self.max_signals:
                reason = f"ya se operaron {st.signals} señales hoy (máximo {self.max_signals})"
            if reason:
                st.tripped, st.reason = True, reason
                self._save()
                return Verdict(False, reason, just_tripped=True)
        return Verdict(not st.tripped, st.reason if st.tripped else "")

    def register_signal(self) -> None:
        self.state.signals += 1
        self._save()

    def resume(self) -> None:
        """El usuario decide seguir operando hoy aunque se haya alcanzado el límite."""
        self.state.override, self.state.tripped = True, False
        self._save()
