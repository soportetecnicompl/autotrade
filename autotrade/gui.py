"""Asistente de instalación y panel de control de AutoTrade (tkinter).

Un paso por pantalla, con instrucciones en lenguaje sencillo. La configuración se guarda
en config.ini (carpeta de datos del usuario) al avanzar de paso. Al final hay un panel
para iniciar/detener el bot y ver su registro.
"""
from __future__ import annotations

import os
import queue
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import tkinter as tk
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Dict, List, Optional

from . import backup, mt5_helper, perf, setup_logic as sl, updates
from .control import Control
from .eventlog import EventLog
from .risk import RiskGuard, day_key
from .version import __version__
from .paths import (IS_WINDOWS, data_dir, launcher_cmd, pid_alive, popen_kwargs, stop_tree)
from .schedule import parse_days
from .setup_logic import Settings
from .channels import ChannelCfg, channel_ref, next_num
from .supervisor import PID_FILE, read_pid

WRAP = 640

LIGHT = dict(
    BG="#ffffff", BAR="#f3f5f9", BORDER="#d5dae6", TEXT="#1d2433", MUTED="#667085",
    ACCENT="#2a5bd7", ACCENT_DK="#1f48ad", ACCENT_PR="#1a3d94", LINK="#1f48ad",
    OK="#0b7a43", BAD="#c4321f", WARN="#a15c00", DANGER="#c4321f",
    SIDEBAR="#18202f", SIDE_ACTIVE="#2a3650", SIDE_HOVER="#222c41", SIDE_TEXT="#aab4cc", SIDE_DONE="#dfe5f3",
    SIDE_SUB="#8d99b3", CHIP="#38435c", CHIP_DONE="#1f9d63", SIDE_BAR="#7aa2ff",
    FIELD="#ffffff", SEL="#dbe6ff", TROUGH="#e6eaf2", DIS_FG="#98a1b3", HOVER_BORDER="#aab4cc",
    B2_BG="#f4f6fb", B2_BORDER="#9aa6c0", B2_HOVER="#e4e9f5", B2_PRESS="#d6ddee", B2_DIS_BG="#f4f6fa", B2_DIS_FG="#6f788d",
    B1_DIS_BG="#dfe5f5", B1_DIS_FG="#6b7fb5",
    BD_HOVER="#a82a19", BD_PRESS="#8f2214", BD_DIS_BG="#f6dcd8", BD_DIS_FG="#b0675d",
    IND_BORDER="#7d89a6", IND_PRESS="#e1e7f3", HEAD_HOVER="#e8ecf5", SCROLL="#c9d0de", SCROLL_HOVER="#aab4cc",
    TINT_OK=("#e8f6ee", "#0b7a43"), TINT_WARN=("#fff3de", "#a15c00"), TINT_BAD=("#fdeceb", "#c4321f"),
    TINT_INFO=("#eaf0fd", "#1f48ad"),
)
DARK = dict(
    BG="#12161f", BAR="#181d28", BORDER="#2c3444", TEXT="#e6e9f0", MUTED="#9aa4b8",
    ACCENT="#3b69e6", ACCENT_DK="#2f58cc", ACCENT_PR="#2749b0", LINK="#8fb0ff",
    OK="#4fd69a", BAD="#ff7a6b", WARN="#ffb454", DANGER="#c2392a",
    SIDEBAR="#0c1018", SIDE_ACTIVE="#1b2436", SIDE_HOVER="#141b29", SIDE_TEXT="#aab4cc", SIDE_DONE="#dfe5f3",
    SIDE_SUB="#7d899f", CHIP="#2c3548", CHIP_DONE="#1f9d63", SIDE_BAR="#6c93ff",
    FIELD="#1a202c", SEL="#2a3a63", TROUGH="#232b3a", DIS_FG="#6c768b", HOVER_BORDER="#4b5870",
    B2_BG="#222a38", B2_BORDER="#4a566e", B2_HOVER="#2b3548", B2_PRESS="#34405a", B2_DIS_BG="#1c2330", B2_DIS_FG="#8a94a9",
    B1_DIS_BG="#26314d", B1_DIS_FG="#8fa3d6",
    BD_HOVER="#a82f21", BD_PRESS="#8f261a", BD_DIS_BG="#3c1f1d", BD_DIS_FG="#d29a92",
    IND_BORDER="#6f7b94", IND_PRESS="#34405a", HEAD_HOVER="#232b3a", SCROLL="#3a4558", SCROLL_HOVER="#4b5870",
    TINT_OK=("#12301f", "#5ee0a0"), TINT_WARN=("#3a2a0c", "#ffc66d"), TINT_BAD=("#3d1a17", "#ff8f82"),
    TINT_INFO=("#16264a", "#9bb8ff"),
)
THEME_NAMES = {"auto": "Automático (como Windows)", "light": "Claro", "dark": "Oscuro"}


