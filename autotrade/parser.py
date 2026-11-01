"""Convierte el texto de un mensaje de Telegram en una señal o en comandos.

Soporta inglés y español. No depende de MT5 ni de Telegram, así que se puede
probar en cualquier sistema operativo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

NUM = r"(\d{3,5}(?:\.\d+)?)"
NUM_ANY = r"(\d+(?:\.\d+)?)"

SIDE_RE = re.compile(
    r"\b(BUY|SELL|LONG|SHORT|COMPRAR?|VENDER?|COMPRA|VENTA)\b"
    r"(?:\s+(?:XAUUSD|XAU|GOLD|ORO))?"   # "SELL GOLD limit 3915"
    r"(?:\s+(LIMIT|STOP))?"
)
SL_RE = re.compile(r"(?:\bSL|STOP\s*LOSS|STOPLOSS)\s*[:@=\-]?\s*" + NUM_ANY)
# "TP1 2655" / "TP 2655" / "TAKE PROFIT 2: 2660". El (?:\d\b)? evita comerse
# el primer dígito del precio en "TP 2655".
# También lista de TPs: "TP 3902 - 3890" o "TP 3902 / 3890".
TP_RE = re.compile(
    r"(?:\bTP|TAKE\s*PROFIT|TARGET)\s*(?:\d\b)?\s*[:@=\-]?\s*"
    r"(" + NUM_ANY + r"(?:\s*[-/,]\s*" + NUM_ANY + r")*)"
)
CUT_RE = re.compile(r"\bSL\b|STOP\s*LOSS|STOPLOSS|\bTP\b|\bTP\d|TAKE\s*PROFIT|TARGET")
ENTRY_RE = re.compile(NUM + r"(?:\s*[-/–]\s*" + NUM + r")?")

BREAKEVEN_RE = re.compile(
    r"\b(BREAK\s*-?\s*EVEN|BE|SL\s+(?:TO\s+|A\s+)?(?:ENTRY|ENTRADA)|"
    r"MOVE\s+(?:SL|STOP(?:\s*LOSS)?)\s+TO\s+(?:ENTRY|BE)|PONER\s+BE)\b"
)
CLOSE_ALL_RE = re.compile(
    r"\b(CLOSE\s+(?:ALL|NOW|TRADES?|POSITIONS?|XAUUSD|GOLD)|EXIT\s+(?:ALL|NOW)|"
    r"CERRAR(?:\s+(?:TODO|AHORA|OPERACI\w+|POSICI\w+))?)\b"
)
PARTIAL_RE = re.compile(
    r"\b(?:CLOSE|CERRAR|CIERRA)\s+(?:(HALF|MITAD|PARTIAL|PARCIAL(?:ES)?)|(\d{1,2})\s*%)"
)
MOVE_SL_RE = re.compile(
    r"(?:\bSL|STOP\s*LOSS)\s*(?:TO|A|->|=>|:|@)\s*" + NUM
)
CANCEL_RE = re.compile(r"\b(CANCEL|CANCELAR|DELETE\s+PENDING)\b")


@dataclass
class Signal:
    side: str                      # "BUY" | "SELL"
    kind: Optional[str] = None     # None | "limit" | "stop" (explícito en el texto)
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    sl: Optional[float] = None
    tps: List[float] = field(default_factory=list)


@dataclass
class Command:
    kind: str                      # breakeven | close | partial | move_sl | cancel
    value: Optional[float] = None  # precio (move_sl) o fracción (partial)


@dataclass
class ParseResult:
    signal: Optional[Signal] = None
    commands: List[Command] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.signal is not None or bool(self.commands)


def _normalize(text: str) -> str:
    t = text.upper()
    # 2,650.50 -> 2650.50 (solo comas de miles; "2655,2660" no se toca)
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)
    return t


def _side(word: str) -> str:
    return "BUY" if word in {"BUY", "LONG", "COMPRA", "COMPRAR"} else "SELL"


def parse_signal(text: str) -> Optional[Signal]:
    t = _normalize(text)
    m = SIDE_RE.search(t)
    if not m:
        return None

    sl_m = SL_RE.search(t)
    tps = [float(x) for m_ in TP_RE.finditer(t)
           for x in re.findall(r"\d+(?:\.\d+)?", m_.group(1))]
    # Evita falsos positivos tipo "buyers are taking profit": exige SL o TP.
    if not sl_m and not tps:
        return None

    sig = Signal(side=_side(m.group(1)))
    if m.group(2):
        sig.kind = m.group(2).lower()
    sig.sl = float(sl_m.group(1)) if sl_m else None
    sig.tps = tps

    rest = t[m.end():]
    cut = CUT_RE.search(rest)
    segment = rest[: cut.start()] if cut else rest
    e = ENTRY_RE.search(segment)
    if not e:
        e = re.search(r"@\s*" + NUM, t)
    if e:
        a = float(e.group(1))
        b = float(e.group(2)) if e.lastindex and e.lastindex >= 2 and e.group(2) else a
        sig.entry_low, sig.entry_high = min(a, b), max(a, b)
    return sig


def parse_commands(text: str) -> List[Command]:
    t = _normalize(text)
    cmds: List[Command] = []

    pm = PARTIAL_RE.search(t)
    if pm:
        frac = 0.5 if pm.group(1) else int(pm.group(2)) / 100
        if 0 < frac < 1:
            cmds.append(Command("partial", frac))
    elif CLOSE_ALL_RE.search(t) or t.strip() in {"CLOSE", "CERRAR"}:
        cmds.append(Command("close"))
        cmds.append(Command("cancel"))  # "close all" también borra pendientes

    if CANCEL_RE.search(t) and not any(c.kind == "cancel" for c in cmds):
        cmds.append(Command("cancel"))

    sm = MOVE_SL_RE.search(t)
    if sm:
        cmds.append(Command("move_sl", float(sm.group(1))))
    elif BREAKEVEN_RE.search(t):
        cmds.append(Command("breakeven"))
    return cmds


def parse(text: str) -> ParseResult:
    sig = parse_signal(text)
    if sig:
        return ParseResult(signal=sig)
    return ParseResult(commands=parse_commands(text))
