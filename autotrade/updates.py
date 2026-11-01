"""Aviso de nueva versión (consulta la última Release publicada en GitHub)."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable, Tuple

RELEASES_API = "https://api.github.com/repos/soportetecnicompl/autotrade/releases/latest"
RELEASES_PAGE = "https://github.com/soportetecnicompl/autotrade/releases"


def parse_version(tag: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AutoTrade", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=12).read()


def check(current: str, url: str = RELEASES_API, fetch: Callable[[str], bytes] = _get) -> dict:
    """{status: 'newer'|'current'|'none'|'error', latest, url, message}"""
    try:
        data = json.loads(fetch(url).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "none", "latest": "", "url": RELEASES_PAGE,
                    "message": "Todavía no hay versiones publicadas (o el repositorio es privado)."}
        return {"status": "error", "latest": "", "url": RELEASES_PAGE, "message": f"GitHub respondió {e.code}."}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "latest": "", "url": RELEASES_PAGE,
                "message": f"No se pudo consultar ({type(e).__name__}). Revisa tu internet."}
    tag = str(data.get("tag_name", ""))
    page = data.get("html_url") or RELEASES_PAGE
    if tag and is_newer(tag, current):
        return {"status": "newer", "latest": tag, "url": page, "message": f"Hay una versión nueva: {tag} (tienes {current})."}
    return {"status": "current", "latest": tag, "url": page, "message": f"Tienes la última versión ({current})."}
