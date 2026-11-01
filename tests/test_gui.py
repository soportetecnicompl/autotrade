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
    from autotrade.channels import ChannelCfg


class FakeTG:
    def __init__(self, needs_password=False):
        self.needs_password, self.calls, self.folders_error = needs_password, [], ""

    def connect(self): return False
    def request_code(self, phone): self.calls.append(("code", phone))
    def sign_in(self, code):
        self.calls.append(("sign_in", code))
        return "password" if self.needs_password else "ok"
    def sign_in_password(self, pw): self.calls.append(("pw", pw))
    def me_name(self): return "María Pérez"
    def overview(self):
        ds = [{"id": -1001, "name": "IC GOLD VIP", "username": "", "kind": "canal", "muted": False, "archived": False, "unread": 1},
              {"id": -1002, "name": "Gold Pro", "username": "goldpro", "kind": "canal", "muted": False, "archived": False, "unread": 0},
              {"id": -1003, "name": "Familia", "username": "", "kind": "grupo", "muted": False, "archived": False, "unread": 0}]
        return {"channels": ds, "folders": [{"id": 2, "title": "Señales", "members": [-1001, -1002]},
                                            {"id": 3, "title": "Personal", "members": [-1003]}],
                "folders_error": self.folders_error}
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
        c.toggle_iid("1")                          # «Gold Pro» (@goldpro)
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
        self.assertEqual([(c["ref"], c["title"]) for c in cfg["channels"]], [("@goldpro", "Gold Pro")])
        self.assertEqual((cfg["lot_size"], cfg["max_tps"], cfg["dry_run"]), (0.04, 3, False))
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
        self.assertNotIn("✔", a.side_chips[4].cget("text"))       # «Operativa» aún no visitado
        a.show(4); a.show(0)
        self.assertIn("✔", a.side_chips[4].cget("text"))

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

    # ---- varios canales y carpetas
    def load_channels(self):
        a = self.app
        a.v["api_id"].set("12345678"); a.v["api_hash"].set("a" * 32); a.v["phone"].set("+50499998888")
        a.tg_user = "María"; a.tg = self.tg
        a.show(2)
        return a.steps[2]

    def test_folders_filter_the_channel_list(self):
        c = self.load_channels()
        self.assertEqual(len(c.visible), 3)                              # «Todos»
        self.assertEqual(list(c.folder_cb["values"]), [c.ALL, "Señales", "Personal"])
        c.folder_var.set("Señales"); c.refill()
        self.assertEqual([d["name"] for d in c.visible], ["IC GOLD VIP", "Gold Pro"])
        c.folder_var.set("Personal"); c.refill()
        self.assertEqual([d["name"] for d in c.visible], ["Familia"])
        c.folder_var.set(c.ALL); c.q.set("gold"); c.refill()
        self.assertEqual(len(c.visible), 2)                              # búsqueda dentro de la vista

    def test_pick_several_channels_and_set_per_channel_options(self):
        a, c = self.app, self.load_channels()
        c.folder_var.set("Señales"); c.refill()
        c.add_visible()                                                  # agrega los 2 de la carpeta
        self.assertEqual([x.ref for x in a.settings.channels], ["-1001", "@goldpro"])
        self.assertEqual([x.num for x in a.settings.channels], [1, 2])
        c.chosen.selection_set("2"); c.on_pick()
        c.ed_lot.set("0.05"); c.ed_tps.set("3"); c.apply_edit()
        ch = a.settings.channels[1]
        self.assertEqual((ch.lot_size, ch.max_tps), (0.05, 3))
        self.assertEqual(c.chosen.item("2", "values")[2:], ("0.05", "3"))
        c.ed_lot.set(""); c.ed_tps.set("General"); c.apply_edit()
        self.assertEqual((ch.lot_size, ch.max_tps), (0.0, 0))            # vacío/General = usar los generales
        c.toggle_enabled("2")
        self.assertFalse(ch.enabled)
        c.remove_selected()
        self.assertEqual([x.num for x in a.settings.channels], [1])

    def test_toggle_in_list_adds_and_removes_and_numbers_stay_stable(self):
        a, c = self.app, self.load_channels()
        c.toggle_iid("0"); c.toggle_iid("1")                              # -1001 (#1) y @goldpro (#2)
        c.toggle_iid("0")                                                 # quita #1
        c.toggle_iid("2")                                                 # «Familia» -> nuevo número 3, no reutiliza 1
        self.assertEqual([(x.num, x.ref) for x in a.settings.channels], [(2, "@goldpro"), (3, "-1003")])
        self.assertEqual(c.avail.item("0", "values")[0], "☐")
        self.assertEqual(c.avail.item("1", "values")[0], "☑")

    def test_manual_channel_and_duplicates(self):
        a, c = self.app, self.load_channels()
        c.manual.set("micanal"); c.use_manual()
        c.manual.set("@micanal"); c.use_manual()                          # duplicado: se ignora
        c.manual.set("-1007"); c.use_manual()
        self.assertEqual([x.ref for x in a.settings.channels], ["@micanal", "-1007"])

    def test_cannot_advance_without_an_active_channel(self):
        a, c = self.app, self.load_channels()
        a.tg_user = "María"
        a.goto(3)
        self.assertEqual(a.index, 2)
        c.toggle_iid("0"); c.toggle_enabled("1")                           # elegido pero pausado
        a.goto(3)
        self.assertEqual(a.index, 2)
        c.toggle_enabled("1")
        a.goto(3)
        self.assertEqual(a.index, 3)

    def test_folder_errors_do_not_block_choosing_channels(self):
        self.tg.folders_error = "RPCError: FILTERS_NOT_AVAILABLE"
        a = self.app
        c = self.load_channels()
        self.assertEqual(len(c.visible), 3)
        self.assertIn("carpetas", c.st.cget("text"))

    def test_panel_summary_lists_each_channel(self):
        a = self.app
        a.settings.channels = [ChannelCfg(1, "@a", "Canal A"), ChannelCfg(2, "@b", "Canal B", enabled=False, lot_size=0.05, max_tps=1)]
        a.show(10)
        text = a.steps[10].summary.cget("text")
        self.assertIn("Canal A (lote 0.02)", text)
        self.assertIn("Canal B (lote 0.05, 1 TP, PAUSADO)", text)

    def test_backtest_step_is_per_channel(self):
        a = self.app
        a.settings.channels = [ChannelCfg(4, "@a", "Canal A"), ChannelCfg(9, "@b", "Canal B")]
        a.show(8)
        b = a.steps[8]
        self.assertEqual(list(b.chan_cb["values"]), ["Canal A  (#4)", "Canal B  (#9)"])
        self.assertEqual(b.selected_channel().num, 4)
        self.assertTrue(str(b.report_path()).endswith("report_c4.html"))
        b.chan.set("Canal B  (#9)")
        self.assertTrue(str(b.report_path()).endswith("report_c9.html"))

    def test_buttons_define_readable_colors_for_every_state(self):
        """Regresión: texto de botones invisible. Cada estilo debe fijar texto Y fondo contrastados."""
        from tkinter import ttk
        st = ttk.Style(self.app)
        def lum(c):
            r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
            f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
            return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
        def ratio(a, b):
            la, lb = sorted((lum(a), lum(b)), reverse=True)
            return (la + 0.05) / (lb + 0.05)
        for name in ("TButton", "Accent.TButton", "Danger.TButton", "Small.TButton"):
            bg, fg = st.lookup(name, "background"), st.lookup(name, "foreground")
            self.assertGreaterEqual(ratio(bg, fg), 4.5, f"{name}: texto poco legible en reposo")
            for state in (["active"], ["pressed"]):
                bg2 = st.lookup(name, "background", state)
                fg2 = st.lookup(name, "foreground", state)
                self.assertGreaterEqual(ratio(bg2, fg2), 4.5, f"{name} {state}")
            bg3, fg3 = st.lookup(name, "background", ["disabled"]), st.lookup(name, "foreground", ["disabled"])
            self.assertGreaterEqual(ratio(bg3, fg3), 2.5, f"{name}: deshabilitado ilegible")

    def test_welcome_label_texts_are_spanish_days(self):
        self.assertEqual(gui.days_label("Mon-Fri"), "Lun, Mar, Mié, Jue, Vie")
        self.assertEqual(gui.days_to_text({0, 2}), "Mon,Wed")

    # ---- panel
    def configured(self):
        a = self.app
        a.v["api_id"].set("12345678"); a.v["api_hash"].set("a" * 32); a.v["phone"].set("+50499998888")
        a.settings.channels = [ChannelCfg(1, "@goldpro", "Gold Pro")]
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
