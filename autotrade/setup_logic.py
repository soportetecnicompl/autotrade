"""Lógica del asistente de instalación (sin interfaz gráfica, para poder probarla).

- Settings: todo lo configurable desde el asistente.
- render_config / load_settings: escribe y lee config.ini (compatible con main.load_config).
- validadores con mensajes claros en español.
- detección de MT5, arranque automático (carpeta Inicio de Windows) y offset horario.
"""
from __future__ import annotations

import configparser
import glob
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import List, Optional

from .backtest import is_us_dst
from .channels import ChannelCfg, parse_channels, render_channels
from .schedule import TradingSchedule


@dataclass
class Settings:
    # Telegram
    api_id: str = ""
    api_hash: str = ""
    phone: str = ""
    session_name: str = "autotrade"
    channels: List[ChannelCfg] = field(default_factory=list)
    # MT5
    mt5_path: str = ""
    mt5_login: str = ""
    mt5_password: str = ""
    mt5_server: str = ""
    symbol: str = "XAUUSD"
    magic: int = 20260101
    deviation: int = 30
    # Operativa
    dry_run: bool = True
    lot_size: float = 0.02
    max_tps: int = 2
    max_open_positions: int = 6
    enable_commands: bool = True
    stops_policy: str = "skip"
    auto_be: bool = False                 # break-even automático cuando se cumple el TP1
    # Protecciones
    daily_loss_limit_pct: float = 0.0     # 0 = sin límite
    max_signals_per_day: int = 0          # 0 = sin límite
    close_on_limit: bool = False          # al saltar el límite diario, cerrar también lo abierto
    max_spread_usd: float = 0.0           # 0 = sin filtro
    news_mode: str = "off"                # off | manual | auto
    news_minutes_before: int = 15
    news_minutes_after: int = 15
    news_manual: str = ""                 # «Fri 08:30 NFP; Wed 14:00 FOMC»
    news_currencies: str = "USD"
    # Interfaz
    theme: str = "auto"                   # auto | light | dark
    # Filtro de horario
    filter_enabled: bool = False
    trading_days: str = "Mon-Fri"
    trading_hours: str = "08:00-17:00"
    tz_offset_hours: float = -6.0
    # Avisos
    alerts_enabled: bool = True
    notify_signals: bool = True
    notify_skips: bool = True
    heartbeat_url: str = ""


def _b(v: bool) -> str:
    return "true" if v else "false"


def render_config(s: Settings) -> str:
    days = s.trading_days if s.filter_enabled else ""
    hours = s.trading_hours if s.filter_enabled else ""
    return f"""; Generado por el asistente de AutoTrade. Puedes editarlo a mano o volver a abrir el asistente.

[telegram]
api_id = {s.api_id}
api_hash = {s.api_hash}
phone = {s.phone}
session_name = {s.session_name}

{render_channels(s.channels)}
[mt5]
login = {s.mt5_login}
password = {s.mt5_password}
server = {s.mt5_server}
path = {s.mt5_path}
symbol = {s.symbol}
magic = {s.magic}
deviation = {s.deviation}

[trading]
dry_run = {_b(s.dry_run)}
lot_size = {s.lot_size}
risk_percent = 0
max_tps = {s.max_tps}
max_open_positions = {s.max_open_positions}
market_tolerance = 1.5
max_pending_distance = 30
pending_expiry_min = 60
max_signal_age_sec = 60
allow_no_tp = false
enable_commands = {_b(s.enable_commands)}
be_offset = 0.0
requote_retries = 3
retry_delay_ms = 200
retry_max_slippage = 1.0
stops_policy = {s.stops_policy}
auto_be = {_b(s.auto_be)}

[risk]
daily_loss_limit_pct = {s.daily_loss_limit_pct:g}
max_signals_per_day = {s.max_signals_per_day}
close_on_limit = {_b(s.close_on_limit)}

[filters]
trading_days = {days}
trading_hours = {hours}
tz_offset_hours = {s.tz_offset_hours:g}
max_spread_usd = {s.max_spread_usd:g}
news_mode = {s.news_mode}
news_minutes_before = {s.news_minutes_before}
news_minutes_after = {s.news_minutes_after}
news_manual = {s.news_manual.replace(chr(10), " ").replace(chr(13), " ")}
news_currencies = {s.news_currencies}

[ui]
theme = {s.theme}

[alerts]
enabled = {_b(s.alerts_enabled)}
target = me
notify_signals = {_b(s.notify_signals)}
notify_skips = {_b(s.notify_skips)}
mt5_check_seconds = 30
heartbeat_url = {s.heartbeat_url}
heartbeat_minutes = 5
"""