def detect_system_dark() -> bool:
    """¿Windows está en modo oscuro? (fuera de Windows: no)."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:  # noqa: BLE001
        return False


def resolve_theme(pref: str) -> str:
    return pref if pref in ("light", "dark") else ("dark" if detect_system_dark() else "light")


def apply_palette(name: str) -> None:
    """Fija los colores globales ANTES de crear la ventana (todos los widgets los leen al crearse)."""
    global TINT
    pal = DARK if name == "dark" else LIGHT
    for key, val in pal.items():
        if not key.startswith("TINT_"):
            globals()[key] = val
    TINT = {"ok": pal["TINT_OK"], "warn": pal["TINT_WARN"], "bad": pal["TINT_BAD"], "info": pal["TINT_INFO"]}


apply_palette("light")
DAYS = [("Mon", "Lun"), ("Tue", "Mar"), ("Wed", "Mié"), ("Thu", "Jue"), ("Fri", "Vie"), ("Sat", "Sáb"), ("Sun", "Dom")]
TZ_CHOICES = ["-8", "-7", "-6", "-5", "-4", "-3", "0", "1", "2", "3"]
SAMPLE = ("SELL GOLD  3912 -  3917\n\nTakeprofit1 = 3902\nTakeprofit2 = 3892\nTakeprofit3 = 3812\n\nStoploss = 3932")


# ------------------------------------------------------------------ utilidades
def run_logged(cmd: List[str], outfile: Path, emit: Callable[[str], None], cwd: Optional[str] = None) -> int:
    """Ejecuta un proceso hijo que escribe su salida en `outfile` (AUTOTRADE_OUTFILE) y la va
    mostrando con `emit`. No depende de tuberías: un .exe de ventanas puede no tener stdout."""
    outfile = Path(outfile)
    outfile.write_text("", encoding="utf-8")
    env = {**os.environ, "AUTOTRADE_OUTFILE": str(outfile), "PYTHONIOENCODING": "utf-8"}
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **popen_kwargs())
    pos = 0

    def drain():
        nonlocal pos
        with open(outfile, "rb") as f:
            f.seek(pos)
            chunk = f.read()
            pos += len(chunk)
        if chunk:
            emit(chunk.decode("utf-8", errors="replace"))

    while p.poll() is None:
        drain()
        time.sleep(0.3)
    drain()
    return p.returncode


def days_label(text: str) -> str:
    """'Mon,Tue,Wed' -> 'Lun, Mar, Mié' (para mostrar al usuario)."""
    try:
        return ", ".join(DAYS[i][1] for i in sorted(parse_days(text)))
    except ValueError:
        return text


def days_to_text(selected: set) -> str:
    return ",".join(code for i, (code, _) in enumerate(DAYS) if i in selected)


class Banner(tk.Label):
    """Mensaje de estado con color de fondo según el tipo (ok / warn / bad / info)."""

    def __init__(self, parent):
        super().__init__(parent, text="", anchor="w", justify="left", bg=BG, fg=MUTED, padx=14, pady=0,
                         wraplength=WRAP - 30, bd=0, highlightthickness=0)

    def show(self, msg: str, kind: str = "info") -> None:
        if not msg:
            self.config(text="", bg=BG, pady=0)
            return
        bg, fg = TINT.get(kind, TINT["info"])
        self.config(text=msg, bg=bg, fg=fg, pady=9)


class Step(ttk.Frame):
    title = ""
    short = ""

    def __init__(self, app: "App", parent):
        super().__init__(parent)
        self.app = app
        self.build()

    def build(self) -> None: ...
    def on_show(self) -> None: ...
    def leave(self) -> None:
        """Se llama al salir del paso (adelante o atrás): confirmar campos especiales."""
    def check(self) -> List[str]:
        """Errores que impiden avanzar (lista vacía = se puede avanzar)."""
        return []
    def done(self) -> bool:
        return False

    # ---- widgets comunes
    def head(self, title: str, intro: str = "") -> None:
        ttk.Label(self, text=title, style="Title.TLabel").pack(anchor="w")
        if intro:
            ttk.Label(self, text=intro, wraplength=WRAP, justify="left", style="Muted.TLabel").pack(anchor="w", pady=(4, 12))

    def text(self, msg: str, style: str = "TLabel", pady=(0, 6)) -> ttk.Label:
        l = ttk.Label(self, text=msg, wraplength=WRAP, justify="left", style=style)
        l.pack(anchor="w", pady=pady)
        return l

    def reveal_button(self, parent, entry: ttk.Entry, mask: str = "•") -> ttk.Button:
        """Botón «Mostrar/Ocultar» para campos secretos."""
        def toggle():
            hidden = entry.cget("show") != ""
            entry.config(show="" if hidden else mask)
            btn.config(text="Ocultar" if hidden else "Mostrar")
        btn = ttk.Button(parent, text="Mostrar", style="Small.TButton", command=toggle)
        btn.pack(side="left", padx=(6, 0))
        return btn

    def row(self, label: str, name: str, show: str = "", width: int = 38, help: str = "",
            reveal: bool = False) -> ttk.Entry:
        f = ttk.Frame(self)
        f.pack(anchor="w", fill="x", pady=(5, 0 if help else 3))
        ttk.Label(f, text=label, width=22).pack(side="left")
        e = ttk.Entry(f, textvariable=self.app.v[name], show=show, width=width)
        e.pack(side="left")
        if reveal:
            self.reveal_button(f, e, show or "•")
        if help:
            ttk.Label(self, text=help, style="Muted.TLabel").pack(anchor="w", padx=(176, 0), pady=(2, 4))
        return e

    def status(self) -> Banner:
        b = Banner(self)
        b.pack(fill="x", pady=(8, 0))
        return b

    def set_status(self, label, msg: str, kind: str = "info") -> None:
        if isinstance(label, Banner):
            label.show(msg, kind)
        else:                               # ttk.Label (filas de la comprobación inicial)
            label.config(text=msg, foreground={"ok": OK, "bad": BAD, "warn": WARN}.get(kind, MUTED))


def make_tree(parent, columns, specs, height: int) -> ttk.Treeview:
    """Treeview con barra de desplazamiento. specs: [(columna, título, ancho)]."""
    box = ttk.Frame(parent)
    box.pack(fill="x")
    tree = ttk.Treeview(box, columns=columns, show="headings", height=height, selectmode="browse")
    for col, title, width in specs:
        tree.heading(col, text=title)
        tree.column(col, width=width, anchor="center" if width <= 60 else "w", stretch=width > 60)
    sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="x", expand=True)
    sb.pack(side="right", fill="y")
    return tree


# ------------------------------------------------------------------- pasos
class WelcomeStep(Step):
    title, short = "Bienvenida", "Bienvenida"

    def build(self):
        self.head("Bienvenido a AutoTrade",
                  "Este asistente te guía, paso a paso, para copiar las señales de oro (XAUUSD) de un canal de "
                  "Telegram a tu cuenta de MetaTrader 5. Tardarás unos 10 minutos.")
        box = ttk.LabelFrame(self, text=" Antes de empezar ", padding=10)
        box.pack(fill="x", pady=4)
        for t in ("• Empezarás en modo SIMULACIÓN: el programa decide y anota lo que haría, pero NO abre operaciones reales.",
                  "• Usa primero una cuenta DEMO de tu broker. Pasa a dinero real solo cuando estés conforme.",
                  "• Ninguna señal garantiza ganancias: muchos canales pierden dinero. Más adelante podrás medir si el tuyo sirve.",
                  "• Necesitas tener MetaTrader 5 instalado y una cuenta iniciada en él."):
            ttk.Label(box, text=t, wraplength=WRAP - 30, justify="left").pack(anchor="w", pady=2)
        ttk.Label(self, text="Comprobación de tu equipo", style="H2.TLabel").pack(anchor="w", pady=(14, 4))
        self.rows: Dict[str, ttk.Label] = {}
        for key, name in (("os", "Sistema operativo"), ("mt5", "MetaTrader 5 instalado"),
                          ("net", "Conexión a Telegram"), ("lib", "Componentes del programa")):
            f = ttk.Frame(self)
            f.pack(anchor="w", fill="x", pady=2)
            ttk.Label(f, text=name, width=28).pack(side="left")
            self.rows[key] = ttk.Label(f, text="…", style="Muted.TLabel", wraplength=380, justify="left")
            self.rows[key].pack(side="left")
        ttk.Button(self, text="Volver a comprobar", command=self.run_checks).pack(anchor="w", pady=10)

    def on_show(self):
        self.run_checks()

    def run_checks(self):
        def work():
            res = {}
            res["os"] = (("ok", "Windows") if IS_WINDOWS else
                         ("warn", f"{sys.platform}: puedes configurar, pero operar en MT5 requiere Windows"))
            found = sl.detect_mt5_paths()
            res["mt5"] = (("ok", found[0]) if found else
                          ("warn", "No lo encontré; podrás indicar su ubicación en el paso de MetaTrader 5"))
            try:
                socket.create_connection(("my.telegram.org", 443), timeout=6).close()
                res["net"] = ("ok", "Conectado")
            except OSError:
                res["net"] = ("bad", "Sin conexión a Telegram. Revisa tu internet.")
            from .selfcheck import check_imports
            results = check_imports()
            bad = [r for r in results if r[3] and not r[1]]
            soft = [r for r in results if not r[3] and not r[1]]
            if bad:
                res["lib"] = ("bad", "Faltan: " + "; ".join(f"{n} ({d})" for n, _, d, _ in bad))
            elif soft:
                res["lib"] = ("warn", "Solo disponible en Windows: " + ", ".join(n for n, *_ in soft))
            else:
                res["lib"] = ("ok", "Completos")
            return res

        def show(res):
            for k, (kind, msg) in res.items():
                self.set_status(self.rows[k], {"ok": "✔ ", "warn": "⚠ ", "bad": "✖ "}[kind] + msg, kind)
        for l in self.rows.values():
            l.config(text="…", foreground=MUTED)
        self.app.bg(work, show)

    def done(self):
        return True


class TelegramStep(Step):
    title, short = "Conectar Telegram", "Telegram"

    def build(self):
        self.head("Conectar tu cuenta de Telegram",
                  "El programa necesita leer el canal con tu propia cuenta (no hace falta ser administrador, solo "
                  "estar unido al canal).")
        self.text("1. Pulsa «Abrir my.telegram.org» e inicia sesión con tu teléfono.\n"
                  "2. Entra en «API development tools» y crea una aplicación (el nombre da igual).\n"
                  "3. Copia aquí el api_id y el api_hash que te muestra.")
        ttk.Button(self, text="Abrir my.telegram.org", command=lambda: webbrowser.open("https://my.telegram.org")).pack(anchor="w", pady=4)
        self.row("api_id", "api_id", width=20)
        self.row("api_hash", "api_hash", show="•", width=40, reveal=True)
        self.row("Tu teléfono", "phone", width=20, help="con + y código de país, ej. +50499998888")
        self.send_btn = ttk.Button(self, text="Enviar código a mi Telegram", style="Accent.TButton", command=self.send_code)
        self.send_btn.pack(anchor="w", pady=(10, 4))
        self.code_frame = ttk.Frame(self)
        ttk.Label(self.code_frame, text="Código recibido en Telegram:", width=28).pack(side="left")
        self.code = tk.StringVar()
        ttk.Entry(self.code_frame, textvariable=self.code, width=12).pack(side="left")
        self.code_btn = ttk.Button(self.code_frame, text="Confirmar", command=self.confirm_code)
        self.code_btn.pack(side="left", padx=6)
        self.pw_frame = ttk.Frame(self)
        ttk.Label(self.pw_frame, text="Contraseña de dos pasos:", width=28).pack(side="left")
        self.pw = tk.StringVar()
        ttk.Entry(self.pw_frame, textvariable=self.pw, show="•", width=20).pack(side="left")
        ttk.Button(self.pw_frame, text="Entrar", command=self.confirm_password).pack(side="left", padx=6)
        self.st = self.status()

    def on_show(self):
        a = self.app
        if a.tg_user:
            self.set_status(self.st, f"✔ Conectado como {a.tg_user}", "ok")
            return
        session = data_dir() / f"{a.settings.session_name}.session"
        if session.exists() and not sl.validate_telegram(a.sync_settings()) and not a.bot_running():
            self.set_status(self.st, "Comprobando la sesión guardada…")
            self.connect_existing()

    def connect_existing(self):
        def work():
            h = self.app.make_tg()
            return h.connect() and h.me_name()
        def ok(name):
            if name:
                self.app.tg_user = name
                self.set_status(self.st, f"✔ Sesión guardada válida: conectado como {name}", "ok")
            else:
                self.set_status(self.st, "Introduce tus datos y pulsa «Enviar código».")
        self.app.bg(work, ok, lambda e: self.set_status(self.st, str(e), "warn"))

    def send_code(self):
        errs = sl.validate_telegram(self.app.sync_settings())
        if errs:
            return messagebox.showwarning("Revisa los datos", "\n".join(errs))
        self.code_frame.pack_forget(); self.pw_frame.pack_forget()
        self.set_status(self.st, "Conectando con Telegram…")
        self.send_btn.state(["disabled"])
        phone = self.app.settings.phone

        def work():
            self.app.close_tg()
            h = self.app.make_tg()
            if h.connect():
                return ("already", h.me_name())
            h.request_code(phone)
            return ("code", "")

        def ok(res):
            self.send_btn.state(["!disabled"])
            if res[0] == "already":
                self.app.tg_user = res[1]
                self.set_status(self.st, f"✔ Ya estabas conectado como {res[1]}", "ok")
            else:
                self.code_frame.pack(anchor="w", pady=4, before=self.st)
                self.set_status(self.st, "Te enviamos un código a tu app de Telegram (mensaje de «Telegram»). "
                                         "Escríbelo arriba y pulsa «Confirmar».")

        def fail(e):
            self.send_btn.state(["!disabled"])
            self.set_status(self.st, f"✖ {e}", "bad")
        self.app.bg(work, ok, fail)

    def _after_signin(self, res):
        if res == "password":
            self.pw_frame.pack(anchor="w", pady=4, before=self.st)
            self.set_status(self.st, "Tu cuenta tiene verificación en dos pasos: escribe tu contraseña de Telegram.")
        else:
            self.app.bg(self.app.tg.me_name, self._logged)

    def _logged(self, name):
        self.app.tg_user = name
        self.code_frame.pack_forget(); self.pw_frame.pack_forget()
        self.set_status(self.st, f"✔ Conectado como {name}", "ok")

    def confirm_code(self):
        code = self.code.get().strip()
        if not code:
            return messagebox.showinfo("Falta el código", "Escribe el código que te llegó a Telegram.")
        self.app.bg(lambda: self.app.tg.sign_in(code), self._after_signin,
                    lambda e: self.set_status(self.st, f"✖ {e}", "bad"))

    def confirm_password(self):
        self.app.bg(lambda: self.app.tg.sign_in_password(self.pw.get()),
                    lambda _: self.app.bg(self.app.tg.me_name, self._logged),
                    lambda e: self.set_status(self.st, f"✖ {e}", "bad"))

    def check(self):
        return [] if self.app.tg_user else ["Conecta tu cuenta de Telegram antes de continuar (pulsa «Enviar código»)."]

    def done(self):
        return bool(self.app.tg_user)


class ChannelStep(Step):
    title, short = "Elegir los canales", "Canales"
    ALL = "Todos mis canales y grupos"

    def build(self):
        self.head("¿De qué canales copiamos las señales?",
                  "Marca uno o varios canales. Usa tus carpetas de Telegram para encontrarlos más rápido. "
                  "Cada canal puede tener su propio lote.")
        self.dialogs: List[dict] = []
        self.folders: List[dict] = []
        self.visible: List[dict] = []
        self.folder_by_label: Dict[str, dict] = {}
        self.sel_num: Optional[int] = None
        self._loading = False

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Carpeta").pack(side="left")
        self.folder_var = tk.StringVar(value=self.ALL)
        self.folder_cb = ttk.Combobox(bar, textvariable=self.folder_var, state="readonly", width=24, values=[self.ALL])
        self.folder_cb.pack(side="left", padx=(8, 14))
        self.folder_cb.bind("<<ComboboxSelected>>", lambda e: self.refill())
        ttk.Label(bar, text="Buscar").pack(side="left")
        self.q = tk.StringVar()
        self.q.trace_add("write", lambda *_: self.refill())
        ttk.Entry(bar, textvariable=self.q, width=14).pack(side="left", padx=(8, 0))
        self.load_btn = ttk.Button(bar, text="Cargar mis canales", style="Accent.TButton", command=self.load)
        self.load_btn.pack(side="right")

        self.avail = make_tree(self, ("sel", "name", "kind", "user"),
                               (("sel", "", 54), ("name", "Canal o grupo", 332), ("kind", "Tipo", 70),
                                ("user", "@usuario", 150)), height=5)
        self.avail.bind("<Button-1>", lambda e: self.on_click(self.avail, e, self.toggle_iid))
        self.avail.bind("<Double-1>", lambda e: self.on_double(self.avail, e, self.toggle_iid))

        act = ttk.Frame(self)
        act.pack(fill="x", pady=(8, 2))
        self.add_all_btn = ttk.Button(act, text="Agregar todos los de esta vista", command=self.add_visible)
        self.add_all_btn.pack(side="left")
        ttk.Button(act, text="Agregar", command=self.use_manual).pack(side="right")
        self.manual = tk.StringVar()
        ttk.Entry(act, textvariable=self.manual, width=18).pack(side="right", padx=(6, 6))
        ttk.Label(act, text="o escribe un @usuario:", style="Muted.TLabel").pack(side="right")

        ttk.Label(self, text="Canales elegidos", style="H2.TLabel").pack(anchor="w", pady=(10, 4))
        self.chosen = make_tree(self, ("on", "name", "lot", "tps"),
                                (("on", "Activo", 74), ("name", "Canal", 330), ("lot", "Lote total", 100),
                                 ("tps", "TPs", 100)), height=3)
        self.chosen.bind("<Button-1>", lambda e: self.on_click(self.chosen, e, self.toggle_enabled))
        self.chosen.bind("<<TreeviewSelect>>", self.on_pick)

        ed = ttk.Frame(self)
        ed.pack(fill="x", pady=(8, 0))
        ttk.Label(ed, text="Ajustes del canal marcado:", style="Muted.TLabel").pack(side="left")
        ttk.Label(ed, text="Lote").pack(side="left", padx=(12, 4))
        self.ed_lot = tk.StringVar()
        ttk.Entry(ed, textvariable=self.ed_lot, width=7).pack(side="left")
        self.ed_lot.trace_add("write", lambda *_: self.apply_edit())
        ttk.Label(ed, text="TPs").pack(side="left", padx=(12, 4))
        self.ed_tps = tk.StringVar(value="General")
        cb = ttk.Combobox(ed, textvariable=self.ed_tps, values=["General", "1", "2", "3"], state="readonly", width=9)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.apply_edit())
        ttk.Button(ed, text="Quitar canal", command=self.remove_selected).pack(side="right")
        ttk.Label(self, text="Lote vacío o TPs «General» = se usan los ajustes de «Cómo operar».",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        self.st = self.status()

    # ---- datos
    def channels(self):
        return self.app.settings.channels

    def mark(self, d: dict) -> str:
        return "☑" if any(c.ref == channel_ref(d) for c in self.channels()) else "☐"

    def refill(self):
        label, q = self.folder_var.get(), self.q.get().strip().lower()
        base = self.dialogs
        folder = self.folder_by_label.get(label)
        if folder is not None:
            members = set(folder["members"])
            base = [d for d in self.dialogs if d["id"] in members]
        self.visible = [d for d in base if q in d["name"].lower() or q in d["username"].lower()]
        self.avail.delete(*self.avail.get_children())
        for i, d in enumerate(self.visible):
            self.avail.insert("", "end", iid=str(i), values=(
                self.mark(d), d["name"], d["kind"], ("@" + d["username"]) if d["username"] else ""))

    def refill_chosen(self, keep: Optional[int] = None):
        self.chosen.delete(*self.chosen.get_children())
        for c in self.channels():
            self.chosen.insert("", "end", iid=str(c.num), values=self.chosen_values(c))
        if keep is not None and self.chosen.exists(str(keep)):
            self.chosen.selection_set(str(keep))

    @staticmethod
    def chosen_values(c: ChannelCfg):
        return ("☑" if c.enabled else "☐", c.title or c.ref, f"{c.lot_size:g}" if c.lot_size else "General",
                str(c.max_tps) if c.max_tps else "General")

    def refresh_all(self):
        keep = self.sel_num
        self.refill()
        self.refill_chosen(keep)
        chans = self.channels()
        active = [c for c in chans if c.enabled]
        if not active:
            self.set_status(self.st, "Marca al menos un canal de la lista para continuar.", "warn")
        else:
            names = ", ".join(c.title or c.ref for c in active)
            extra = ""
            if len(active) > 3:
                extra = (f"\nCon tantos canales, recuerda que el máximo de posiciones abiertas "
                         f"({self.app.settings.max_open_positions}) se comparte entre todos.")
            self.set_status(self.st, f"✔ {len(active)} canal(es) activo(s): {names}{extra}", "ok")

    # ---- clics
    def on_click(self, tree, ev, action):
        """Un clic en la columna ☐/☑ marca o desmarca."""
        if tree.identify("region", ev.x, ev.y) != "cell" or tree.identify_column(ev.x) != "#1":
            return None
        iid = tree.identify_row(ev.y)
        if iid:
            action(iid)
        return "break"

    def on_double(self, tree, ev, action):
        if tree.identify_column(ev.x) == "#1":
            return
        iid = tree.identify_row(ev.y)
        if iid:
            action(iid)

    def toggle_iid(self, iid: str):
        d = self.visible[int(iid)]
        ref, chans = channel_ref(d), self.channels()
        existing = next((c for c in chans if c.ref == ref), None)
        if existing:
            chans.remove(existing)
            if self.sel_num == existing.num:
                self.sel_num = None
        else:
            chans.append(ChannelCfg(next_num(chans), ref, d["name"]))
        self.refresh_all()

    def toggle_enabled(self, iid: str):
        c = self.by_num(int(iid))
        if c:
            c.enabled = not c.enabled
            self.refresh_all()

    def by_num(self, num: Optional[int]) -> Optional[ChannelCfg]:
        return next((c for c in self.channels() if c.num == num), None)

    def add_visible(self):
        chans = self.channels()
        have = {c.ref for c in chans}
        new = [d for d in self.visible if channel_ref(d) not in have]
        if not new:
            return self.set_status(self.st, "Todos los de esta vista ya están elegidos.", "info")
        if len(new) > 5 and not messagebox.askyesno(
                "Agregar muchos canales",
                f"Vas a agregar {len(new)} canales. Cada uno puede abrir operaciones.\n¿Quieres agregarlos todos?"):
            return
        for d in new:
            chans.append(ChannelCfg(next_num(chans), channel_ref(d), d["name"]))
        self.refresh_all()

    def use_manual(self):
        ref = self.manual.get().strip()
        if not ref:
            return
        if ref.lstrip("-").isdigit() is False and not ref.startswith("@"):
            ref = "@" + ref
        chans = self.channels()
        if any(c.ref == ref for c in chans):
            return self.set_status(self.st, f"{ref} ya estaba en la lista.", "info")
        chans.append(ChannelCfg(next_num(chans), ref, ref))
        self.manual.set("")
        self.refresh_all()

    # ---- ajustes por canal
    def on_pick(self, _=None):
        sel = self.chosen.selection()
        self.sel_num = int(sel[0]) if sel else None
        c = self.by_num(self.sel_num)
        self._loading = True
        self.ed_lot.set(f"{c.lot_size:g}" if c and c.lot_size else "")
        self.ed_tps.set(str(c.max_tps) if c and c.max_tps else "General")
        self._loading = False

    def apply_edit(self):
        c = self.by_num(self.sel_num)
        if self._loading or c is None:
            return
        raw = self.ed_lot.get().strip().replace(",", ".")
        try:
            c.lot_size = float(raw) if raw else 0.0
        except ValueError:
            return                              # escribiendo (ej. «0.»): se valida al avanzar
        tps = self.ed_tps.get()
        c.max_tps = int(tps) if tps.isdigit() else 0
        if self.chosen.exists(str(c.num)):
            self.chosen.item(str(c.num), values=self.chosen_values(c))

    def remove_selected(self):
        c = self.by_num(self.sel_num)
        if c is None:
            return self.set_status(self.st, "Marca primero un canal en «Canales elegidos».", "warn")
        self.channels().remove(c)
        self.sel_num = None
        self.on_pick()
        self.refresh_all()

    # ---- Telegram
    def on_show(self):
        self.refresh_all()
        if self.app.tg_user and self.app.tg and not self.dialogs:
            self.load()

    def load(self):
        if not self.app.tg:
            return self.set_status(self.st, "Primero conecta Telegram en el paso anterior.", "warn")
        self.load_btn.state(["disabled"])
        self.set_status(self.st, "Cargando tus canales y carpetas…")

        def ok(ov):
            self.load_btn.state(["!disabled"])
            self.dialogs, self.folders = ov["channels"], ov["folders"]
            self.folder_by_label = {}
            for f in self.folders:
                label, n = f["title"], 2
                while label in self.folder_by_label or label == self.ALL:
                    label, n = f"{f['title']} ({n})", n + 1
                self.folder_by_label[label] = f
            self.folder_cb["values"] = [self.ALL] + list(self.folder_by_label)
            self.folder_var.set(self.ALL)
            self.refresh_all()
            note = ""
            if ov.get("folders_error"):
                note = "\nNo pude leer tus carpetas de Telegram; se muestran todos tus canales."
            elif self.folders:
                note = f"\n{len(self.folders)} carpeta(s) disponibles en el desplegable «Carpeta»."
            if not self.dialogs:
                self.set_status(self.st, "No encontré canales. ¿Estás unido al canal de señales?", "warn")
            elif note:
                self.st.show(self.st.cget("text") + note, "ok" if "No pude" not in note else "warn")
        self.app.bg(self.app.tg.overview, ok,
                    lambda e: (self.load_btn.state(["!disabled"]), self.set_status(self.st, f"✖ {e}", "bad")))

    def check(self):
        return sl.validate_channel(self.app.settings)

    def done(self):
        return any(c.enabled for c in self.app.settings.channels)


class MT5Step(Step):
    title, short = "Conectar MetaTrader 5", "MetaTrader 5"

    def build(self):
        self.head("Conectar MetaTrader 5",
                  "Abre MetaTrader 5 e inicia sesión en tu cuenta (mejor una cuenta DEMO para empezar). "
                  "Después pulsa «Probar conexión».")
        f = ttk.Frame(self)
        f.pack(anchor="w", fill="x", pady=3)
        ttk.Label(f, text="Ubicación de MT5", width=22).pack(side="left")
        self.path_cb = ttk.Combobox(f, textvariable=self.app.v["mt5_path"], width=42)
        self.path_cb.pack(side="left")
        ttk.Button(f, text="Examinar…", command=self.browse).pack(side="left", padx=6)
        box = ttk.LabelFrame(self, text=" Datos de la cuenta (opcional) ", padding=8)
        box.pack(fill="x", pady=8)
        ttk.Label(box, text="Déjalos vacíos para usar la cuenta que tengas abierta en MT5.",
                  style="Muted.TLabel").pack(anchor="w")
        for label, name, show in (("Número de cuenta", "mt5_login", ""), ("Contraseña", "mt5_password", "•"),
                                  ("Servidor", "mt5_server", "")):
            r = ttk.Frame(box)
            r.pack(anchor="w", pady=2)
            ttk.Label(r, text=label, width=20).pack(side="left")
            e = ttk.Entry(r, textvariable=self.app.v[name], show=show, width=30)
            e.pack(side="left")
            if show:
                self.reveal_button(r, e, show)
        r = ttk.Frame(self)
        r.pack(anchor="w", pady=4)
        ttk.Label(r, text="Símbolo del oro", width=22).pack(side="left")
        self.sym_cb = ttk.Combobox(r, textvariable=self.app.v["symbol"], width=20)
        self.sym_cb.pack(side="left")
        ttk.Label(self, text="Debe ser el nombre exacto que usa tu broker: XAUUSD, XAUUSDm, GOLD…",
                  style="Muted.TLabel").pack(anchor="w")
        self.btn = ttk.Button(self, text="Probar conexión", style="Accent.TButton", command=self.probe)
        self.btn.pack(anchor="w", pady=10)
        self.st = self.status()

    def on_show(self):
        paths = sl.detect_mt5_paths()
        self.path_cb["values"] = paths
        if paths and not self.app.v["mt5_path"].get():
            self.app.v["mt5_path"].set(paths[0])
        if self.app.mt5_info and self.app.mt5_info.get("ok"):
            self.show_result(self.app.mt5_info)

    def browse(self):
        p = filedialog.askopenfilename(title="Elige terminal64.exe", filetypes=[("MetaTrader 5", "terminal64.exe"), ("Todos", "*.*")])
        if p:
            self.app.v["mt5_path"].set(p)

    def probe(self):
        s = self.app.sync_settings()
        errs = sl.validate_mt5(s)
        if errs:
            return messagebox.showwarning("Revisa los datos", "\n".join(errs))
        self.set_status(self.st, "Conectando con MetaTrader 5… (puede tardar unos segundos)")
        self.btn.state(["disabled"])
        from . import mt5_helper

        def done(res):
            self.btn.state(["!disabled"])
            self.app.mt5_info = res
            self.show_result(res)
        self.app.bg(lambda: mt5_helper.probe(s.mt5_path, s.mt5_login, s.mt5_password, s.mt5_server, s.symbol), done,
                    lambda e: (self.btn.state(["!disabled"]), self.set_status(self.st, f"✖ {e}", "bad")))

    def show_result(self, res: dict):
        if not res["ok"]:
            return self.set_status(self.st, "✖ " + res["error"], "bad")
        a = res["account"]
        lines = [f"✔ Cuenta {a['login']} en {a['server']} ({a['mode']}) — saldo {a['balance']:,.2f} {a['currency']}"]
        kind = "ok"
        if a["mode"] == "REAL":
            lines.append("⚠ Es una cuenta REAL. Para empezar te recomendamos una cuenta DEMO.")
            kind = "warn"
        if not a["algo_trading"]:
            lines.append("⚠ El botón «Algo Trading» de MT5 está apagado: actívalo para que se puedan enviar órdenes.")
            kind = "warn"
        if res["gold_symbols"]:
            self.sym_cb["values"] = res["gold_symbols"]
            if not res["symbol_ok"]:
                self.app.v["symbol"].set(res["gold_symbols"][0])
                lines.append(f"⚠ El símbolo escrito no existe en tu broker; elegí «{res['gold_symbols'][0]}». Revísalo.")
                kind = "warn"
            else:
                lines.append(f"✔ El símbolo «{self.app.v['symbol'].get()}» existe en tu broker.")
        elif not res["symbol_ok"]:
            lines.append("⚠ No encontré símbolos de oro; escribe el nombre exacto del símbolo.")
            kind = "warn"
        self.set_status(self.st, "\n".join(lines), kind)

    def check(self):
        errs = sl.validate_mt5(self.app.settings)
        if not errs and not (self.app.mt5_info and self.app.mt5_info.get("ok")):
            if not messagebox.askyesno("MetaTrader 5 sin probar",
                                       "Todavía no has probado la conexión con MetaTrader 5.\n¿Quieres continuar de todos modos?"):
                return ["Pulsa «Probar conexión» para comprobar MetaTrader 5."]
        return errs

    def done(self):
        return bool(self.app.mt5_info and self.app.mt5_info.get("ok"))


class TradingStep(Step):
    title, short = "Cómo operar", "Operativa"

    def build(self):
        self.head("¿Cómo quieres operar las señales?")
        v = self.app.v
        box = ttk.LabelFrame(self, text=" Modo ", padding=8)
        box.pack(fill="x", pady=4)
        ttk.Radiobutton(box, text="SIMULACIÓN (recomendado al empezar): decide y anota, pero NO opera", variable=v["dry_run"], value=True).pack(anchor="w")
        ttk.Radiobutton(box, text="OPERAR DE VERDAD: envía órdenes a tu cuenta de MetaTrader 5", variable=v["dry_run"], value=False).pack(anchor="w")
        r = ttk.Frame(self); r.pack(anchor="w", pady=6)
        ttk.Label(r, text="Lote total por señal", width=24).pack(side="left")
        ttk.Spinbox(r, from_=0.01, to=100, increment=0.01, textvariable=v["lot_size"], width=8, format="%.2f").pack(side="left")
        ttk.Label(self, text="El lote total se reparte entre los TPs: 0.02 con 2 TPs = dos órdenes de 0.01.",
                  style="Muted.TLabel").pack(anchor="w")
        r = ttk.Frame(self); r.pack(anchor="w", pady=4)
        ttk.Label(r, text="Órdenes por señal (TPs)", width=24).pack(side="left")
        for n in (1, 2, 3):
            ttk.Radiobutton(r, text=str(n), variable=v["max_tps"], value=str(n)).pack(side="left", padx=6)
        ttk.Label(r, text="una orden por cada take profit del canal", style="Muted.TLabel").pack(side="left", padx=8)
        r = ttk.Frame(self); r.pack(anchor="w", pady=4)
        ttk.Label(r, text="Máx. posiciones abiertas", width=24).pack(side="left")
        ttk.Spinbox(r, from_=1, to=50, textvariable=v["max_open_positions"], width=8).pack(side="left")
        ttk.Checkbutton(self, text="Obedecer los mensajes del canal («mover a BE», «cerrar todo», «cerrar mitad»…)",
                        variable=v["enable_commands"]).pack(anchor="w", pady=6)
        box = ttk.LabelFrame(self, text=" Si el SL/TP queda más cerca de lo que permite tu broker ", padding=8)
        box.pack(fill="x", pady=6)
        ttk.Radiobutton(box, text="No operar esa señal y avisarme (recomendado)", variable=v["stops_policy"], value="skip").pack(anchor="w")
        ttk.Radiobutton(box, text="Alejar el SL/TP hasta el mínimo (cambia el riesgo de la señal)", variable=v["stops_policy"], value="adjust").pack(anchor="w")

    def check(self):
        return sl.validate_trading(self.app.sync_settings())

    def done(self):
        return not sl.validate_trading(self.app.settings)


class ScheduleStep(Step):
    title, short = "Horario", "Horario"

    def build(self):
        self.head("¿En qué días y horas operar?",
                  "Solo se operan las señales ENVIADAS dentro de este horario. Los mensajes de gestión (mover a BE, "
                  "cerrar) siempre se obedecen. Si no estás seguro, déjalo desactivado: más adelante el análisis del "
                  "canal te dirá si un horario ayuda de verdad.")
        v = self.app.v
        ttk.Checkbutton(self, text="Operar solo en ciertos días y horas", variable=v["filter_enabled"], command=self.toggle).pack(anchor="w", pady=6)
        self.box = ttk.LabelFrame(self, text=" Horario permitido ", padding=10)
        self.box.pack(fill="x", pady=4)
        r = ttk.Frame(self.box); r.pack(anchor="w", pady=4)
        ttk.Label(r, text="Días", width=12).pack(side="left")
        self.days = {}
        for i, (_, name) in enumerate(DAYS):
            var = tk.BooleanVar(value=i < 5)
            self.days[i] = var
            ttk.Checkbutton(r, text=name, variable=var).pack(side="left", padx=3)
        r = ttk.Frame(self.box); r.pack(anchor="w", pady=4)
        ttk.Label(r, text="Horas", width=12).pack(side="left")
        ttk.Entry(r, textvariable=v["trading_hours"], width=30).pack(side="left")
        ttk.Label(r, text="ej. 08:00-12:00, 14:00-18:00", style="Muted.TLabel").pack(side="left", padx=8)
        r = ttk.Frame(self.box); r.pack(anchor="w", pady=4)
        ttk.Label(r, text="Zona horaria", width=12).pack(side="left")
        ttk.Combobox(r, textvariable=v["tz_offset_hours"], values=TZ_CHOICES, width=6).pack(side="left")
        ttk.Label(r, text="horas respecto a UTC (Honduras = -6)", style="Muted.TLabel").pack(side="left", padx=8)
        self.st = self.status()

    def on_show(self):
        sel = parse_days(self.app.settings.trading_days) if self.app.settings.trading_days else set(range(5))
        for i, var in self.days.items():
            var.set(i in sel)
        self.toggle()

    def toggle(self):
        state = "normal" if self.app.v["filter_enabled"].get() else "disabled"
        def walk(w):
            for c in w.winfo_children():
                try:
                    c.configure(state=state)
                except tk.TclError:
                    pass
                walk(c)
        walk(self.box)

    def leave(self):
        self.commit_days()

    def commit_days(self):
        self.app.v["trading_days"].set(days_to_text({i for i, v in self.days.items() if v.get()}))

    def check(self):
        self.commit_days()
        return sl.validate_trading(self.app.sync_settings())

    def done(self):
        return True


class ProtectionsStep(Step):
    title, short = "Protecciones", "Protecciones"

    def build(self):
        self.head("Protecciones y seguridad",
                  "Reglas para que una mala racha, una noticia o un spread disparado no te cuesten de más. "
                  "Todo es opcional: 0 o «desactivado» significa sin límite.")
        v = self.app.v

        def line(parent, label, name, hint, width=8, label_w=26):
            r = ttk.Frame(parent)
            r.pack(anchor="w", fill="x", pady=3)
            ttk.Label(r, text=label, width=label_w).pack(side="left")
            ttk.Entry(r, textvariable=v[name], width=width).pack(side="left")
            ttk.Label(r, text=hint, style="Muted.TLabel").pack(side="left", padx=8)

        box = ttk.LabelFrame(self, text=" Límites del día ", padding=(12, 6))
        box.pack(fill="x", pady=(0, 8))
        line(box, "Pérdida máxima por día", "daily_loss_limit_pct", "% del saldo. Al llegar, no se abren más señales hoy.")
        line(box, "Máximo de señales por día", "max_signals_per_day", "señales (0 = sin límite)")
        ttk.Checkbutton(box, text="Al llegar al límite de pérdida, cerrar también lo que esté abierto",
                        variable=v["close_on_limit"]).pack(anchor="w", pady=(2, 0))

        box = ttk.LabelFrame(self, text=" Condiciones del mercado ", padding=(12, 6))
        box.pack(fill="x", pady=(0, 8))
        line(box, "Spread máximo", "max_spread_usd", "USD (0 = sin filtro). Con spread mayor no se abre la señal.")

        box = ttk.LabelFrame(self, text=" Noticias importantes ", padding=(12, 6))
        box.pack(fill="x", pady=(0, 8))
        for text, val in (("Desactivado", "off"), ("Solo las horas que yo indique", "manual"),
                          ("Automático (calendario económico) + mis horas", "auto")):
            ttk.Radiobutton(box, text=text, variable=v["news_mode"], value=val).pack(anchor="w")
        r = ttk.Frame(box)
        r.pack(anchor="w", pady=(6, 2))
        ttk.Label(r, text="No abrir señales").pack(side="left")
        ttk.Spinbox(r, from_=0, to=240, textvariable=v["news_minutes_before"], width=5).pack(side="left", padx=6)
        ttk.Label(r, text="min antes y").pack(side="left")
        ttk.Spinbox(r, from_=0, to=240, textvariable=v["news_minutes_after"], width=5).pack(side="left", padx=6)
        ttk.Label(r, text="min después de cada noticia").pack(side="left")
        line(box, "Mis noticias", "news_manual", "", width=44, label_w=14)
        ttk.Label(box, text="Ej.: Fri 08:30 NFP; Wed 14:00 FOMC   (días en inglés, hora en tu zona del paso «Horario»)",
                  style="Muted.TLabel").pack(anchor="w")
        line(box, "Monedas (automático)", "news_currencies", "ej. USD, EUR", width=14, label_w=20)

        box = ttk.LabelFrame(self, text=" Gestión automática ", padding=(12, 6))
        box.pack(fill="x")
        ttk.Checkbutton(box, text="Mover a break-even automáticamente cuando se cumpla el TP1 de una señal",
                        variable=v["auto_be"]).pack(anchor="w")

    def check(self):
        return sl.validate_protections(self.app.sync_settings())

    def done(self):
        return not sl.validate_protections(self.app.settings)


class AlertsStep(Step):
    title, short = "Avisos", "Avisos"

    def build(self):
        self.head("Avisos en tu Telegram",
                  "El programa te escribe a tu chat «Mensajes guardados» cuando arranca, abre o ignora una señal, "
                  "falla una orden o se cae MetaTrader 5. Así te enteras desde el celular.")
        v = self.app.v
        ttk.Checkbutton(self, text="Enviarme avisos por Telegram", variable=v["alerts_enabled"]).pack(anchor="w", pady=3)
        ttk.Checkbutton(self, text="Avisar de cada señal operada y de los comandos del canal", variable=v["notify_signals"]).pack(anchor="w", padx=20)
        ttk.Checkbutton(self, text="Avisar de las señales ignoradas (fuera de horario, SL inválido…)", variable=v["notify_skips"]).pack(anchor="w", padx=20)
        self.btn = ttk.Button(self, text="Enviar un aviso de prueba", command=self.test)
        self.btn.pack(anchor="w", pady=10)
        self.st = self.status()
        box = ttk.LabelFrame(self, text=" Opcional: aviso si el equipo se apaga (latido) ", padding=8)
        box.pack(fill="x", pady=10)
        ttk.Label(box, text="Un equipo apagado no puede avisar de que está apagado. Crea un chequeo gratuito en un servicio "
                            "como healthchecks.io, pega aquí su dirección y configúralo para avisarte si deja de recibir señales de vida.",
                  wraplength=WRAP - 30, justify="left", style="Muted.TLabel").pack(anchor="w", pady=(0, 4))
        r = ttk.Frame(box); r.pack(anchor="w", fill="x")
        ttk.Label(r, text="Dirección:", width=10).pack(side="left")
        ttk.Entry(r, textvariable=v["heartbeat_url"], width=52).pack(side="left")

    def test(self):
        if not self.app.tg_user:
            return self.set_status(self.st, "Conecta Telegram primero (paso «Telegram»).", "warn")
        self.set_status(self.st, "Enviando…")

        def work():
            h = self.app.make_tg()
            h.connect()
            h.send_to_me("✅ Prueba de AutoTrade: los avisos funcionan. Si ves este mensaje en «Mensajes guardados», todo está bien.")
        self.app.bg(work, lambda _: self.set_status(self.st, "✔ Enviado. Revisa «Mensajes guardados» en tu Telegram.", "ok"),
                    lambda e: self.set_status(self.st, f"✖ {e}", "bad"))

    def check(self):
        return sl.validate_alerts(self.app.sync_settings())

    def done(self):
        return True


class TestStep(Step):
    title, short = "Probar un mensaje", "Probar mensaje"

    def build(self):
        self.head("Prueba cómo se interpreta una señal",
                  "Pega aquí un mensaje real del canal. Verás qué entendería el programa y qué órdenes abriría, sin operar nada.")
        self.box = scrolledtext.ScrolledText(self, height=7, width=70, wrap="word")
        self.box.pack(fill="x", pady=4)
        self.box.insert("1.0", SAMPLE)
        r = ttk.Frame(self); r.pack(anchor="w", pady=6)
        ttk.Label(r, text="Precio actual del oro:").pack(side="left")
        self.price = tk.StringVar()
        ttk.Entry(r, textvariable=self.price, width=10).pack(side="left", padx=6)
        ttk.Label(r, text="(opcional; si lo dejas vacío se usa el centro de la zona de entrada)", style="Muted.TLabel").pack(side="left")
        ttk.Button(self, text="Analizar mensaje", style="Accent.TButton", command=self.analyze).pack(anchor="w", pady=6)
        self.out = scrolledtext.ScrolledText(self, height=11, width=70, wrap="word", state="disabled", background=BAR)
        self.out.pack(fill="both", expand=True, pady=4)

    def write(self, text: str):
        self.out.config(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("1.0", text)
        self.out.config(state="disabled")

    def analyze(self):
        from .backtest import SimConfig, split_lots
        from .parser import parse
        from .planner import Skip, plan_orders
        s = self.app.sync_settings()
        res = parse(self.box.get("1.0", "end"))
        if not res:
            return self.write("Este mensaje se IGNORARÍA: no parece una señal (falta lado BUY/SELL con SL o TP) ni un comando.")
        lines = []
        if res.signal:
            g = res.signal
            zone = ("sin precio de entrada (a mercado)" if g.entry_low is None else
                    f"{g.entry_low}" if g.entry_low == g.entry_high else f"{g.entry_low} - {g.entry_high}")
            kind = {None: "", "limit": " LIMIT", "stop": " STOP"}[g.kind]
            lines += [f"SEÑAL {g.side}{kind}", f"  Entrada: {zone}", f"  Stop loss: {g.sl}",
                      f"  Take profits: {', '.join(map(str, g.tps)) or '—'}", ""]
            try:
                p = float(self.price.get().replace(",", ".")) if self.price.get().strip() else (
                    (g.entry_low + g.entry_high) / 2 if g.entry_low is not None else None)
            except ValueError:
                return self.write("El precio actual debe ser un número, por ejemplo 3914.5")
            if p is None:
                lines.append("Escribe el precio actual del oro para ver qué órdenes se abrirían.")
            else:
                try:
                    plans = plan_orders(g, p - 0.15, p + 0.15, max_tps=s.max_tps)
                    lots = split_lots(s.lot_size, len(plans), SimConfig())
                    lines.append(f"Con el precio en {p} y tu configuración (lote total {s.lot_size}, hasta {s.max_tps} TPs) se abrirían:")
                    for pl, lot in zip(plans, lots):
                        tp = pl.tp if pl.tp else "sin TP"
                        lines.append(f"  • {pl.side} {pl.kind.upper()} {lot:.2f} lotes @ {pl.price:.2f}   SL {pl.sl}   TP {tp}")
                    if len(lots) < len(plans):
                        lines.append("  (el lote total no alcanza para todas las órdenes, se abren menos)")
                except Skip as e:
                    lines.append(f"NO se operaría: {e}")
        for c in res.commands:
            txt = {"breakeven": "mover el SL a break-even", "close": "cerrar todas las operaciones del programa",
                   "cancel": "cancelar órdenes pendientes", "partial": f"cerrar {int((c.value or 0) * 100)}% de las posiciones",
                   "move_sl": f"mover el SL a {c.value}"}[c.kind]
            lines.append(f"COMANDO: {txt}" + ("" if s.enable_commands else "   (desactivado en tu configuración)"))
        self.write("\n".join(lines))

    def done(self):
        return True


class BacktestStep(Step):
    title, short = "Analizar el canal", "Analizar canal"

    def build(self):
        self.head("Analiza si el canal habría sido rentable (recomendado)",
                  "Descarga el historial del canal y simula sus señales sobre los precios pasados del oro. Obtendrás un "
                  "informe con ganancia, riesgo, mejor forma de operarlo y mejor horario. Necesita MetaTrader 5 abierto.")
        v = self.app.v
        r = ttk.Frame(self); r.pack(anchor="w", pady=3)
        ttk.Label(r, text="Canal a analizar", width=24).pack(side="left")
        self.chan = tk.StringVar()
        self.chan_cb = ttk.Combobox(r, textvariable=self.chan, state="readonly", width=36)
        self.chan_cb.pack(side="left")
        self.chan_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_report_button())
        r = ttk.Frame(self); r.pack(anchor="w", pady=3)
        ttk.Label(r, text="Historial a descargar", width=24).pack(side="left")
        self.days = tk.StringVar(value="90")
        ttk.Combobox(r, textvariable=self.days, values=["30", "60", "90", "180"], width=6).pack(side="left")
        ttk.Label(r, text="días (más es mejor: se recomiendan 30+ señales)", style="Muted.TLabel").pack(side="left", padx=8)
        r = ttk.Frame(self); r.pack(anchor="w", pady=3)
        ttk.Label(r, text="Saldo de referencia", width=24).pack(side="left")
        self.balance = tk.StringVar(value="1000")
        ttk.Entry(r, textvariable=self.balance, width=9).pack(side="left")
        ttk.Label(r, text="USD (para calcular % de ganancia y de pérdida máxima)", style="Muted.TLabel").pack(side="left", padx=8)
        r = ttk.Frame(self); r.pack(anchor="w", pady=3)
        ttk.Label(r, text="Hora del servidor MT5", width=24).pack(side="left")
        self.offset = tk.StringVar(value="2")
        ttk.Spinbox(r, from_=-12, to=14, textvariable=self.offset, width=5).pack(side="left")
        self.dst = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="cambia con el horario de verano de EE. UU.", variable=self.dst).pack(side="left", padx=8)
        ttk.Button(r, text="Detectar", command=self.detect).pack(side="left")
        ttk.Label(self, text="Es la diferencia en horas entre la hora del servidor de tu broker y UTC en invierno (muchos brokers: 2). "
                             "Si está mal, el resultado no sirve. «Detectar» lo calcula (con el mercado abierto).",
                  wraplength=WRAP, style="Muted.TLabel").pack(anchor="w", pady=2)
        r = ttk.Frame(self); r.pack(anchor="w", pady=8)
        self.btn = ttk.Button(r, text="Descargar y analizar", style="Accent.TButton", command=self.run)
        self.btn.pack(side="left")
        self.open_btn = ttk.Button(r, text="Abrir informe", command=self.open_report, state="disabled")
        self.open_btn.pack(side="left", padx=8)
        self.bar = ttk.Progressbar(r, mode="indeterminate", length=160)
        self.bar.pack(side="left", padx=8)
        self.st = self.status()
        self.log = scrolledtext.ScrolledText(self, height=11, width=80, wrap="none", state="disabled", font=("Consolas" if IS_WINDOWS else "TkFixedFont", 9))
        self.log.pack(fill="both", expand=True)

    def channel_labels(self):
        return {f"{c.title or c.ref}  (#{c.num})": c for c in self.app.settings.channels}

    def selected_channel(self) -> Optional[ChannelCfg]:
        return self.channel_labels().get(self.chan.get())

    def report_path(self, c: Optional[ChannelCfg] = None) -> Path:
        c = c or self.selected_channel()
        return data_dir() / (f"report_c{c.num}.html" if c else "report.html")

    def refresh_report_button(self):
        self.open_btn.state(["!disabled" if self.report_path().exists() else "disabled"])

    def on_show(self):
        labels = list(self.channel_labels())
        self.chan_cb["values"] = labels
        if self.chan.get() not in labels:
            self.chan.set(labels[0] if labels else "")
        self.refresh_report_button()

    def detect(self):
        s = self.app.sync_settings()
        from . import mt5_helper
        self.set_status(self.st, "Consultando MetaTrader 5…")

        def ok(res):
            if not res["ok"] or res["tick_time"] is None:
                return self.set_status(self.st, "No se pudo detectar (¿MT5 abierto y mercado abierto?). Ponlo a mano.", "warn")
            now = time.time()
            cur = sl.estimate_server_offset(res["tick_time"], now)
            if cur is None:
                return self.set_status(self.st, "El último precio es viejo (¿mercado cerrado?). Inténtalo con el mercado abierto o ponlo a mano.", "warn")
            base = sl.base_offset(cur, now, self.dst.get())
            self.offset.set(str(base))
            self.set_status(self.st, f"✔ Tu servidor va ahora a UTC{cur:+d}. Hora de invierno configurada: {base}.", "ok")
        self.app.bg(lambda: mt5_helper.probe(s.mt5_path, s.mt5_login, s.mt5_password, s.mt5_server, s.symbol), ok,
                    lambda e: self.set_status(self.st, f"✖ {e}", "bad"))

    def append(self, line: str):
        self.log.config(state="normal")
        self.log.insert("end", line)
        self.log.see("end")
        self.log.config(state="disabled")

    def run(self):
        s = self.app.sync_settings()
        errs = sl.validate_all(s)
        if errs:
            return messagebox.showwarning("Faltan datos", "\n".join(errs))
        ch = self.selected_channel()
        if ch is None:
            return messagebox.showwarning("Falta el canal", "Elige primero el canal que quieres analizar.")
        try:
            days, bal, off = int(self.days.get()), float(self.balance.get()), float(self.offset.get())
        except ValueError:
            return messagebox.showwarning("Revisa los datos", "Días, saldo y hora del servidor deben ser números.")
        self.app.save()
        self.app.close_tg()          # libera la sesión de Telegram para el proceso hijo
        self.btn.state(["disabled"]); self.bar.start(12)
        self.set_status(self.st, "Descargando el historial y simulando… puede tardar unos minutos.")
        out = str(data_dir() / f"history_c{ch.num}.json")
        steps = [["--backtest", "fetch", "--channel", str(ch.num), "--days", str(days), "--out", out],
                 ["--backtest", "run", "--history", out, "--prices", "mt5", "--symbol", s.symbol,
                  "--server-offset", str(off), "--lot", str(ch.lot_size or s.lot_size),
                  "--max-tps", str(ch.max_tps or s.max_tps),
                  "--balance", str(bal), "--report-tz", f"{s.tz_offset_hours:g}",
                  "--report", str(self.report_path(ch)),
                  "--out", str(data_dir() / f"backtest_trades_c{ch.num}.csv")]
                 + (["--us-dst"] if self.dst.get() else [])]

        def work():
            for args in steps:
                code = run_logged(launcher_cmd(args), data_dir() / "backtest.log",
                                  lambda t: self.app.ui(self.append, t), cwd=str(data_dir()))
                if code != 0:
                    return False
            return True

        def finished(ok):
            self.bar.stop(); self.btn.state(["!disabled"])
            if ok:
                self.open_btn.state(["!disabled"])
                self.set_status(self.st, "✔ Listo. Pulsa «Abrir informe» para ver los resultados y las conclusiones.", "ok")
                self.open_report()
            else:
                self.set_status(self.st, "✖ Algo falló. Revisa el detalle de abajo (¿MT5 abierto con historia del oro? ¿hora del servidor?).", "bad")
        self.app.bg(work, finished, lambda e: (self.bar.stop(), self.btn.state(["!disabled"]), self.set_status(self.st, f"✖ {e}", "bad")))

    def open_report(self):
        p = self.report_path()
        if p.exists():
            webbrowser.open(p.as_uri())

    def done(self):
        return any(self.report_path(c).exists() for c in self.app.settings.channels)


class StartupStep(Step):
    title, short = "Inicio automático", "Inicio automático"

    def build(self):
        self.head("Que arranque solo con Windows",
                  "Si el equipo se reinicia (por una actualización o un corte de luz), el programa puede volver a iniciarse "
                  "solo y avisarte por Telegram.")
        self.state = self.status()
        r = ttk.Frame(self); r.pack(anchor="w", pady=4)
        ttk.Button(r, text="Activar inicio automático", style="Accent.TButton", command=self.enable).pack(side="left")
        ttk.Button(r, text="Desactivar", command=self.disable).pack(side="left", padx=8)
        box = ttk.LabelFrame(self, text=" Para que funcione después de un reinicio ", padding=10)
        box.pack(fill="x", pady=12)
        for t in ("1. Windows debe iniciar sesión solo: MetaTrader 5 necesita el escritorio abierto. "
                  "Pulsa el botón de abajo y desmarca «Los usuarios deben escribir su nombre y contraseña». "
                  "Úsalo solo en un equipo con acceso físico controlado.",
                  "2. Evita que el equipo se duerma: Configuración → Sistema → Inicio/apagado y suspensión → «Nunca».",
                  "3. En equipos de escritorio o mini PC, activa en el BIOS «Restore on AC Power Loss» para que encienda solo al volver la luz.",
                  "4. Idealmente usa un UPS (batería de respaldo) y un plan de datos como respaldo de internet."):
            ttk.Label(box, text=t, wraplength=WRAP - 30, justify="left").pack(anchor="w", pady=3)
        ttk.Button(box, text="Abrir ajustes de inicio de sesión automático", command=self.autologin).pack(anchor="w", pady=4)

    def refresh(self):
        if not IS_WINDOWS:
            return self.set_status(self.state, "Esta función solo está disponible en Windows.", "warn")
        on = sl.startup_installed()
        self.set_status(self.state, "Inicio automático: ACTIVADO ✔" if on else "Inicio automático: desactivado", "ok" if on else "info")

    on_show = refresh

    def enable(self):
        if not IS_WINDOWS:
            return messagebox.showinfo("Solo Windows", "El inicio automático solo funciona en Windows.")
        try:
            p = sl.install_startup(launcher_cmd(["--bot"]))
            self.set_status(self.state, f"✔ Activado. Se iniciará tras iniciar sesión en Windows ({p.name}).", "ok")
        except Exception as e:  # noqa: BLE001
            self.set_status(self.state, f"✖ No se pudo activar: {e}", "bad")

    def disable(self):
        if IS_WINDOWS:
            sl.remove_startup()
            self.refresh()

    def autologin(self):
        if IS_WINDOWS:
            subprocess.Popen(["netplwiz"])
        else:
            messagebox.showinfo("Solo Windows", "Esto se configura en Windows con «netplwiz».")

    def done(self):
        return IS_WINDOWS and sl.startup_installed()


class PanelStep(Step):
    title, short = "Panel de control", "Panel"
    RESULTS = {"operada": ("✅ Operada", "ok"), "parcial": ("◐ Parcial", "warn"), "ignorada": ("⏭ Ignorada", "muted"),
               "comando": ("🔧 Comando", "info"), "revisar": ("🔎 Revisar", "warn"), "error": ("❌ Error", "bad")}

    def build(self):
        self.head("Panel de control")
        self.mode = ttk.Label(self, text="", font=self.app.font_big, anchor="center")
        self.mode.pack(fill="x", pady=(0, 2))
        self.summary = ttk.Label(self, text="", justify="left", style="Muted.TLabel", wraplength=WRAP + 40)
        self.summary.pack(anchor="w", pady=(2, 4))
        r = ttk.Frame(self)
        r.pack(anchor="w", pady=4)
        self.start_btn = ttk.Button(r, text="▶  Iniciar", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(r, text="■  Detener", style="Danger.TButton", command=self.stop)
        self.stop_btn.pack(side="left", padx=8)
        self.pause_btn = ttk.Button(r, text="⏸  Pausar señales", command=self.toggle_pause)
        self.pause_btn.pack(side="left")
        self.limit_btn = ttk.Button(r, text="Seguir hoy", command=self.resume_today)   # solo se muestra si saltó el límite
        self.st = self.status()

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, pady=(8, 0))
        # --- Señales recibidas
        t = ttk.Frame(self.nb, padding=(0, 8, 0, 0))
        self.nb.add(t, text="  Señales  ")
        self.inbox_rows: List[dict] = []
        self._inbox_sig = None
        self.inbox = make_tree(t, ("time", "chan", "res", "detail"),
                               (("time", "Hora", 76), ("chan", "Canal", 130), ("res", "Resultado", 110),
                                ("detail", "Detalle", 330)), height=8)
        for tag, color in (("ok", OK), ("bad", BAD), ("warn", WARN), ("muted", MUTED), ("info", LINK)):
            self.inbox.tag_configure(tag, foreground=color)
        self.inbox.bind("<Double-1>", self.show_message)
        ttk.Label(t, text="Doble clic en una fila para ver el mensaje completo. «Revisar» = parece una señal pero "
                          "no se entendió.", style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        # --- Rendimiento
        t = ttk.Frame(self.nb, padding=(0, 8, 0, 0))
        self.nb.add(t, text="  Rendimiento  ")
        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=(0, 6))
        self.perf_btn = ttk.Button(bar, text="Actualizar desde MetaTrader 5", command=self.load_perf)
        self.perf_btn.pack(side="left")
        self.perf_note = ttk.Label(bar, text="", style="Muted.TLabel")
        self.perf_note.pack(side="left", padx=10)
        self.perf = make_tree(t, ("chan", "sig", "cl", "wr", "d0", "d7", "d30", "tot"),
                              (("chan", "Canal", 170), ("sig", "Señales", 64), ("cl", "Cerradas", 70), ("wr", "Acierto", 64),
                               ("d0", "Hoy", 70), ("d7", "7 días", 76), ("d30", "30 días", 76), ("tot", "Total", 80)),
                              height=6)
        ttk.Label(t, text="Resultado real en USD (con comisiones y swaps) de las operaciones de este programa en MT5. "
                          "En simulación no hay operaciones.", style="Muted.TLabel", wraplength=WRAP).pack(anchor="w", pady=(4, 0))
        # --- Registro
        t = ttk.Frame(self.nb, padding=(0, 8, 0, 0))
        self.nb.add(t, text="  Registro  ")
        self.log = scrolledtext.ScrolledText(t, height=9, width=80, wrap="none", state="disabled",
                                             font=("Consolas" if IS_WINDOWS else "TkFixedFont", 9))
        self.log.pack(fill="both", expand=True)
        # --- Más
        t = ttk.Frame(self.nb, padding=(4, 12, 4, 0))
        self.nb.add(t, text="  Más  ")
        r = ttk.Frame(t)
        r.pack(anchor="w", pady=4)
        ttk.Label(r, text="Tema de la ventana", width=22).pack(side="left")
        self.theme_var = tk.StringVar(value=THEME_NAMES.get(self.app.settings.theme, THEME_NAMES["auto"]))
        ttk.Combobox(r, textvariable=self.theme_var, values=list(THEME_NAMES.values()), state="readonly", width=26).pack(side="left")
        ttk.Button(r, text="Aplicar", command=self.apply_theme).pack(side="left", padx=8)
        r = ttk.Frame(t)
        r.pack(anchor="w", pady=4)
        ttk.Label(r, text="Copia de seguridad", width=22).pack(side="left")
        ttk.Button(r, text="Guardar configuración…", command=self.export_backup).pack(side="left")
        ttk.Button(r, text="Restaurar…", command=self.import_backup).pack(side="left", padx=8)
        self.with_session = tk.BooleanVar(value=False)
        ttk.Checkbutton(t, text="Incluir la sesión de Telegram", variable=self.with_session).pack(anchor="w", padx=(176, 0))
        ttk.Label(t, text="Evita volver a pedir el código, pero da acceso a tu cuenta: guarda ese archivo en un lugar "
                          "seguro y no lo compartas.", style="Muted.TLabel", wraplength=WRAP - 180).pack(anchor="w", padx=(176, 0))
        r = ttk.Frame(t)
        r.pack(anchor="w", pady=(10, 4))
        ttk.Label(r, text=f"AutoTrade {__version__}", width=22).pack(side="left")
        self.upd_btn = ttk.Button(r, text="Buscar actualizaciones", command=self.check_updates)
        self.upd_btn.pack(side="left")
        self.upd_open = ttk.Button(r, text="Abrir página de descarga", command=lambda: webbrowser.open(self._upd_url))
        self._upd_url = updates.RELEASES_PAGE
        self.upd_msg = ttk.Label(t, text="", style="Muted.TLabel", wraplength=WRAP)
        self.upd_msg.pack(anchor="w", padx=(176, 0))
        r = ttk.Frame(t)
        r.pack(anchor="w", pady=(10, 0))
        ttk.Label(r, text="Archivos del programa", width=22).pack(side="left")
        ttk.Button(r, text="Abrir carpeta de datos", command=lambda: self.app.open_path(data_dir())).pack(side="left")
        self._jobs: set = set()

    # ---- tareas programadas (se cancelan al cerrar)
    def later(self, ms: int, fn):
        jid = self.after(ms, lambda: (self._jobs.discard(jid), fn()))
        self._jobs.add(jid)
        return jid

    def cancel_jobs(self):
        for jid in list(self._jobs):
            try:
                self.after_cancel(jid)
            except tk.TclError:
                pass
        self._jobs.clear()

    def on_show(self):
        self.refresh()

    # ---- estado
    def control(self) -> Control:
        return Control(data_dir() / "control.json")

    def limit_state(self):
        """Estado del límite diario de HOY (None si no aplica o no ha saltado)."""
        st = RiskGuard(data_dir() / "risk_state.json").state
        today = day_key(time.time(), self.app.settings.tz_offset_hours)
        return st if st.tripped and not st.override and st.day == today else None

    def refresh(self):
        s = self.app.settings
        if s.dry_run:
            self.mode.config(text="MODO SIMULACIÓN — no se abren operaciones reales", foreground=WARN)
        else:
            self.mode.config(text="MODO REAL — se enviarán órdenes a tu cuenta", foreground=BAD)
        sched = "sin restricción" if not s.filter_enabled else f"{days_label(s.trading_days)} · {s.trading_hours} (UTC{s.tz_offset_hours:+g})"
        ctl = self.control().read()
        chans = "; ".join(
            f"{c.title or c.ref} (lote {c.lot_size or s.lot_size:g}"
            f"{', ' + str(c.max_tps) + ' TP' if c.max_tps else ''}{'' if c.enabled else ', DESACTIVADO'}"
            f"{', EN PAUSA' if c.num in ctl['paused_channels'] else ''})"
            for c in s.channels) or "—"
        prot = []
        if s.daily_loss_limit_pct:
            prot.append(f"pérdida diaria {s.daily_loss_limit_pct:g}%")
        if s.max_signals_per_day:
            prot.append(f"{s.max_signals_per_day} señales/día")
        if s.max_spread_usd:
            prot.append(f"spread ≤ {s.max_spread_usd:g}")
        if s.news_mode != "off":
            prot.append(f"noticias ({s.news_mode})")
        if s.auto_be:
            prot.append("BE automático")
        self.summary.config(text=(f"Canales: {chans}\nSímbolo: {s.symbol}  |  Lote total: {s.lot_size}  |  TPs: {s.max_tps}  |  "
                                  f"Horario: {sched}\nProtecciones: {', '.join(prot) or 'ninguna'}  |  "
                                  f"Avisos por Telegram: {'sí' if s.alerts_enabled else 'no'}"))
        running = self.app.bot_running()
        paused = ctl["paused"]
        self.start_btn.state(["disabled" if running else "!disabled"])
        self.stop_btn.state(["!disabled" if running else "disabled"])
        self.pause_btn.config(text="▶  Reanudar señales" if paused else "⏸  Pausar señales")
        tripped = self.limit_state()
        if tripped and not self.limit_btn.winfo_ismapped():
            self.limit_btn.pack(side="left", padx=8)
        elif not tripped and self.limit_btn.winfo_ismapped():
            self.limit_btn.pack_forget()
        if tripped:
            self.set_status(self.st, f"🛑 Límite diario alcanzado: {tripped.reason}. No se abren señales nuevas hoy.", "bad")
        elif paused:
            self.set_status(self.st, "⏸ EN PAUSA: no se abren señales nuevas (los mensajes de gestión siguen funcionando).", "warn")
        elif running:
            self.set_status(self.st, f"● El programa está FUNCIONANDO (proceso {read_pid()}).", "ok")
        elif not self.st.cget("text") or self.st.cget("text")[:1] in "●⏸🛑":
            self.set_status(self.st, "○ Detenido. Pulsa «Iniciar» para empezar.", "info")
        self.refresh_inbox()
        self.tail()
        self.cancel_jobs()
        if self.app.current is self:
            self.later(2000, self.refresh)

    # ---- señales recibidas
    def local_time(self, ts: float) -> str:
        return time.strftime("%H:%M:%S", time.gmtime(ts + self.app.settings.tz_offset_hours * 3600))

    def refresh_inbox(self):
        path = data_dir() / "signals.jsonl"
        try:
            sig = (path.stat().st_mtime, path.stat().st_size)
        except OSError:
            sig = None
        if sig == self._inbox_sig:
            return
        self._inbox_sig = sig
        self.inbox_rows = list(reversed(EventLog(path).tail(150)))          # lo más nuevo arriba
        self.inbox.delete(*self.inbox.get_children())
        for i, r in enumerate(self.inbox_rows):
            label, tag = self.RESULTS.get(r.get("status", ""), (r.get("status", "?"), "muted"))
            self.inbox.insert("", "end", iid=str(i), tags=(tag,), values=(
                self.local_time(r.get("ts", 0)), r.get("title") or f"#{r.get('channel', '?')}", label, r.get("detail", "")))

    def show_message(self, ev):
        iid = self.inbox.identify_row(ev.y)
        if not iid:
            return
        r = self.inbox_rows[int(iid)]
        label = self.RESULTS.get(r.get("status", ""), (r.get("status", ""), ""))[0]
        messagebox.showinfo(f"{r.get('title', '')} · mensaje {r.get('msg_id', '')}",
                            f"{label}\n{r.get('detail', '')}\n\n— Mensaje —\n{r.get('text', '')}")

    # ---- rendimiento real por canal
    def load_perf(self):
        s = self.app.sync_settings()
        self.perf_btn.state(["disabled"])
        self.perf_note.config(text="Consultando MetaTrader 5…", foreground=MUTED)

        def work():
            res = mt5_helper.fetch_deals(s.mt5_path, s.mt5_login, s.mt5_password, s.mt5_server, s.symbol, s.magic, 90)
            if not res["ok"]:
                return res
            res["rows"] = perf.aggregate(res["deals"], time.time(), s.tz_offset_hours, res["server_offset"])
            return res

        def done(res):
            self.perf_btn.state(["!disabled"])
            self.perf.delete(*self.perf.get_children())
            if not res["ok"]:
                return self.perf_note.config(text="✖ " + res["error"], foreground=BAD)
            names = {c.num: c.title or c.ref for c in s.channels}
            money = lambda v: f"{v:+.2f}"
            for r in res["rows"]:
                name = names.get(r["num"], f"Canal #{r['num']} (quitado)") if r["num"] is not None else "Sin etiqueta (otras)"
                self.perf.insert("", "end", values=(name, r["signals"], r["closed"], f"{r['win_rate']:.0f}%",
                                                    money(r["pnl_today"]), money(r["pnl_7d"]), money(r["pnl_30d"]),
                                                    money(r["pnl_total"])))
            self.perf_note.config(text=("Últimos 90 días." if res["rows"] else "Todavía no hay operaciones cerradas del programa."),
                                  foreground=MUTED)
        self.app.bg(work, done, lambda e: (self.perf_btn.state(["!disabled"]),
                                           self.perf_note.config(text=f"✖ {e}", foreground=BAD)))

    # ---- registro
    def tail(self):
        p = data_dir() / "autotrade.log"
        text = ""
        if p.exists():
            try:
                text = "".join(p.read_text(encoding="utf-8", errors="replace").splitlines(True)[-200:])
            except OSError:
                pass
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", text or "(todavía no hay registro)")
        self.log.see("end")
        self.log.config(state="disabled")

    # ---- control
    def start(self):
        s = self.app.sync_settings()
        errs = sl.validate_all(s)
        if errs:
            return messagebox.showwarning("Faltan datos", "Antes de iniciar, completa:\n\n• " + "\n• ".join(errs))
        if not s.dry_run and not messagebox.askyesno(
                "Operar de verdad",
                "Vas a iniciar en MODO REAL: se enviarán órdenes a tu cuenta de MetaTrader 5.\n\n"
                "¿Ya probaste en simulación o demo y quieres continuar?", icon="warning"):
            return
        self.app.save()
        self.app.close_tg()
        try:
            subprocess.Popen(launcher_cmd(["--bot"]), cwd=str(data_dir()), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen_kwargs(detached=True))
        except OSError as e:
            return self.set_status(self.st, f"✖ No se pudo iniciar: {e}", "bad")
        self.set_status(self.st, "Iniciando… (en unos segundos recibirás un aviso en Telegram)", "info")
        self.later(1500, self.refresh)

    def stop(self):
        pid = read_pid()
        if pid and pid_alive(pid):
            stop_tree(pid)
        try:
            (data_dir() / PID_FILE).unlink()
        except OSError:
            pass
        self.set_status(self.st, "○ Detenido.", "info")
        self.later(800, self.refresh)

    def toggle_pause(self):
        c = self.control()
        c.set_paused(not c.read()["paused"])
        self.refresh()

    def resume_today(self):
        RiskGuard(data_dir() / "risk_state.json").resume()
        self.refresh()

    # ---- más
    def apply_theme(self):
        key = next((k for k, v in THEME_NAMES.items() if v == self.theme_var.get()), "auto")
        if key == self.app.settings.theme:
            return
        self.app.v["theme"].set(key)
        self.app.restart()

    def export_backup(self):
        path = filedialog.asksaveasfilename(title="Guardar copia de seguridad", defaultextension=".zip",
                                            initialfile="autotrade-copia.zip", filetypes=[("Copia de AutoTrade", "*.zip")])
        if not path:
            return
        self.app.sync_settings()
        self.app.save()
        try:
            names = backup.export_zip(data_dir(), path, self.with_session.get())
        except (OSError, FileNotFoundError) as e:
            return messagebox.showerror("No se pudo guardar", str(e))
        messagebox.showinfo("Copia guardada", f"Se guardó: {', '.join(names)}\n\n{path}"
                            + ("\n\nIncluye tu sesión de Telegram: no la compartas con nadie." if self.with_session.get() else ""))

    def import_backup(self):
        path = filedialog.askopenfilename(title="Elegir copia de seguridad", filetypes=[("Copia de AutoTrade", "*.zip")])
        if not path:
            return
        if not messagebox.askyesno("Restaurar copia", "Se reemplazará la configuración actual (se guarda una copia "
                                   "como config.ini.bak-…). ¿Continuar?"):
            return
        try:
            backup.import_zip(path, data_dir())
        except (OSError, ValueError) as e:
            return messagebox.showerror("No se pudo restaurar", str(e))
        self.app.settings = sl.load_settings(data_dir() / "config.ini")
        messagebox.showinfo("Copia restaurada", "Listo. La ventana se reiniciará para cargar la configuración.")
        self.app.restart(save=False)

    def check_updates(self):
        self.upd_btn.state(["disabled"])
        self.upd_msg.config(text="Buscando…", foreground=MUTED)

        def done(r):
            self.upd_btn.state(["!disabled"])
            self._upd_url = r["url"]
            self.upd_msg.config(text=r["message"], foreground={"newer": WARN, "current": OK, "none": MUTED, "error": BAD}[r["status"]])
            if r["status"] == "newer":
                self.upd_open.pack(side="left", padx=8)
            else:
                self.upd_open.pack_forget()
        self.app.bg(lambda: updates.check(__version__), done,
                    lambda e: (self.upd_btn.state(["!disabled"]), self.upd_msg.config(text=str(e), foreground=BAD)))

    def done(self):
        return self.app.bot_running()


STEPS = [WelcomeStep, TelegramStep, ChannelStep, MT5Step, TradingStep, ScheduleStep, ProtectionsStep,
         AlertsStep, TestStep, BacktestStep, StartupStep, PanelStep]


# ---------------------------------------------------------------- aplicación
class App(tk.Tk):
    def __init__(self, sync: bool = False):
        super().__init__()
        self.sync = sync                    # modo síncrono (pruebas): sin hilos
        self.title("AutoTrade — Asistente de instalación")
        self.geometry("1080x800")
        self.minsize(1000, 740)
        self.settings = sl.load_settings(data_dir() / "config.ini")
        self.restart_requested = False
        apply_palette(resolve_theme(self.settings.theme))        # antes de crear cualquier widget
        self.tg = None
        self.tg_user: Optional[str] = None
        self.mt5_info: Optional[dict] = None
        self.q: "queue.Queue" = queue.Queue()
        self._style()
        self.v: Dict[str, tk.Variable] = {}
        for f in fields(Settings):
            if f.name == "channels":            # lista: la edita el paso «Canales», no un campo de texto
                continue
            val = getattr(self.settings, f.name)
            self.v[f.name] = (tk.BooleanVar(value=val) if isinstance(val, bool)
                              else tk.StringVar(value=f"{val:g}" if isinstance(val, float) else str(val)))
        self.v["max_tps"].set(str(self.settings.max_tps))
        self._build()
        self.current: Optional[Step] = None
        self.index = -1
        self.visited: set = set()
        first = len(STEPS) - 1 if self._configured() else 0
        self.show(first, validate=False)
        self._pump_job = self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- estilo y estructura
    def _style(self):
        from tkinter import font as tkfont
        st = ttk.Style(self)
        # "clam" en todas las plataformas: el tema nativo de Windows ignora los colores de fondo
        # (botones con texto blanco sobre gris = ilegibles). Aquí cada estado define fondo Y texto.
        st.theme_use("clam")
        fam = "Segoe UI" if IS_WINDOWS else None
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(size=10, **({"family": fam} if fam else {}))
        family = fam or base.actual("family")
        mk = lambda size, weight="normal": tkfont.Font(family=family, size=size, weight=weight)
        self.font_big, self.font_bold, self.font_small = mk(14, "bold"), mk(10, "bold"), mk(8, "bold")
        self.font_base = base

        st.configure(".", background=BG, foreground=TEXT, bordercolor=BORDER, font=base,
                     troughcolor=BAR, focuscolor=BG, lightcolor=BG, darkcolor=BG)
        st.configure("Title.TLabel", font=mk(20, "bold"), foreground=TEXT)
        st.configure("H2.TLabel", font=mk(11, "bold"))
        st.configure("Muted.TLabel", foreground=MUTED)
        st.configure("Bar.TFrame", background=BAR)

        def button(name, bg, fg, border, hover, press, dis_bg, dis_fg, pad=(16, 8), font=None, bevel=None):
            bevel = bevel or bg                 # color del filo interior: igual al borde en los secundarios
            st.configure(name, background=bg, foreground=fg, bordercolor=border, lightcolor=bevel, darkcolor=bevel,
                         focuscolor=bg, padding=pad, relief="raised", borderwidth=1, font=font or self.font_bold)
            st.map(name,
                   background=[("disabled", dis_bg), ("pressed", press), ("active", hover)],
                   foreground=[("disabled", dis_fg), ("!disabled", fg)],
                   bordercolor=[("disabled", dis_bg), ("focus", ACCENT), ("active", hover)],
                   lightcolor=[("disabled", dis_bg), ("pressed", press), ("active", hover)],
                   darkcolor=[("disabled", dis_bg), ("pressed", press), ("active", hover)])
        # Secundario (por defecto): fondo claro + texto oscuro
        # (fondo gris claro + borde visible: se distingue como botón sobre el fondo blanco)
        button("TButton", B2_BG, TEXT, B2_BORDER, B2_HOVER, B2_PRESS, B2_DIS_BG, B2_DIS_FG,
               font=self.font_base, bevel=B2_BORDER)
        # Principal: azul + texto blanco
        button("Accent.TButton", ACCENT, "#ffffff", ACCENT, ACCENT_DK, ACCENT_PR, B1_DIS_BG, B1_DIS_FG)
        # Peligro: rojo + texto blanco
        button("Danger.TButton", DANGER, "#ffffff", DANGER, BD_HOVER, BD_PRESS, BD_DIS_BG, BD_DIS_FG)
        # Pequeño (Mostrar/Ocultar)
        button("Small.TButton", B2_BG, LINK, B2_BORDER, B2_HOVER, B2_PRESS, B2_DIS_BG, B2_DIS_FG,
               pad=(8, 4), font=self.font_base, bevel=B2_BORDER)

        for w in ("TEntry", "TCombobox", "TSpinbox"):
            st.configure(w, fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                         bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, insertcolor=TEXT,
                         padding=(8, 6), arrowcolor=MUTED, arrowsize=14,
                         selectbackground=SEL, selectforeground=TEXT)
            st.map(w, bordercolor=[("focus", ACCENT), ("hover", HOVER_BORDER)],
                   lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)],
                   fieldbackground=[("disabled", BAR), ("readonly", FIELD)],
                   foreground=[("disabled", DIS_FG)],
                   selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", base)

        for w in ("TCheckbutton", "TRadiobutton"):
            # indicador grande y con borde oscuro (el gris claro por defecto casi no se veía)
            st.configure(w, background=BG, foreground=TEXT, indicatorbackground=FIELD,
                         indicatorforeground=FIELD, upperbordercolor=IND_BORDER, lowerbordercolor=IND_BORDER,
                         indicatorsize=18, indicatormargin=(2, 2, 8, 2), padding=(2, 4))
            st.map(w, background=[("active", BG)], foreground=[("disabled", DIS_FG)],
                   indicatorbackground=[("selected", ACCENT), ("pressed", IND_PRESS)],
                   indicatorforeground=[("selected", "#ffffff")])
        # círculo elegido = punto azul en el centro con borde azul
        st.map("TRadiobutton", indicatorbackground=[("selected", FIELD), ("pressed", IND_PRESS)],
               indicatorforeground=[("selected", ACCENT)],
               upperbordercolor=[("selected", ACCENT)], lowerbordercolor=[("selected", ACCENT)])
        st.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="solid", borderwidth=1)
        st.configure("TLabelframe.Label", background=BG, foreground=TEXT, font=self.font_bold)

        st.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT,
                     rowheight=28, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, borderwidth=1)
        st.map("Treeview", background=[("selected", SEL)], foreground=[("selected", TEXT)])
        st.configure("Treeview.Heading", background=BAR, foreground=TEXT, font=self.font_bold, relief="flat",
                     padding=(8, 6), bordercolor=BORDER, lightcolor=BAR, darkcolor=BAR)
        st.map("Treeview.Heading", background=[("active", HEAD_HOVER)])
        st.configure("Vertical.TScrollbar", background=SCROLL, troughcolor=BAR, bordercolor=BAR,
                     arrowcolor=MUTED, lightcolor=SCROLL, darkcolor=SCROLL)
        st.map("Vertical.TScrollbar", background=[("active", SCROLL_HOVER)])
        st.configure("TNotebook", background=BG, bordercolor=BORDER, tabmargins=(0, 4, 0, 0))
        st.configure("TNotebook.Tab", background=BAR, foreground=MUTED, padding=(16, 8), bordercolor=BORDER,
                     font=self.font_base)
        st.map("TNotebook.Tab", background=[("selected", BG), ("active", HEAD_HOVER)],
               foreground=[("selected", TEXT)], padding=[("selected", (16, 8))])
        st.configure("Horizontal.TProgressbar", troughcolor=TROUGH, background=ACCENT, bordercolor=TROUGH,
                     lightcolor=ACCENT, darkcolor=ACCENT)
        st.configure("TSeparator", background=BORDER)

        # Cuadros de texto (tk): borde fino, relleno interior y selección legible
        for opt, val in (("background", FIELD), ("foreground", TEXT), ("relief", "flat"),
                         ("highlightThickness", 1), ("highlightBackground", BORDER), ("highlightColor", ACCENT),
                         ("padX", 10), ("padY", 8), ("selectBackground", SEL), ("selectForeground", TEXT),
                         ("insertBackground", TEXT)):
            self.option_add(f"*Text.{opt}", val)
        self.configure(bg=BG)

    def _build(self):
        # --- barra lateral oscura con los pasos
        self.side = tk.Frame(self, bg=SIDEBAR, width=236)
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)
        tk.Label(self.side, text="AutoTrade", bg=SIDEBAR, fg="#ffffff", font=self.font_big, anchor="w").pack(
            fill="x", padx=20, pady=(22, 0))
        tk.Label(self.side, text="Señales de Telegram → MT5", bg=SIDEBAR, fg=SIDE_SUB, font=self.font_base,
                 anchor="w").pack(fill="x", padx=20, pady=(2, 18))
        self.side_rows, self.side_chips, self.side_labels, self.side_bars = [], [], [], []
        for i, cls in enumerate(STEPS):
            row = tk.Frame(self.side, bg=SIDEBAR, cursor="hand2")
            row.pack(fill="x")
            bar = tk.Frame(row, width=4, bg=SIDEBAR)
            bar.pack(side="left", fill="y")
            chip = tk.Label(row, text=str(i + 1), width=3, bg=CHIP, fg="#ffffff", font=self.font_small)
            chip.pack(side="left", padx=(14, 12), pady=9)
            lab = tk.Label(row, text="", anchor="w", bg=SIDEBAR, fg=SIDE_TEXT, font=self.font_base)
            lab.pack(side="left", fill="x", expand=True)
            for w in (row, bar, chip, lab):
                w.bind("<Button-1>", lambda e, i=i: self.goto(i))
                w.bind("<Enter>", lambda e, i=i: self._hover(i, True))
                w.bind("<Leave>", lambda e, i=i: self._hover(i, False))
            self.side_rows.append(row)
            self.side_bars.append(bar)
            self.side_chips.append(chip)
            self.side_labels.append(lab)

        # --- zona principal: encabezado con progreso, contenido y barra de navegación
        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        head = tk.Frame(right, bg=BG)
        head.pack(fill="x", padx=34, pady=(20, 0))
        self.crumb = tk.Label(head, text="", bg=BG, fg=MUTED, font=self.font_small, anchor="w")
        self.crumb.pack(fill="x")
        self.progress = tk.Canvas(head, height=4, bg=TROUGH, highlightthickness=0)
        self.progress.pack(fill="x", pady=(6, 0))
        self.progress.bind("<Configure>", lambda e: self._draw_progress())
        # La barra de navegación se reserva primero (abajo): si un paso crece, se recorta el contenido,
        # nunca los botones «Atrás» / «Siguiente».
        nav = ttk.Frame(right, style="Bar.TFrame", padding=(34, 12))
        nav.pack(side="bottom", fill="x")
        ttk.Separator(right, orient="horizontal").pack(side="bottom", fill="x")
        self.body = ttk.Frame(right, padding=(34, 16, 34, 6))
        self.body.pack(fill="both", expand=True)
        self.steps = [cls(self, self.body) for cls in STEPS]
        self.back_btn = ttk.Button(nav, text="←  Atrás", command=lambda: self.goto(self.index - 1))
        self.back_btn.pack(side="left")
        self.next_btn = ttk.Button(nav, text="Siguiente  →", style="Accent.TButton",
                                   command=lambda: self.goto(self.index + 1))
        self.next_btn.pack(side="right")

    def _draw_progress(self):
        c = self.progress
        c.delete("all")
        w = c.winfo_width()
        if self.index >= 0 and w > 1:
            c.create_rectangle(0, 0, w * (self.index + 1) / len(STEPS), 4, fill=ACCENT, width=0)

    def _hover(self, i: int, on: bool):
        if i == self.index:
            return
        bg = SIDE_HOVER if on else SIDEBAR
        for w in (self.side_rows[i], self.side_bars[i], self.side_labels[i]):
            w.config(bg=bg)

    def _configured(self) -> bool:
        p = data_dir() / "config.ini"
        return p.exists() and not (sl.validate_telegram(self.settings) or sl.validate_channel(self.settings))

    # ---- navegación
    def goto(self, i: int):
        if i < 0 or i >= len(self.steps) or i == self.index:
            return
        if i > self.index and self.current is not None:
            self.sync_settings()
            errs = self.current.check()
            if errs:
                messagebox.showwarning("Falta un paso", "\n".join(errs))
                return
        self.show(i)

    def show(self, i: int, validate: bool = True):
        if self.current is not None:
            self.current.leave()
            self.sync_settings()
            self.save()
            self.current.pack_forget()
        self.index, self.current = i, self.steps[i]
        self.visited.add(i)
        self.current.pack(fill="both", expand=True)
        self.current.on_show()
        self.title(f"AutoTrade — {self.current.title}")
        self.crumb.config(text=f"PASO {i + 1} DE {len(STEPS)}")
        self._draw_progress()
        self.back_btn.state(["disabled" if i == 0 else "!disabled"])
        self.next_btn.pack_forget() if i == len(self.steps) - 1 else self.next_btn.pack(side="right")
        self.refresh_side()

    def refresh_side(self):
        for i in range(len(STEPS)):
            cur = i == self.index
            done = i in self.visited and not cur and self.steps[i].done()
            row_bg = SIDE_ACTIVE if cur else SIDEBAR
            chip_bg = ACCENT if cur else (CHIP_DONE if done else CHIP)
            self.side_chips[i].config(text="✔" if done else str(i + 1), bg=chip_bg)
            self.side_labels[i].config(text=self.steps[i].short, bg=row_bg,
                                       fg="#ffffff" if cur else (SIDE_DONE if done else SIDE_TEXT),
                                       font=self.font_bold if cur else self.font_base)
            self.side_rows[i].config(bg=row_bg)
            self.side_bars[i].config(bg=SIDE_BAR if cur else row_bg)

    # ---- ajustes
    def sync_settings(self) -> Settings:
        """Vuelca los campos de pantalla a Settings (conversión de tipos tolerante)."""
        s = self.settings
        for f in fields(Settings):
            if f.name == "channels":
                continue
            raw = self.v[f.name].get()
            cur = getattr(s, f.name)
            try:
                if isinstance(cur, bool):
                    val = bool(raw)
                elif isinstance(cur, int):
                    val = int(float(str(raw).strip()))
                elif isinstance(cur, float):
                    val = float(str(raw).strip().replace(",", "."))
                else:
                    val = raw if f.name == "mt5_password" else str(raw).strip()
            except ValueError:
                continue                    # se conserva el valor anterior; los validadores avisan
            setattr(s, f.name, val)
        return s

    def save(self):
        (data_dir() / "config.ini").write_text(sl.render_config(self.settings), encoding="utf-8")

    # ---- Telegram
    def make_tg(self):
        from .telegram_helper import TelegramHelper
        if self.tg is None:
            self.tg = TelegramHelper(int(self.settings.api_id), self.settings.api_hash, self.settings.session_name)
        return self.tg

    def close_tg(self):
        if self.tg is not None:
            self.tg.close()
            self.tg = None

    # ---- hilos
    def bg(self, fn: Callable, ok: Callable, fail: Optional[Callable] = None):
        def runner():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                self.q.put((fail or self._default_fail, e))
            else:
                self.q.put((ok, res))
        if self.sync:
            runner()
            self._pump_once()
        else:
            threading.Thread(target=runner, daemon=True).start()

    def ui(self, fn: Callable, *args):
        self.q.put((lambda a: fn(*a), args))

    def _default_fail(self, e):
        messagebox.showerror("Error", str(e))

    def _pump_once(self):
        ran = False
        try:
            while True:
                cb, arg = self.q.get_nowait()
                cb(arg)
                ran = True
        except queue.Empty:
            pass
        if ran and self.current is not None:
            self.refresh_side()

    def _pump(self):
        self._pump_once()
        self._pump_job = self.after(100, self._pump)

    def destroy(self):
        # cancelar tareas programadas para que no queden callbacks huérfanos
        job = getattr(self, "_pump_job", None)
        if job:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        for step in getattr(self, "steps", []):
            getattr(step, "cancel_jobs", lambda: None)()
        super().destroy()

    # ---- bot
    def bot_running(self) -> bool:
        pid = read_pid()
        return bool(pid and pid_alive(pid))

    def open_path(self, p: Path):
        try:
            if IS_WINDOWS:
                os.startfile(str(p))          # type: ignore[attr-defined]
            else:
                webbrowser.open(Path(p).as_uri())
        except Exception as e:  # noqa: BLE001
            messagebox.showinfo("No se pudo abrir", f"{p}\n\n{e}")

    def restart(self, save: bool = True):
        """Cierra y vuelve a abrir la ventana (para aplicar el tema o una copia restaurada)."""
        self.restart_requested = True
        if save:
            self.sync_settings()
            self.save()
        self.close_tg()
        self.destroy()

    def on_close(self):
        if self.bot_running():
            r = messagebox.askyesnocancel(
                "El programa sigue funcionando",
                "El bot está operando en segundo plano.\n\n"
                "Sí = cerrar solo esta ventana (el bot sigue funcionando)\n"
                "No = detener el bot y salir\nCancelar = volver")
            if r is None:
                return
            if r is False:
                pid = read_pid()
                if pid:
                    stop_tree(pid)
        self.save()
        self.close_tg()
        self.destroy()


def run_gui() -> int:
    while True:
        app = App()
        app.mainloop()
        if not app.restart_requested:
            return 0
