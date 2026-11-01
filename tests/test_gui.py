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


def _idx():
    return {c.__name__: i for i, c in enumerate(gui.STEPS)} if HAVE_TK else {}


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
    STEP = _idx()
    TEST, BACKTEST, PANEL = STEP.get("TestStep"), STEP.get("BacktestStep"), STEP.get("PanelStep")

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
        gui.apply_palette("light")
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
        a.show(self.TEST)
        t = a.steps[self.TEST]
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
        a.show(self.PANEL)
        text = a.steps[self.PANEL].summary.cget("text")
        self.assertIn("Canal A (lote 0.02)", text)
        self.assertIn("Canal B (lote 0.05, 1 TP, DESACTIVADO)", text)

    def test_backtest_step_is_per_channel(self):
        a = self.app
        a.settings.channels = [ChannelCfg(4, "@a", "Canal A"), ChannelCfg(9, "@b", "Canal B")]
        a.show(self.BACKTEST)
        b = a.steps[self.BACKTEST]
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

    # ---- protecciones, panel, tema y respaldo
    def test_protections_step_saves_values_and_blocks_invalid_ones(self):
        a = self.app
        a.v["api_id"].set("12345678"); a.v["api_hash"].set("a" * 32); a.v["phone"].set("+50499998888")
        a.show(self.STEP["ProtectionsStep"])
        a.v["daily_loss_limit_pct"].set("3"); a.v["max_signals_per_day"].set("5"); a.v["max_spread_usd"].set("0.8")
        a.v["news_mode"].set("manual"); a.v["news_manual"].set("Fri 08:30 NFP"); a.v["auto_be"].set(True)
        a.v["close_on_limit"].set(True)
        a.goto(self.STEP["ProtectionsStep"] + 1)
        self.assertEqual(a.index, self.STEP["ProtectionsStep"] + 1)
        cfg = bot_main.load_config(str(Path(self.tmp.name) / "config.ini"))
        self.assertEqual((cfg["daily_loss_limit_pct"], cfg["max_signals_per_day"], cfg["max_spread_usd"]), (3.0, 5, 0.8))
        self.assertEqual((cfg["news_mode"], cfg["news_manual"], cfg["auto_be"], cfg["close_on_limit"]),
                         ("manual", "Fri 08:30 NFP", True, True))
        a.show(self.STEP["ProtectionsStep"])
        a.v["news_manual"].set("viernes a las 8")                           # formato inválido
        a.goto(self.STEP["ProtectionsStep"] + 1)
        self.assertEqual(a.index, self.STEP["ProtectionsStep"])
        self.assertTrue(any("Noticias manuales" in m for _, _, m in self.dialogs))
        a.v["news_manual"].set("Fri 08:30 NFP"); a.v["daily_loss_limit_pct"].set("90")
        a.goto(self.STEP["ProtectionsStep"] + 1)
        self.assertEqual(a.index, self.STEP["ProtectionsStep"])             # 90 % no es un límite razonable

    def test_panel_inbox_lists_decisions_newest_first(self):
        from autotrade.eventlog import EventLog
        log = EventLog(Path(self.tmp.name) / "signals.jsonl")
        log.add(channel=1, title="IC GOLD", msg_id=1, status="operada", detail="2/2 órdenes", text="SELL GOLD 2650")
        log.add(channel=2, title="Gold Pro", msg_id=7, status="revisar", detail="parece una señal", text="BUY GOLD 3900 ya")
        log.add(channel=1, title="IC GOLD", msg_id=2, status="ignorada", detail="fuera de horario", text="x")
        self.app.settings.channels = [ChannelCfg(1, "-1", "IC GOLD")]
        self.app.show(self.PANEL)
        p = self.app.steps[self.PANEL]
        rows = [p.inbox.item(i, "values") for i in p.inbox.get_children()]
        self.assertEqual([r[2] for r in rows], ["⏭ Ignorada", "🔎 Revisar", "✅ Operada"])     # más nuevo arriba
        self.assertEqual([r[1] for r in rows], ["IC GOLD", "Gold Pro", "IC GOLD"])
        self.assertIn("fuera de horario", rows[0][3])
        self.assertEqual(p.inbox.item(p.inbox.get_children()[1], "tags"), ("warn",))
        log.add(channel=1, title="IC GOLD", msg_id=3, status="operada", detail="1/1", text="y")
        p.refresh()                                                                     # se actualiza al cambiar el archivo
        self.assertEqual(len(p.inbox.get_children()), 4)

    def test_pause_button_writes_control_file_and_shows_it(self):
        from autotrade.control import Control
        a = self.app
        a.settings.channels = [ChannelCfg(1, "-1", "A"), ChannelCfg(2, "-2", "B")]
        a.show(self.PANEL)
        p = a.steps[self.PANEL]
        p.toggle_pause()
        self.assertTrue(Control(Path(self.tmp.name) / "control.json").read()["paused"])
        self.assertIn("EN PAUSA", p.st.cget("text"))
        self.assertIn("Reanudar", p.pause_btn.cget("text"))
        p.toggle_pause()
        self.assertFalse(Control(Path(self.tmp.name) / "control.json").read()["paused"])
        self.assertIn("Pausar", p.pause_btn.cget("text"))
        Control(Path(self.tmp.name) / "control.json").set_paused(True, 2)               # /pausa 2 desde Telegram
        p.refresh()
        self.assertIn("B (lote 0.02, EN PAUSA)", p.summary.cget("text"))

    def test_daily_limit_is_shown_and_can_be_resumed_from_the_panel(self):
        import time as _t
        from autotrade.risk import RiskGuard
        a = self.app
        a.settings.tz_offset_hours = 0.0
        g = RiskGuard(Path(self.tmp.name) / "risk_state.json", loss_pct=1.0, tz_hours=0.0)
        g.check(_t.time(), 1000, 0, 0)
        self.assertFalse(g.check(_t.time(), 980, -20, 0).allowed)
        a.show(self.PANEL)
        p = a.steps[self.PANEL]
        self.assertIn("Límite diario alcanzado", p.st.cget("text"))
        p.resume_today()
        self.assertNotIn("Límite diario", p.st.cget("text"))
        bot = RiskGuard(Path(self.tmp.name) / "risk_state.json", loss_pct=1.0, tz_hours=0.0)
        self.assertTrue(bot.state.override)

    def test_performance_tab_shows_real_results_per_channel(self):
        a = self.app
        a.settings.channels = [ChannelCfg(1, "-1", "IC GOLD")]
        import time as _t
        now = _t.time()
        deals = [{"position_id": 1, "entry": "in", "time": now - 60, "comment": "tg1.5:1"},
                 {"position_id": 1, "entry": "out", "time": now - 30, "profit": 12.5, "swap": 0, "commission": -0.5, "comment": "[tp]"},
                 {"position_id": 2, "entry": "out", "time": now - 30, "profit": 3.0, "swap": 0, "commission": 0, "comment": "[tp]"}]
        real = mt5_helper.fetch_deals
        mt5_helper.fetch_deals = lambda *a, **k: {"ok": True, "error": "", "deals": deals, "server_offset": 0}
        try:
            a.show(self.PANEL)
            p = a.steps[self.PANEL]
            p.load_perf()
        finally:
            mt5_helper.fetch_deals = real
        rows = [p.perf.item(i, "values") for i in p.perf.get_children()]
        self.assertEqual(rows[0][0], "IC GOLD")
        self.assertEqual((rows[0][1], rows[0][2], rows[0][3]), ("1", "1", "100%"))
        self.assertEqual(rows[0][7], "+12.00")
        self.assertEqual(rows[1][0], "Sin etiqueta (otras)")
        mt5_helper.fetch_deals = lambda *a, **k: {"ok": False, "error": "MT5 cerrado"}
        try:
            p.load_perf()
        finally:
            mt5_helper.fetch_deals = real
        self.assertIn("MT5 cerrado", p.perf_note.cget("text"))

    def test_backup_export_import_and_theme_restart(self):
        from tkinter import filedialog
        a = self.app
        a.settings.channels = [ChannelCfg(1, "@goldpro", "Gold Pro")]
        a.show(self.PANEL)
        p = a.steps[self.PANEL]
        dest = str(Path(self.tmp.name) / "copia.zip")
        orig = (filedialog.asksaveasfilename, filedialog.askopenfilename)
        filedialog.asksaveasfilename = lambda **k: dest
        filedialog.askopenfilename = lambda **k: dest
        try:
            p.export_backup()
            self.assertTrue(Path(dest).exists())
            (Path(self.tmp.name) / "config.ini").write_text("[telegram]\napi_id = 9\n", encoding="utf-8")   # «se estropea»
            a.restart = lambda save=True: setattr(a, "restart_requested", True)
            p.import_backup()
        finally:
            filedialog.asksaveasfilename, filedialog.askopenfilename = orig
        self.assertEqual([c.ref for c in a.settings.channels], ["@goldpro"])             # volvió la configuración guardada
        self.assertTrue(a.restart_requested)
        self.assertTrue(list(Path(self.tmp.name).glob("config.ini.bak-*")))
        # tema
        a.restart_requested = False
        p.theme_var.set("Oscuro")
        p.apply_theme()
        self.assertEqual(a.v["theme"].get(), "dark")
        self.assertTrue(a.restart_requested)

    def test_update_check_messages(self):
        from autotrade import updates
        a = self.app
        a.show(self.PANEL)
        p = a.steps[self.PANEL]
        real = updates.check
        try:
            updates.check = lambda cur: {"status": "newer", "latest": "v9", "url": "https://x/r", "message": "Hay una versión nueva: v9"}
            p.check_updates()
            self.assertIn("nueva", p.upd_msg.cget("text"))
            self.assertEqual(p._upd_url, "https://x/r")
            updates.check = lambda cur: {"status": "none", "latest": "", "url": "u", "message": "Todavía no hay versiones publicadas"}
            p.check_updates()
            self.assertIn("Todavía no hay", p.upd_msg.cget("text"))
        finally:
            updates.check = real

    def test_dark_theme_applies_and_both_palettes_have_readable_buttons(self):
        from tkinter import ttk
        def lum(c):
            r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
            f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
            return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
        ratio = lambda a, b: (max(lum(a), lum(b)) + 0.05) / (min(lum(a), lum(b)) + 0.05)
        for theme in ("light", "dark"):
            (Path(self.tmp.name) / "config.ini").write_text(sl.render_config(sl.Settings(theme=theme)), encoding="utf-8")
            self.app.destroy()
            self.app = gui.App(sync=True)
            self.assertEqual(gui.BG, gui.DARK["BG"] if theme == "dark" else gui.LIGHT["BG"])
            st = ttk.Style(self.app)
            for name in ("TButton", "Accent.TButton", "Danger.TButton", "Small.TButton"):
                for state in ([], ["active"], ["pressed"]):
                    bg, fg = st.lookup(name, "background", state), st.lookup(name, "foreground", state)
                    self.assertGreaterEqual(ratio(bg, fg), 4.5, f"{theme} {name} {state}")
                bg, fg = st.lookup(name, "background", ["disabled"]), st.lookup(name, "foreground", ["disabled"])
                self.assertGreaterEqual(ratio(bg, fg), 2.5, f"{theme} {name} deshabilitado")
            for name in ("TEntry", "Treeview"):                                           # texto sobre el campo
                self.assertGreaterEqual(ratio(st.lookup(name, "fieldbackground"), st.lookup(name, "foreground")), 7,
                                        f"{theme} {name}")
            for kind, (bg, fg) in gui.TINT.items():                                      # avisos de color
                self.assertGreaterEqual(ratio(bg, fg), 4.5, f"{theme} aviso {kind}")
            for key in ("OK", "BAD", "WARN", "MUTED", "LINK"):                           # textos de color sobre el fondo
                self.assertGreaterEqual(ratio(gui.BG, getattr(gui, key)), 4.5, f"{theme} {key} sobre fondo")
            self.assertGreaterEqual(ratio(gui.SIDEBAR, gui.SIDE_TEXT), 4.5, f"{theme} barra lateral")

    def test_theme_auto_follows_system_and_restart_flag(self):
        real = gui.detect_system_dark
        try:
            gui.detect_system_dark = lambda: True
            self.assertEqual(gui.resolve_theme("auto"), "dark")
            self.assertEqual(gui.resolve_theme("light"), "light")
            gui.detect_system_dark = lambda: False
            self.assertEqual(gui.resolve_theme("auto"), "light")
            self.assertEqual(gui.resolve_theme("dark"), "dark")
        finally:
            gui.detect_system_dark = real
        self.app.restart()
        self.assertTrue(self.app.restart_requested)
        self.app = gui.App(sync=True)            # tearDown espera una ventana viva

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
            self.app.show(self.PANEL)
            self.app.steps[self.PANEL].start()
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
            self.app.show(self.PANEL)
            self.answer = False                        # el usuario cancela
            self.app.steps[self.PANEL].start()
            self.assertEqual(launched, [])
            self.answer = True
            self.app.tg = self.tg                      # el asistente tiene la sesión de Telegram abierta
            self.app.steps[self.PANEL].start()
        finally:
            subprocess.Popen = real_popen
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0][-1], "--bot")
        self.assertIn("REAL", self.app.steps[self.PANEL].mode.cget("text"))
        self.assertIsNone(self.app.tg)                 # sesión de Telegram liberada antes de lanzar el bot
        self.assertIn(("close",), self.tg.calls)

    @unittest.skipUnless(os.name == "posix", "usa procesos POSIX")
    def test_stop_kills_the_process_tree(self):
        self.configured()
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             **paths.popen_kwargs(detached=True))
        (Path(self.tmp.name) / "bot.pid").write_text(str(p.pid))
        self.app.show(self.PANEL)
        self.assertTrue(self.app.bot_running())
        self.assertIn("FUNCIONANDO", self.app.steps[self.PANEL].st.cget("text"))
        self.app.steps[self.PANEL].stop()
        p.wait(timeout=10)
        self.assertFalse(self.app.bot_running())

    def test_panel_shows_log_tail_and_summary(self):
        self.configured()
        (Path(self.tmp.name) / "autotrade.log").write_text("línea 1\nSeñal 7 ignorada\n", encoding="utf-8")
        self.app.show(self.PANEL)
        self.assertIn("Señal 7 ignorada", self.app.steps[self.PANEL].log.get("1.0", "end"))
        self.assertIn("Gold Pro", self.app.steps[self.PANEL].summary.cget("text"))

    def test_app_starts_on_panel_when_already_configured(self):
        self.configured()
        self.app.save()
        self.app.destroy()
        self.app = gui.App(sync=True)
        self.assertEqual(self.app.index, self.PANEL)


if __name__ == "__main__":
    unittest.main()