def load_settings(path: os.PathLike) -> Settings:
    s = Settings()
    cp = configparser.ConfigParser(interpolation=None)
    if not cp.read(path, encoding="utf-8"):
        return s

    def g(sec, key, default):
        try:
            raw = cp.get(sec, key).strip()
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default
        if isinstance(default, bool):
            return raw.lower() in ("1", "true", "yes", "si", "sí", "on")
        if isinstance(default, int):
            try:
                return int(float(raw))
            except ValueError:
                return default
        if isinstance(default, float):
            try:
                return float(raw)
            except ValueError:
                return default
        return raw

    mapping = {
        "api_id": ("telegram", "api_id"), "api_hash": ("telegram", "api_hash"),
        "phone": ("telegram", "phone"), "session_name": ("telegram", "session_name"),
        "mt5_path": ("mt5", "path"), "mt5_login": ("mt5", "login"), "mt5_password": ("mt5", "password"),
        "mt5_server": ("mt5", "server"), "symbol": ("mt5", "symbol"), "magic": ("mt5", "magic"),
        "deviation": ("mt5", "deviation"), "dry_run": ("trading", "dry_run"),
        "lot_size": ("trading", "lot_size"), "max_tps": ("trading", "max_tps"),
        "max_open_positions": ("trading", "max_open_positions"),
        "enable_commands": ("trading", "enable_commands"), "stops_policy": ("trading", "stops_policy"),
        "trading_days": ("filters", "trading_days"), "trading_hours": ("filters", "trading_hours"),
        "tz_offset_hours": ("filters", "tz_offset_hours"), "alerts_enabled": ("alerts", "enabled"),
        "auto_be": ("trading", "auto_be"), "daily_loss_limit_pct": ("risk", "daily_loss_limit_pct"),
        "max_signals_per_day": ("risk", "max_signals_per_day"), "close_on_limit": ("risk", "close_on_limit"),
        "max_spread_usd": ("filters", "max_spread_usd"), "news_mode": ("filters", "news_mode"),
        "news_minutes_before": ("filters", "news_minutes_before"), "news_minutes_after": ("filters", "news_minutes_after"),
        "news_manual": ("filters", "news_manual"), "news_currencies": ("filters", "news_currencies"),
        "theme": ("ui", "theme"),
        "notify_signals": ("alerts", "notify_signals"), "notify_skips": ("alerts", "notify_skips"),
        "heartbeat_url": ("alerts", "heartbeat_url"),
    }
    for f in fields(Settings):
        if f.name in mapping:
            sec, key = mapping[f.name]
            setattr(s, f.name, g(sec, key, getattr(s, f.name)))
    s.channels = parse_channels(cp)
    s.filter_enabled = bool(g("filters", "trading_days", "") or g("filters", "trading_hours", ""))
    if not s.trading_days:
        s.trading_days = "Mon-Fri"
    if not s.trading_hours:
        s.trading_hours = "08:00-17:00"
    return s


