"""Pruebas de la interfaz. Se saltan si no hay tkinter o pantalla.
En Linux: xvfb-run -a python3 -m unittest tests.test_gui
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    if os.environ.get("AUTOTRADE_SKIP_GUI"):
        raise RuntimeError("pruebas de interfaz desactivadas")
    import tkinter as tk
    _r = tk.Tk()
    _r.destroy()
    HAVE_TK = True
except Exception:  # noqa: BLE001 - sin tkinter, sin DISPLAY o desactivadas
    HAVE_TK = False

if HAVE_TK:
    from tkinter import messagebox
    import main as bot_main
    from autotrade import gui, mt5_helper, paths, setup_logic as sl


class FakeTG:
    def __init__(self, needs_password=False):
        self.needs_password, self.calls = needs_password, []

    def connect(self): return False
    def request_code(self, phone): self.calls.append(("code", phone))
    def sign_in(self, code):
        self.calls.append(("sign_in", code))
        return "password" if self.needs_password else "ok"
    def sign_in_password(self, pw): self.calls.append(("pw", pw))
    def me_name(self): return "María Pérez"
    def channels(self):
        return [{"id": -1001, "name": "IC GOLD VIP", "username": "", "kind": "canal"},
                {"id": -1002, "name": "Gold Pro", "username": "goldpro", "kind": "canal"}]
    def send_to_me(self, text): self.calls.append(("me", text))
    def close(self): self.calls.append(("close",))


PROBE_OK = {"ok": True, "error": "", "account": {"login": 5012345, "server": "Demo", "balance": 1000.0,
            "currency": "USD", "mode": "DEMO", "algo_trading": True}, "gold_symbols": ["XAUUSD", "XAUUSDm"],
            "symbol_ok": True, "tick_time": None, "bid": 3914.2, "ask": 3914.5}


@unittest.skipUnless(HAVE_TK, "requiere tkinter y una pantalla (xvfb-run)")
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["AUTOTRADE_HOME"] = self.tmp.name
        self.dialogs = []
        self.answer = True
        for n in ("showwarning", "showinfo", "showerror"):
            setattr(messagebox, n, lambda t, m, *a, _n=n, **k: self.dialogs.append((_n, t, m)))
        messagebox.askyesno = lambda t, m, **k: (self.dialogs.append(("askyesno", t, m)), self.answer)[1]
        self._probe = mt5_helper.probe
        mt5_helper.probe = lambda *a, **k: PROBE_OK
        self.app = gui.App(sync=True)
        self.tg = FakeTG()
        self.app.make_tg = lambda: (setattr(self.app, "tg", self.tg), self.tg)[1]
        self.app.update()

    def tearDown(self):
        mt5_helper.probe = self._probe
        try:
            self.app.destroy()
        except Exception:  # noqa: BLE001
            pass
        os.environ.pop("AUTOTRADE_HOME", None)
        self.tmp.cleanup()

    def connect_telegram(self):
        a = self.app
        a.v["api_id"].set("12345678"); a.v["api_hash"].set("a" * 32); a.v["phone"].set("+50499998888")
        a.show(1)
        t = a.steps[1]
        t.send_code()
        t.code.set("12345")
        t.confirm_code()
        return t

    # ---- navegación y validaciones
    def test_cannot_advance_without_telegram(self):
        a = self.app
        a.show(1)
        a.goto(2)
        self.assertEqual(a.index, 1)
        self.assertTrue(any("Telegram" in m for _, _, m in self.dialogs))

    def test_bad_telegram_data_is_explained(self):
        a = self.app
        a.show(1)
        a.v["api_id"].set("abc")
        a.steps[1].send_code()
        text = " ".join(m for _, _, m in self.dialogs)
        self.assertIn("api_id", text)
        self.assertIn("api_hash", text)
        self.assertNotIn(("code",), [c[:1] for c in self.tg.calls])

    def test_full_flow_saves_a_config_the_bot_can_read(self):
        a = self.app
        self.connect_telegram()
        self.assertEqual(a.tg_user, "María Pérez")
        self.assertEqual([c[0] for c in self.tg.calls][:2], ["code", "sign_in"])
        a.goto(2)
        self.assertEqual(a.index, 2)
        c = a.steps[2]
        c.load()
        c.tree.selection_set("1")
        c.pick()
        a.goto(3)
        a.steps[3].probe()
        self.assertIn("5012345", a.steps[3].st.cget("text"))
        a.v["lot_size"].set("0.04"); a.v["max_tps"].set("3"); a.v["dry_run"].set(False)
        a.goto(4); a.goto(5)
        a.v["filter_enabled"].set(True)
        for i in (5, 6):                       # sábado y domingo
            a.steps[5].days[i].set(True)
        a.goto(6)
        cfg = bot_main.load_config(str(Path(self.tmp.name) / "config.ini"))
        self.assertEqual((cfg["channel"], cfg["lot_size"], cfg["max_tps"], cfg["dry_run"]),
                         ("@goldpro", 0.04, 3, False))
        self.assertFalse(cfg["schedule"].always)
        self.assertEqual(cfg["api_id"], 12345678)
        self.assertEqual(sl.load_settings(Path(self.tmp.name) / "config.ini").trading_days,
                         "Mon,Tue,Wed,Thu,Fri,Sat,Sun")

    def test_two_step_verification_branch(self):
        self.tg.needs_password = True
        t = self.connect_telegram()
        self.assertIsNone(self.app.tg_user)
        self.assertIn("dos pasos", t.st.cget("text"))
        t.pw.set("secreto")
        t.confirm_password()
        self.assertEqual(self.app.tg_user, "María Pérez")
        self.assertIn(("pw", "secreto"), self.tg.calls)

    def test_mt5_warnings_for_real_account_and_algo_off(self):
        a = self.app
        a.show(3)
        real = dict(PROBE_OK, account=dict(PROBE_OK["account"], mode="REAL", algo_trading=False))
        a.steps[3].show_result(real)
        text = a.steps[3].st.cget("text")
        self.assertIn("REAL", text)
        self.assertIn("Algo Trading", text)
        a.steps[3].show_result({"ok": False, "error": "No se pudo conectar"})
        self.assertIn("✖", a.steps[3].st.cget("text"))

    def test_schedule_days_survive_going_back(self):
        a = self.app
        a.show(5)
        s = a.steps[5]
        for i, var in s.days.items():
            var.set(i in (0, 2))                  # solo lun y mié
        a.show(4)                                  # retroceder
        self.assertEqual(a.settings.trading_days, "Mon,Wed")
        a.show(5)
        self.assertEqual({i for i, v in s.days.items() if v.get()}, {0, 2})

    def test_invalid_hours_block_advance(self):
        a = self.app
        a.show(5)
        a.v["filter_enabled"].set(True)
        a.v["trading_hours"].set("8 a 12")
        a.goto(6)
        self.assertEqual(a.index, 5)
        self.assertTrue(any("Filtro de horario" in m for _, _, m in self.dialogs))

    def test_sidebar_checks_only_visited_steps(self):
        a = self.app
        a.show(0)
        self.assertNotIn("✔", a.side_labels[4].cget("text"))      # «Operativa» aún no visitado
        a.show(4); a.show(0)
        self.assertIn("✔", a.side_labels[4].cget("text"))

    # ---- probar mensaje
    def test_message_analyzer(self):
        a = self.app
        a.show(7)
        t = a.steps[7]
        t.analyze()
        out = t.out.get("1.0", "end")
        self.assertIn("SEÑAL SELL", out)
        self.assertIn("SELL MARKET 0.01 lotes", out)
        t.box.delete("1.0", "end"); t.box.insert("1.0", "Buenos días equipo, vamos con todo")
        t.analyze()
        self.assertIn("se IGNORARÍA", t.out.get("1.0", "end"))
        t.box.delete("1.0", "end"); t.box.insert("1.0", "110 pips, now can set BE")
        t.analyze()
        self.assertIn("break-even", t.out.get("1.0", "end"))
        t.box.delete("1.0", "end"); t.box.insert("1.0", "BUY GOLD 3900 SL 3890 TP 3910")
        t.price.set("3950")                                     # entrada demasiado lejos
        t.analyze()
        self.assertIn("NO se operaría", t.out.get("1.0", "end"))

    def test_run_logged_streams_child_output_and_exit_code(self):
        code = ("import os,time; f=open(os.environ['AUTOTRADE_OUTFILE'],'a',encoding='utf-8',buffering=1);"
                "f.write('uno\\n'); time.sleep(0.7); f.write('dos ñ\\n'); raise SystemExit(3)")
        got = []
        rc = gui.run_logged([sys.executable, "-c", code], Path(self.tmp.name) / "o.log", got.append)
        self.assertEqual(rc, 3)
        self.assertEqual("".join(got), "uno\ndos ñ\n")
        self.assertGreaterEqual(len(got), 2)                     # llegó en trozos, no todo al final

    def test_theme_is_clam_so_blue_buttons_are_readable_on_windows(self):
        from tkinter import ttk
        self.assertEqual(ttk.Style(self.app).theme_use(), "clam")

    def test_welcome_check_shows_the_real_missing_component_error(self):
        from autotrade import selfcheck
        fake = [("telethon", True, "1.45", True), ("tkinter", True, "8.6", True),
                ("numpy", False, "ImportError: numpy._core.multiarray failed to import", True),
                ("MetaTrader5", False, "ImportError: numpy", True)]
        real = selfcheck.check_imports
        selfcheck.check_imports = lambda *a, **k: fake
        try:
            self.app.show(0)
        finally:
            selfcheck.check_imports = real
        text = self.app.steps[0].rows["lib"].cget("text")
        self.assertIn("numpy", text)
        self.assertIn("multiarray failed to import", text)
        self.assertIn("✖", text)

    def test_welcome_label_texts_are_spanish_days(self):
        self.assertEqual(gui.days_label("Mon-Fri"), "Lun, Mar, Mié, Jue, Vie")
        self.assertEqual(gui.days_to_text({0, 2}), "Mon,Wed")

    # ---- panel
    def configured(self):
        a = self.app
        a.v["api_id"].set("12345678"); a.v["api_hash"].set("a" * 32); a.v["phone"].set("+50499998888")
        a.set_channel("@goldpro", "Gold Pro")
        a.sync_settings()

    def test_start_requires_valid_config(self):
        launched = []
        real_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **k: launched.append(a[0])
        try:
            self.app.show(10)
            self.app.steps[10].start()
        finally:
            subprocess.Popen = real_popen
        self.assertEqual(launched, [])
        self.assertTrue(any("Faltan datos" in t for _, t, _ in self.dialogs))

    def test_start_real_mode_needs_confirmation(self):
        self.configured()
        self.app.v["dry_run"].set(False)
        launched = []
        real_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **k: launched.append(a[0])
        try:
            self.app.show(10)
            self.answer = False                        # el usuario cancela
            self.app.steps[10].start()
            self.assertEqual(launched, [])
            self.answer = True
            self.app.tg = self.tg                      # el asistente tiene la sesión de Telegram abierta
            self.app.steps[10].start()
        finally:
            subprocess.Popen = real_popen
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0][-1], "--bot")
        self.assertIn("REAL", self.app.steps[10].mode.cget("text"))
        self.assertIsNone(self.app.tg)                 # sesión de Telegram liberada antes de lanzar el bot
        self.assertIn(("close",), self.tg.calls)

    @unittest.skipUnless(os.name == "posix", "usa procesos POSIX")
    def test_stop_kills_the_process_tree(self):
        self.configured()
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             **paths.popen_kwargs(detached=True))
        (Path(self.tmp.name) / "bot.pid").write_text(str(p.pid))
        self.app.show(10)
        self.assertTrue(self.app.bot_running())
        self.assertIn("FUNCIONANDO", self.app.steps[10].st.cget("text"))
        self.app.steps[10].stop()
        p.wait(timeout=10)
        self.assertFalse(self.app.bot_running())

    def test_panel_shows_log_tail_and_summary(self):
        self.configured()
        (Path(self.tmp.name) / "autotrade.log").write_text("línea 1\nSeñal 7 ignorada\n", encoding="utf-8")
        self.app.show(10)
        self.assertIn("Señal 7 ignorada", self.app.steps[10].log.get("1.0", "end"))
        self.assertIn("Gold Pro", self.app.steps[10].summary.cget("text"))

    def test_app_starts_on_panel_when_already_configured(self):
        self.configured()
        self.app.save()
        self.app.destroy()
        self.app = gui.App(sync=True)
        self.assertEqual(self.app.index, len(gui.STEPS) - 1)


if __name__ == "__main__":
    unittest.main()
