"""Login y consultas a Telegram para el asistente (Telethon en un hilo con su propio loop).

Todos los métodos son bloqueantes: llámalos desde un hilo de trabajo, no desde el hilo
de la interfaz. La sesión se guarda en la carpeta de datos y la reutiliza el bot.
"""
from __future__ import annotations

import asyncio
import threading
from typing import List, Optional


class TelegramError(Exception):
    """Error con mensaje listo para mostrar al usuario."""


class TelegramHelper:
    def __init__(self, api_id: int, api_hash: str, session: str = "autotrade"):
        self.api_id, self.api_hash, self.session = api_id, api_hash, session
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self.client = None
        self._phone = ""
        self._hash = None

    def _run(self, coro, timeout: float = 60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    @staticmethod
    def _friendly(e: Exception) -> TelegramError:
        n = type(e).__name__
        msgs = {
            "ApiIdInvalidError": "El api_id o el api_hash no son válidos. Revísalos en my.telegram.org.",
            "PhoneNumberInvalidError": "Telegram no reconoce ese número. Escríbelo con + y código de país.",
            "PhoneCodeInvalidError": "Ese código no es correcto. Revisa el mensaje que te llegó a Telegram.",
            "PhoneCodeExpiredError": "El código venció. Pulsa «Enviar código» otra vez.",
            "PasswordHashInvalidError": "La contraseña de verificación en dos pasos no es correcta.",
            "FloodWaitError": "Telegram pide esperar un poco antes de reintentar. Prueba en unos minutos.",
        }
        if n in msgs:
            return TelegramError(msgs[n])
        if isinstance(e, (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)):
            return TelegramError("No hay conexión con Telegram. Revisa tu internet.")
        return TelegramError(f"{n}: {e}")

    # ---- sesión
    def connect(self) -> bool:
        """Conecta y devuelve True si la sesión guardada ya está autorizada."""
        from telethon import TelegramClient

        async def go():
            if self.client is None:
                self.client = TelegramClient(self.session, self.api_id, self.api_hash,
                                             loop=self.loop, connection_retries=3)
            if not self.client.is_connected():
                await self.client.connect()
            return await self.client.is_user_authorized()
        try:
            return self._run(go())
        except Exception as e:  # noqa: BLE001
            raise self._friendly(e) from e

    def request_code(self, phone: str) -> None:
        async def go():
            r = await self.client.send_code_request(phone)
            self._phone, self._hash = phone, r.phone_code_hash
        try:
            self._run(go())
        except Exception as e:  # noqa: BLE001
            raise self._friendly(e) from e

    def sign_in(self, code: str) -> str:
        """'ok' si entró; 'password' si la cuenta tiene verificación en dos pasos."""
        from telethon.errors import SessionPasswordNeededError

        async def go():
            try:
                await self.client.sign_in(self._phone, code.strip(), phone_code_hash=self._hash)
                return "ok"
            except SessionPasswordNeededError:
                return "password"
        try:
            return self._run(go())
        except Exception as e:  # noqa: BLE001
            raise self._friendly(e) from e

    def sign_in_password(self, password: str) -> None:
        try:
            self._run(self.client.sign_in(password=password))
        except Exception as e:  # noqa: BLE001
            raise self._friendly(e) from e

    def me_name(self) -> str:
        async def go():
            me = await self.client.get_me()
            return " ".join(x for x in (me.first_name, me.last_name) if x) or (me.username or str(me.id))
        return self._run(go())

    # ---- consultas
    def channels(self) -> List[dict]:
        """Canales y grupos de la cuenta: [{id, name, username, kind}]."""
        async def go():
            out = []
            async for d in self.client.iter_dialogs():
                if d.is_channel or d.is_group:
                    out.append({"id": d.id, "name": d.name or "(sin nombre)",
                                "username": getattr(d.entity, "username", None) or "",
                                "kind": "grupo" if d.is_group else "canal"})
            return out
        try:
            return self._run(go(), timeout=120)
        except Exception as e:  # noqa: BLE001
            raise self._friendly(e) from e

    def overview(self) -> dict:
        """Canales/grupos y carpetas de Telegram:
        {channels: [{id,name,username,kind,muted,archived,unread}], folders: [{id,title,members}],
         folders_error: str}. Si no se pueden leer las carpetas, se devuelven solo los canales."""
        from telethon import functions, utils

        from .channels import build_folders

        async def go():
            import time as _time
            dialogs = []
            async for d in self.client.iter_dialogs():
                if not (d.is_channel or d.is_group):
                    continue
                mute = getattr(getattr(getattr(d, "dialog", None), "notify_settings", None), "mute_until", None)
                try:
                    muted = bool(mute) and (mute.timestamp() if hasattr(mute, "timestamp") else float(mute)) > _time.time()
                except Exception:  # noqa: BLE001
                    muted = False
                dialogs.append({"id": d.id, "name": d.name or "(sin nombre)",
                                "username": getattr(d.entity, "username", None) or "",
                                "kind": "grupo" if d.is_group else "canal", "muted": muted,
                                "archived": bool(getattr(d, "archived", False)),
                                "unread": int(getattr(d, "unread_count", 0) or 0)})
            folders, err = [], ""
            try:
                res = await self.client(functions.messages.GetDialogFiltersRequest())
                folders = build_folders(getattr(res, "filters", res), dialogs, utils.get_peer_id)
            except Exception as e:  # noqa: BLE001 - las carpetas son un extra: no deben impedir elegir canal
                err = f"{type(e).__name__}: {e}"
            return {"channels": dialogs, "folders": folders, "folders_error": err}
        try:
            return self._run(go(), timeout=180)
        except Exception as e:  # noqa: BLE001
            raise self._friendly(e) from e

    def send_to_me(self, text: str) -> None:
        try:
            self._run(self.client.send_message("me", text))
        except Exception as e:  # noqa: BLE001
            raise self._friendly(e) from e

    def close(self) -> None:
        """Desconecta y libera el archivo de sesión (necesario antes de lanzar el bot)."""
        try:
            if self.client is not None:
                self._run(self.client.disconnect(), timeout=15)
        except Exception:  # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