# ------------------------------------------------------------- validaciones
def validate_telegram(s: Settings) -> List[str]:
    errs = []
    if not s.api_id.strip().isdigit():
        errs.append("El api_id debe ser un número (lo ves en my.telegram.org).")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", s.api_hash.strip()):
        errs.append("El api_hash debe tener 32 caracteres (letras a-f y números). Cópialo completo de my.telegram.org.")
    if not re.fullmatch(r"\+\d{8,15}", s.phone.strip()):
        errs.append("El teléfono debe llevar el código de país y empezar con +, por ejemplo +50499998888.")
    return errs


def validate_channel(s: Settings) -> List[str]:
    errs = []
    active = [c for c in s.channels if c.enabled and c.ref.strip()]
    if not active:
        errs.append("Elige al menos un canal de señales (o escribe su @usuario) y déjalo activo.")
    for c in s.channels:
        name = c.title or c.ref
        if c.lot_size and not (0.01 <= c.lot_size <= 100):
            errs.append(f"Canal «{name}»: el lote debe estar entre 0.01 y 100 (o vacío para usar el general).")
        if c.max_tps not in (0, 1, 2, 3):
            errs.append(f"Canal «{name}»: los TPs deben ser 1, 2 o 3 (o el general).")
    return errs


def validate_mt5(s: Settings) -> List[str]:
    errs = []
    if not s.symbol.strip():
        errs.append("Escribe el nombre del símbolo del oro en tu broker (por ejemplo XAUUSD).")
    if s.mt5_login.strip() and not s.mt5_login.strip().isdigit():
        errs.append("El número de cuenta de MT5 solo lleva dígitos (o déjalo vacío para usar la cuenta abierta en MT5).")
    if s.mt5_login.strip() and not (s.mt5_password and s.mt5_server.strip()):
        errs.append("Si indicas el número de cuenta, también hacen falta la contraseña y el servidor.")
    if any("\n" in v or "\r" in v for v in (s.mt5_password, s.mt5_server, s.mt5_path)):
        errs.append("Los campos de MT5 no pueden tener saltos de línea.")
    return errs


def validate_trading(s: Settings) -> List[str]:
    errs = []
    if not (0.01 <= s.lot_size <= 100):
        errs.append("El lote total debe estar entre 0.01 y 100.")
    if s.max_tps not in (1, 2, 3):
        errs.append("El número de TPs debe ser 1, 2 o 3.")
    if s.max_open_positions < 1:
        errs.append("El máximo de posiciones abiertas debe ser al menos 1.")
    if s.stops_policy not in ("skip", "adjust"):
        errs.append("Elige qué hacer cuando el SL/TP queda muy cerca del precio.")
    if s.filter_enabled:
        try:
            TradingSchedule.from_text(s.trading_days, s.trading_hours, s.tz_offset_hours)
        except ValueError as e:
            errs.append(f"Filtro de horario: {e}")
        if not s.trading_days.strip() and not s.trading_hours.strip():
            errs.append("Indica días y/o horas para el filtro, o desactívalo.")
    return errs


def validate_protections(s: Settings) -> List[str]:
    from .news import validate_manual
    errs = []
    if not 0 <= s.daily_loss_limit_pct <= 50:
        errs.append("El límite de pérdida diaria debe estar entre 0 y 50 % (0 = sin límite).")
    if s.max_signals_per_day < 0:
        errs.append("El máximo de señales por día no puede ser negativo (0 = sin límite).")
    if not 0 <= s.max_spread_usd <= 50:
        errs.append("El spread máximo debe estar entre 0 y 50 USD (0 = sin filtro).")
    if s.news_mode not in ("off", "manual", "auto"):
        errs.append("Elige cómo filtrar las noticias: desactivado, manual o automático.")
    for label, v in (("antes", s.news_minutes_before), ("después", s.news_minutes_after)):
        if not 0 <= v <= 240:
            errs.append(f"Los minutos {label} de una noticia deben estar entre 0 y 240.")
    if s.news_mode in ("manual", "auto") and s.news_manual.strip():
        errs += [f"Noticias manuales: {e}" for e in validate_manual(s.news_manual)]
    if s.news_mode == "manual" and not s.news_manual.strip():
        errs.append("Escribe al menos una noticia (ej. «Fri 08:30 NFP») o elige otro modo de noticias.")
    if s.news_mode == "auto" and not re.fullmatch(r"[A-Za-z]{3}(\s*,\s*[A-Za-z]{3})*", s.news_currencies.strip()):
        errs.append("Las monedas de noticias van separadas por comas, ej. USD, EUR.")
    if s.theme not in ("auto", "light", "dark"):
        errs.append("El tema debe ser automático, claro u oscuro.")
    return errs


