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

from . import setup_logic as sl
from .paths import (IS_WINDOWS, data_dir, launcher_cmd, pid_alive, popen_kwargs, stop_tree)
from .schedule import parse_days
from .setup_logic import Settings
from .supervisor import PID_FILE, read_pid

WRAP = 600
ACCENT = "#2a5bd7"
OK, BAD, WARN, MUTED = "#0f8a4f", "#c4321f", "#b26b00", "#667085"
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
            ttk.Label(self, text=intro, wraplength=WRAP, justify="left", style="Muted.TLabel").pack(anchor="w", pady=(4, 10))

    def text(self, msg: str, style: str = "TLabel", pady=(0, 6)) -> ttk.Label:
        l = ttk.Label(self, text=msg, wraplength=WRAP, justify="left", style=style)
        l.pack(anchor="w", pady=pady)
        return l

    def row(self, label: str, name: str, show: str = "", width: int = 38, help: str = "") -> ttk.Entry:
        f = ttk.Frame(self)
        f.pack(anchor="w", fill="x", pady=3)
        ttk.Label(f, text=label, width=22).pack(side="left")
        e = ttk.Entry(f, textvariable=self.app.v[name], show=show, width=width)
        e.pack(side="left")
        if help:
            ttk.Label(f, text=help, style="Muted.TLabel").pack(side="left", padx=8)
        return e

    def status(self) -> ttk.Label:
        l = ttk.Label(self, text="", wraplength=WRAP, justify="left")
        l.pack(anchor="w", pady=6)
        return l

    def set_status(self, label: ttk.Label, msg: str, kind: str = "info") -> None:
        color = {"ok": OK, "bad": BAD, "warn": WARN}.get(kind, MUTED)
        label.config(text=msg, foreground=color)


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
            missing = []
            for mod in ("telethon", "MetaTrader5"):
                try:
                    __import__(mod)
                except ImportError:
                    missing.append(mod)
            res["lib"] = (("ok", "Completos") if not missing else
                          ("warn" if missing == ["MetaTrader5"] and not IS_WINDOWS else "bad",
                           "Faltan: " + ", ".join(missing)))
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
        self.row("api_hash", "api_hash", width=40)
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
    title, short = "Elegir el canal", "Canal"

    def build(self):
        self.head("¿De qué canal copiamos las señales?",
                  "Aquí aparecen los canales y grupos a los que estás unido. Elige el que envía las señales de oro.")
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=4)
        self.load_btn = ttk.Button(bar, text="Cargar mis canales", style="Accent.TButton", command=self.load)
        self.load_btn.pack(side="left")
        ttk.Label(bar, text="  Buscar:").pack(side="left")
        self.q = tk.StringVar()
        self.q.trace_add("write", lambda *_: self.refill())
        ttk.Entry(bar, textvariable=self.q, width=24).pack(side="left")
        cols = ("name", "kind", "user")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=9, selectmode="browse")
        for c, t, w in (("name", "Nombre", 320), ("kind", "Tipo", 70), ("user", "@usuario", 160)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="x", pady=6)
        self.tree.bind("<<TreeviewSelect>>", self.pick)
        self.items: List[dict] = []
        f = ttk.Frame(self)
        f.pack(anchor="w", fill="x", pady=(6, 0))
        ttk.Label(f, text="O escribe su @usuario:").pack(side="left")
        self.manual = tk.StringVar()
        ttk.Entry(f, textvariable=self.manual, width=26).pack(side="left", padx=6)
        ttk.Button(f, text="Usar este", command=self.use_manual).pack(side="left")
        self.st = self.status()

    def on_show(self):
        s = self.app.settings
        if s.channel:
            self.set_status(self.st, f"Canal elegido: {s.channel_title or s.channel}", "ok")
        elif not self.app.tg_user:
            self.set_status(self.st, "Primero conecta Telegram en el paso anterior.", "warn")
        if self.app.tg_user and not self.items:
            self.load()

    def load(self):
        if not self.app.tg:
            return self.set_status(self.st, "Primero conecta Telegram en el paso anterior.", "warn")
        self.set_status(self.st, "Cargando tus canales…")

        def ok(items):
            self.items = items
            self.refill()
            self.set_status(self.st, f"{len(items)} canales/grupos. Selecciona uno de la lista." if items
                            else "No encontré canales. ¿Estás unido al canal de señales?", "info" if items else "warn")
        self.app.bg(self.app.tg.channels, ok, lambda e: self.set_status(self.st, f"✖ {e}", "bad"))

    def refill(self):
        q = self.q.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for i, it in enumerate(self.items):
            if q in it["name"].lower() or q in it["username"].lower():
                self.tree.insert("", "end", iid=str(i), values=(it["name"], it["kind"], ("@" + it["username"]) if it["username"] else ""))

    def pick(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        it = self.items[int(sel[0])]
        self.app.set_channel(("@" + it["username"]) if it["username"] else str(it["id"]), it["name"])
        self.set_status(self.st, f"✔ Canal elegido: {it['name']}", "ok")

    def use_manual(self):
        ref = self.manual.get().strip()
        if not ref:
            return
        self.app.set_channel(ref, ref)
        self.set_status(self.st, f"✔ Canal elegido: {ref}", "ok")

    def check(self):
        return sl.validate_channel(self.app.settings)

    def done(self):
        return bool(self.app.settings.channel)


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
            ttk.Entry(r, textvariable=self.app.v[name], show=show, width=30).pack(side="left")
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
        self.out = scrolledtext.ScrolledText(self, height=11, width=70, wrap="word", state="disabled", background="#f5f6f8")
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

    def on_show(self):
        if (data_dir() / "report.html").exists():
            self.open_btn.state(["!disabled"])

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
        try:
            days, bal, off = int(self.days.get()), float(self.balance.get()), float(self.offset.get())
        except ValueError:
            return messagebox.showwarning("Revisa los datos", "Días, saldo y hora del servidor deben ser números.")
        self.app.save()
        self.app.close_tg()          # libera la sesión de Telegram para el proceso hijo
        self.btn.state(["disabled"]); self.bar.start(12)
        self.set_status(self.st, "Descargando el historial y simulando… puede tardar unos minutos.")
        out = str(data_dir() / "history.json")
        steps = [["--backtest", "fetch", "--days", str(days), "--out", out],
                 ["--backtest", "run", "--history", out, "--prices", "mt5", "--symbol", s.symbol,
                  "--server-offset", str(off), "--lot", str(s.lot_size), "--max-tps", str(s.max_tps),
                  "--balance", str(bal), "--report-tz", f"{s.tz_offset_hours:g}",
                  "--report", str(data_dir() / "report.html"), "--out", str(data_dir() / "backtest_trades.csv")]
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
        p = data_dir() / "report.html"
        if p.exists():
            webbrowser.open(p.as_uri())

    def done(self):
        return (data_dir() / "report.html").exists()


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

    def build(self):
        self.head("Panel de control")
        self.mode = ttk.Label(self, text="", font=self.app.font_big, anchor="center")
        self.mode.pack(fill="x", pady=4)
        self.summary = ttk.Label(self, text="", justify="left", style="Muted.TLabel", wraplength=WRAP)
        self.summary.pack(anchor="w", pady=4)
        r = ttk.Frame(self); r.pack(anchor="w", pady=8)
        self.start_btn = ttk.Button(r, text="▶  Iniciar", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(r, text="■  Detener", command=self.stop)
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(r, text="Abrir carpeta de datos", command=lambda: self.app.open_path(data_dir())).pack(side="left", padx=8)
        ttk.Button(r, text="Abrir informe", command=lambda: self.app.open_path(data_dir() / "report.html")).pack(side="left")
        self.st = self.status()
        ttk.Label(self, text="Registro del programa", style="H2.TLabel").pack(anchor="w", pady=(6, 2))
        self.log = scrolledtext.ScrolledText(self, height=12, width=80, wrap="none", state="disabled", font=("Consolas" if IS_WINDOWS else "TkFixedFont", 9))
        self.log.pack(fill="both", expand=True)
        self._job = None

    def on_show(self):
        self.refresh()

    def refresh(self):
        s = self.app.settings
        if s.dry_run:
            self.mode.config(text="MODO SIMULACIÓN — no se abren operaciones reales", foreground=WARN)
        else:
            self.mode.config(text="MODO REAL — se enviarán órdenes a tu cuenta", foreground=BAD)
        sched = "sin restricción" if not s.filter_enabled else f"{days_label(s.trading_days)} · {s.trading_hours} (UTC{s.tz_offset_hours:+g})"
        self.summary.config(text=(f"Canal: {s.channel_title or s.channel or '—'}   |   Símbolo: {s.symbol}\n"
                                  f"Lote total: {s.lot_size}   |   TPs: {s.max_tps}   |   Horario: {sched}\n"
                                  f"Avisos por Telegram: {'sí' if s.alerts_enabled else 'no'}"))
        running = self.app.bot_running()
        pid = read_pid()
        self.start_btn.state(["disabled" if running else "!disabled"])
        self.stop_btn.state(["!disabled" if running else "disabled"])
        if running:
            self.set_status(self.st, f"● El programa está FUNCIONANDO (proceso {pid}).", "ok")
        elif self.st.cget("text").startswith("●") or not self.st.cget("text"):
            self.set_status(self.st, "○ Detenido. Pulsa «Iniciar» para empezar.", "info")
        self.tail()
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(2000, self.refresh) if self.app.current is self else None

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
        self.after(1500, self.refresh)

    def stop(self):
        pid = read_pid()
        if pid and pid_alive(pid):
            stop_tree(pid)
        try:
            (data_dir() / PID_FILE).unlink()
        except OSError:
            pass
        self.set_status(self.st, "○ Detenido.", "info")
        self.after(800, self.refresh)

    def done(self):
        return self.app.bot_running()


STEPS = [WelcomeStep, TelegramStep, ChannelStep, MT5Step, TradingStep, ScheduleStep,
         AlertsStep, TestStep, BacktestStep, StartupStep, PanelStep]


# ---------------------------------------------------------------- aplicación
class App(tk.Tk):
    def __init__(self, sync: bool = False):
        super().__init__()
        self.sync = sync                    # modo síncrono (pruebas): sin hilos
        self.title("AutoTrade — Asistente de instalación")
        self.geometry("1000x700")
        self.minsize(940, 660)
        self.settings = sl.load_settings(data_dir() / "config.ini")
        self.tg = None
        self.tg_user: Optional[str] = None
        self.mt5_info: Optional[dict] = None
        self.q: "queue.Queue" = queue.Queue()
        self._style()
        self.v: Dict[str, tk.Variable] = {}
        for f in fields(Settings):
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
        st = ttk.Style(self)
        st.theme_use("vista" if "vista" in st.theme_names() else "clam")
        fam = "Segoe UI" if IS_WINDOWS else None
        from tkinter import font as tkfont
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(size=10, **({"family": fam} if fam else {}))
        self.font_big = tkfont.Font(family=fam or base.actual("family"), size=13, weight="bold")
        st.configure("Title.TLabel", font=tkfont.Font(family=fam or base.actual("family"), size=16, weight="bold"))
        st.configure("H2.TLabel", font=tkfont.Font(family=fam or base.actual("family"), size=11, weight="bold"))
        st.configure("Muted.TLabel", foreground=MUTED)
        st.configure("Accent.TButton", foreground="white", background=ACCENT, padding=6)
        st.map("Accent.TButton", background=[("active", "#1f48ad"), ("disabled", "#9db3ea")],
               foreground=[("disabled", "#ffffff")])
        st.configure("Side.TLabel", padding=(14, 7))
        st.configure("SideCur.TLabel", padding=(14, 7), background="#e8eefc", foreground=ACCENT,
                     font=tkfont.Font(family=fam or base.actual("family"), size=10, weight="bold"))

    def _build(self):
        self.side = ttk.Frame(self, width=220)
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)
        ttk.Label(self.side, text="AutoTrade", font=self.font_big, padding=(14, 14, 14, 10)).pack(anchor="w")
        self.side_labels: List[ttk.Label] = []
        for i, cls in enumerate(STEPS):
            l = ttk.Label(self.side, text="", style="Side.TLabel", cursor="hand2")
            l.pack(fill="x")
            l.bind("<Button-1>", lambda e, i=i: self.goto(i))
            self.side_labels.append(l)
        ttk.Separator(self, orient="vertical").pack(side="left", fill="y")
        right = ttk.Frame(self, padding=(24, 18))
        right.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(right)
        self.body.pack(fill="both", expand=True)
        self.steps = [cls(self, self.body) for cls in STEPS]
        nav = ttk.Frame(right)
        nav.pack(fill="x", pady=(10, 0))
        self.back_btn = ttk.Button(nav, text="← Atrás", command=lambda: self.goto(self.index - 1))
        self.back_btn.pack(side="left")
        self.next_btn = ttk.Button(nav, text="Siguiente →", style="Accent.TButton", command=lambda: self.goto(self.index + 1))
        self.next_btn.pack(side="right")

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
        self.back_btn.state(["disabled" if i == 0 else "!disabled"])
        self.next_btn.pack_forget() if i == len(self.steps) - 1 else self.next_btn.pack(side="right")
        self.refresh_side()

    def refresh_side(self):
        for i, l in enumerate(self.side_labels):
            mark = "✔" if i in self.visited and i != self.index and self.steps[i].done() else f"{i + 1}"
            l.config(text=f"  {mark}   {self.steps[i].short}", style="SideCur.TLabel" if i == self.index else "Side.TLabel")

    # ---- ajustes
    def sync_settings(self) -> Settings:
        """Vuelca los campos de pantalla a Settings (conversión de tipos tolerante)."""
        s = self.settings
        for f in fields(Settings):
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

    def set_channel(self, ref: str, title: str):
        self.v["channel"].set(ref)
        self.v["channel_title"].set(title)
        self.settings.channel, self.settings.channel_title = ref, title

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
        for job in (getattr(self, "_pump_job", None), getattr(self.steps[-1], "_job", None) if hasattr(self, "steps") else None):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
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
    App().mainloop()
    return 0
