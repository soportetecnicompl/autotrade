"""Avisos por Telegram a tu propio chat (Mensajes guardados) o a otro destino.

Niveles: "error" (siempre), "warn" (si notify_skips) e "info" (si notify_signals).
`notify` es síncrono y seguro de llamar desde código que corre en el loop de
asyncio; nunca lanza excepciones (un aviso fallido no debe afectar al trading).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Set

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, client=None, target: str = "me", enabled: bool = True,
                 signals: bool = True, skips: bool = True, prefix: str = ""):
        self.client = client
        self.target = int(target) if str(target).lstrip("-").isdigit() else target
        self.enabled = enabled and client is not None
        self.signals, self.skips, self.prefix = signals, skips, prefix
        self._tasks: Set[asyncio.Task] = set()

    def _wanted(self, level: str) -> bool:
        return self.enabled and (level == "error" or (level == "warn" and self.skips)
                                 or (level == "info" and self.signals))

    def notify(self, text: str, level: str = "info") -> None:
        if not self._wanted(level):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("Aviso no enviado (sin loop de asyncio): %s", text)
            return
        task = loop.create_task(self._send(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, text: str, level: str = "error") -> None:
        """Envío esperando a que termine (para avisos justo antes de salir)."""
        if self._wanted(level):
            await self._send(text)

    async def _send(self, text: str) -> None:
        try:
            await asyncio.wait_for(self.client.send_message(self.target, self.prefix + text), 20)
        except Exception as e:  # noqa: BLE001 - un aviso nunca debe romper el bot
            log.warning("No se pudo enviar el aviso (%s): %s", type(e).__name__, e)