def validate_alerts(s: Settings) -> List[str]:
    u = s.heartbeat_url.strip()
    if u and not re.match(r"https?://\S+$", u):
        return ["La dirección del latido debe empezar con http:// o https://"]
    return []


def validate_all(s: Settings) -> List[str]:
    return (validate_telegram(s) + validate_channel(s) + validate_mt5(s)
            + validate_trading(s) + validate_protections(s) + validate_alerts(s))


# ---------------------------------------------------------------- MT5 / red
def detect_mt5_paths(env: Optional[dict] = None) -> List[str]:
    """Busca terminal64.exe en las carpetas habituales de Windows."""
    env = env if env is not None else os.environ
    roots = [env.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    roots += [env.get("LOCALAPPDATA") and os.path.join(env["LOCALAPPDATA"], "Programs")]
    found: List[str] = []
    for root in filter(None, roots):
        for pat in ("*MetaTrader*/terminal64.exe", "*MT5*/terminal64.exe", "*/terminal64.exe"):
            for p in glob.glob(os.path.join(glob.escape(root), pat)):
                if p not in found:
                    found.append(p)
    return found


def estimate_server_offset(tick_time: float, now_utc: float) -> Optional[int]:
    """Horas de diferencia entre el servidor del broker y UTC, a partir de la hora del
    último tick. Devuelve None si el tick es viejo (mercado cerrado) o incoherente."""
    diff = tick_time - now_utc
    hours = round(diff / 3600)
    if abs(diff - hours * 3600) > 600 or not -12 <= hours <= 14:   # tolera 10 min de desfase
        return None
    return int(hours)


def base_offset(current: int, now_utc: float, broker_uses_us_dst: bool) -> int:
    """Offset de invierno a partir del offset actual (lo que espera --server-offset)."""
    return current - (1 if broker_uses_us_dst and is_us_dst(now_utc) else 0)


# ------------------------------------------------------- arranque automático
STARTUP_FILE = "AutoTrade.cmd"


def startup_dir(env: Optional[dict] = None) -> Optional[Path]:
    env = env if env is not None else os.environ
    appdata = env.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def render_startup_cmd(command: List[str], delay_seconds: int = 30) -> str:
    """.cmd para la carpeta Inicio: espera unos segundos (red/MT5 listos) y lanza el bot minimizado."""
    import subprocess
    quoted = subprocess.list2cmdline(command)
    return ("@echo off\r\n"
            "rem AutoTrade: inicio automatico (creado por el asistente)\r\n"
            f"ping -n {delay_seconds + 1} 127.0.0.1 >nul\r\n"
            f'start "" /min {quoted}\r\n')


def install_startup(command: List[str], env: Optional[dict] = None) -> Path:
    d = startup_dir(env)
    if d is None:
        raise RuntimeError("No se encontró la carpeta Inicio de Windows (solo disponible en Windows).")
    d.mkdir(parents=True, exist_ok=True)
    p = d / STARTUP_FILE
    p.write_bytes(render_startup_cmd(command).encode("utf-8"))
    return p


def remove_startup(env: Optional[dict] = None) -> bool:
    d = startup_dir(env)
    p = d / STARTUP_FILE if d else None
    if p and p.exists():
        p.unlink()
        return True
    return False


def startup_installed(env: Optional[dict] = None) -> bool:
    d = startup_dir(env)
    return bool(d and (d / STARTUP_FILE).exists())
