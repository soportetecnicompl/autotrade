"""Control del bot por Telegram: comandos que te escribes en «Mensajes guardados».

Solo se atienden mensajes SALIENTES de tu propia cuenta en tu chat guardado, así que nadie más puede
dar órdenes. Esta capa es pura: recibe funciones para consultar/ejecutar.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

CONFIRM_SECONDS = 60

HELP = ("Comandos (escríbelos aquí, en «Mensajes guardados»):\n"
        "/estado – resumen: modo, pausa, pérdida del día y operaciones abiertas\n"
        "/canales – lista de canales y si están en pausa\n"
        "/pausa – no abrir señales nuevas (los comandos de gestión siguen funcionando)\n"
        "/pausa 2 – pausa solo el canal número 2\n"
        "/reanudar – reanuda todo (también el límite diario de hoy)\n"
        "/reanudar 2 – reanuda solo el canal 2\n"
        "/cerrar – cierra TODAS las operaciones del bot (pide confirmación)")

ALIASES = {"estado": "estado", "status": "estado", "pausa": "pausa", "pausar": "pausa", "pause": "pausa",
           "reanudar": "reanudar", "resume": "reanudar", "continuar": "reanudar",
           "cerrar": "cerrar", "close": "cerrar", "canales": "canales", "ayuda": "ayuda", "help": "ayuda",
           "start": "ayuda"}


def parse_command(text: str) -> Optional[Tuple[str, List[str]]]:
    """'/pausa 2' -> ('pausa', ['2']). None si no es un comando."""
    t = (text or "").strip()
    if not t.startswith("/") or len(t) < 2:
        return None
    parts = t[1:].split()
    name = parts[0].split("@")[0].lower()
    return ALIASES.get(name, "?" + name), parts[1:]


class RemoteHandler:
    def __init__(self, control, channels: List[dict], status_fn: Callable[[], str],
                 close_fn: Callable[[], int], resume_fn: Callable[[], None],
                 now: Callable[[], float] = time.time):
        self.control, self.channels = control, channels
        self.status_fn, self.close_fn, self.resume_fn, self.now = status_fn, close_fn, resume_fn, now
        self._close_asked: Optional[float] = None

    def _channel(self, arg: str) -> Optional[dict]:
        if not arg.isdigit():
            return None
        return next((c for c in self.channels if c["num"] == int(arg)), None)

    def handle(self, text: str) -> Optional[str]:
        """Devuelve la respuesta a enviar, o None si el mensaje no es un comando."""
        parsed = parse_command(text)
        if parsed is None:
            return None
        cmd, args = parsed
        if cmd == "ayuda":
            return HELP
        if cmd == "estado":
            return self.status_fn()
        if cmd == "canales":
            paused = set(self.control.paused_channels())
            g = " (TODO EN PAUSA)" if self.control.read()["paused"] else ""
            lines = [f"#{c['num']} {c['title'] or c['ref']}"
                     f"{' – pausado' if c['num'] in paused else ''}{'' if c['enabled'] else ' – desactivado'}"
                     for c in self.channels]
            return "Canales" + g + ":\n" + ("\n".join(lines) or "(ninguno)")
        if cmd in ("pausa", "reanudar"):
            pause = cmd == "pausa"
            if args:
                ch = self._channel(args[0])
                if ch is None:
                    return f"No encuentro el canal «{args[0]}». Escribe /canales para ver los números."
                self.control.set_paused(pause, ch["num"])
                return (f"⏸ Canal #{ch['num']} {ch['title'] or ch['ref']} en pausa: no abrirá señales nuevas."
                        if pause else f"▶️ Canal #{ch['num']} {ch['title'] or ch['ref']} reanudado.")
            self.control.set_paused(pause)
            if pause:
                return "⏸ Bot en pausa: no abrirá señales nuevas. Las operaciones abiertas se siguen gestionando."
            self.resume_fn()
            return "▶️ Bot reanudado (incluido el límite diario de hoy, si había saltado)."
        if cmd == "cerrar":
            confirm = bool(args) and args[0].lower() in ("confirmar", "si", "sí", "ok")
            fresh = self._close_asked is not None and self.now() - self._close_asked <= CONFIRM_SECONDS
            if confirm and fresh:
                self._close_asked = None
                n = self.close_fn()
                return f"🔒 Cerradas {n} operación(es) y canceladas las pendientes del bot."
            if confirm and not fresh:
                return "La confirmación venció. Escribe /cerrar de nuevo."
            self._close_asked = self.now()
            return (f"⚠️ Vas a cerrar TODAS las operaciones del bot. Escribe /cerrar confirmar "
                    f"en los próximos {CONFIRM_SECONDS} segundos para continuar.")
        return f"No entiendo el comando «/{cmd[1:]}». Escribe /ayuda."
