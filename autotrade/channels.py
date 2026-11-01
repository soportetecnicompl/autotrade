"""Canales de señales (varios) y carpetas de Telegram.

Lógica pura, sin Telegram ni interfaz, para poder probarla en cualquier sistema.

- ChannelCfg: un canal con su número estable (`num`), que identifica sus operaciones en MT5
  (comentario `tg<num>.<mensaje>`): no cambia aunque quites otros canales.
- parse_channels / render_channels: secciones `[channel.N]` del config.ini
  (compatible con el formato antiguo de un solo canal en `[telegram] channel`).
- folder_members: qué canales/grupos pertenecen a una carpeta de Telegram.
"""
from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class ChannelCfg:
    num: int                     # identificador estable (1, 2, 3…)
    ref: str                     # id numérico (-100…) o @usuario
    title: str = ""
    enabled: bool = True
    lot_size: float = 0.0        # 0 = usar el lote general
    max_tps: int = 0             # 0 = usar el máximo general

    def to_dict(self) -> dict:
        return {"num": self.num, "ref": self.ref, "title": self.title, "enabled": self.enabled,
                "lot_size": self.lot_size, "max_tps": self.max_tps}


def next_num(channels: List[ChannelCfg]) -> int:
    return max((c.num for c in channels), default=0) + 1


def parse_channels(cp: configparser.ConfigParser) -> List[ChannelCfg]:
    out: List[ChannelCfg] = []
    for sec in cp.sections():
        m = re.fullmatch(r"channel\.(\d+)", sec)
        if not m:
            continue
        s = cp[sec]
        ref = s.get("ref", "").strip()
        if not ref:
            continue

        def num(key, cast, default):
            try:
                return cast(float(s.get(key, "").strip() or default))
            except ValueError:
                return default
        out.append(ChannelCfg(
            num=int(m.group(1)), ref=ref, title=s.get("title", "").strip(),
            enabled=s.get("enabled", "true").strip().lower() in ("1", "true", "yes", "si", "sí", "on"),
            lot_size=num("lot_size", float, 0.0), max_tps=num("max_tps", int, 0)))
    out.sort(key=lambda c: c.num)
    if not out and cp.has_section("telegram"):      # formato antiguo: un solo canal
        ref = cp["telegram"].get("channel", "").strip()
        if ref:
            out.append(ChannelCfg(1, ref, cp["telegram"].get("channel_title", "").strip()))
    return out


def render_channels(channels: List[ChannelCfg]) -> str:
    if not channels:
        return ""
    blocks = []
    for c in channels:
        title = c.title.replace("\n", " ").replace("\r", " ")
        blocks.append(
            f"[channel.{c.num}]\n"
            f"ref = {c.ref}\n"
            f"title = {title}\n"
            f"enabled = {'true' if c.enabled else 'false'}\n"
            f"; 0 = usar el lote / máximo de TPs generales\n"
            f"lot_size = {c.lot_size:g}\n"
            f"max_tps = {c.max_tps}\n")
    return "\n".join(blocks)


def channel_ref(dialog: dict) -> str:
    """Referencia que se guarda en la configuración para un canal de la lista."""
    return ("@" + dialog["username"]) if dialog.get("username") else str(dialog["id"])


# ---------------------------------------------------------------- carpetas
def folder_members(spec: dict, dialogs: List[dict]) -> List[dict]:
    """Canales/grupos que pertenecen a una carpeta de Telegram.

    spec: {include:set[int], exclude:set[int], broadcasts, groups, exclude_muted,
           exclude_read, exclude_archived}  (ids = ids "marcados" de Telegram)
    dialogs: [{id, kind: 'canal'|'grupo', muted, archived, unread}]

    Igual que Telegram: los chats incluidos a mano siempre aparecen; los incluidos por
    categoría (todos los canales / todos los grupos) se filtran con las exclusiones de
    silenciados, leídos y archivados.
    """
    include, exclude = set(spec.get("include", ())), set(spec.get("exclude", ()))
    out = []
    for d in dialogs:
        i = d["id"]
        if i in exclude:
            continue
        explicit = i in include
        by_kind = ((spec.get("broadcasts") and d["kind"] == "canal")
                   or (spec.get("groups") and d["kind"] == "grupo"))
        if not (explicit or by_kind):
            continue
        if not explicit:
            if spec.get("exclude_muted") and d.get("muted"):
                continue
            if spec.get("exclude_read") and not d.get("unread"):
                continue
            if spec.get("exclude_archived") and d.get("archived"):
                continue
        out.append(d)
    return out


def folder_title(raw) -> str:
    """El título de una carpeta es texto o, en versiones nuevas de Telegram, un objeto con .text."""
    return str(getattr(raw, "text", raw) or "").strip()


def build_folders(raw_filters, dialogs: List[dict], peer_id) -> List[Dict]:
    """Convierte los filtros (carpetas) devueltos por Telegram en
    [{id, title, members:[ids de canales/grupos]}].

    `peer_id` es telethon.utils.get_peer_id (se inyecta para poder probar sin Telethon).
    Solo se consideran carpetas normales y compartidas; la carpeta por defecto se omite.
    """
    folders = []
    for f in raw_filters or []:
        if type(f).__name__ not in ("DialogFilter", "DialogFilterChatlist"):
            continue

        def ids(peers):
            out = set()
            for p in peers or []:
                try:
                    out.add(peer_id(p))
                except Exception:  # noqa: BLE001 - un peer raro no debe romper la carpeta
                    pass
            return out
        spec = {
            "include": ids(getattr(f, "include_peers", None)) | ids(getattr(f, "pinned_peers", None)),
            "exclude": ids(getattr(f, "exclude_peers", None)),
            "broadcasts": bool(getattr(f, "broadcasts", False)),
            "groups": bool(getattr(f, "groups", False)),
            "exclude_muted": bool(getattr(f, "exclude_muted", False)),
            "exclude_read": bool(getattr(f, "exclude_read", False)),
            "exclude_archived": bool(getattr(f, "exclude_archived", False)),
        }
        members = [d["id"] for d in folder_members(spec, dialogs)]
        folders.append({"id": getattr(f, "id", None), "title": folder_title(getattr(f, "title", "")) or "(sin nombre)",
                        "members": members})
    return folders
