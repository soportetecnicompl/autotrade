import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from autotrade import backup, news, perf, remote, updates
from autotrade.control import Control
from autotrade.eventlog import EventLog
from autotrade.risk import RiskGuard, day_key, seconds_into_day


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


class RiskTests(unittest.TestCase):
    def guard(self, **kw):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return RiskGuard(Path(self.tmp.name) / "risk.json", tz_hours=-6, **kw)

    def test_day_is_counted_in_the_users_timezone(self):
        # 02:00 UTC del día 5 = 20:00 del día 4 en Honduras (UTC-6)
        self.assertEqual(day_key(utc(2026, 11, 5, 2), -6), "2026-11-04")
        self.assertEqual(day_key(utc(2026, 11, 5, 7), -6), "2026-11-05")
        self.assertEqual(seconds_into_day(utc(2026, 11, 5, 7, 30), -6), 90 * 60)

    def test_loss_limit_trips_once_and_stays_tripped(self):
        g = self.guard(loss_pct=3.0)
        now = utc(2026, 11, 5, 15)
        v = g.check(now, balance=1000, realized=0, floating=-10)            # -1% : bien
        self.assertEqual((v.allowed, v.just_tripped), (True, False))
        self.assertEqual(g.state.start_balance, 1000)
        v = g.check(now + 60, balance=980, realized=-20, floating=-12)      # -32 USD >= 30 (3% de 1000)
        self.assertEqual((v.allowed, v.just_tripped), (False, True))
        self.assertIn("32.00", v.reason)
        v = g.check(now + 120, balance=980, realized=-20, floating=0)       # aunque mejore, sigue cortado hoy
        self.assertEqual((v.allowed, v.just_tripped), (False, False))

    def test_start_balance_accounts_for_what_was_already_realized(self):
        g = self.guard(loss_pct=5.0)
        # el bot arranca a media jornada con 940 de saldo tras perder 60 hoy -> saldo inicial 1000
        g.check(utc(2026, 11, 5, 16), balance=940, realized=-60, floating=0)
        self.assertEqual(g.state.start_balance, 1000)
        self.assertFalse(g.check(utc(2026, 11, 5, 16, 5), 940, -60, -2).allowed)      # 62 >= 50

    def test_new_day_resets_and_state_survives_restart(self):
        g = self.guard(loss_pct=1.0)
        g.check(utc(2026, 11, 5, 15), 1000, 0, 0)
        g.check(utc(2026, 11, 5, 16), 985, -15, 0)
        self.assertTrue(g.state.tripped)
        again = RiskGuard(g.path, loss_pct=1.0, tz_hours=-6)                # reinicio del bot el mismo día
        self.assertTrue(again.state.tripped)
        self.assertFalse(again.check(utc(2026, 11, 5, 17), 985, -15, 0).allowed)
        v = again.check(utc(2026, 11, 6, 15), 985, 0, 0)                     # día siguiente: limpio
        self.assertTrue(v.allowed)
        self.assertEqual(again.state.start_balance, 985)

    def test_signal_limit_and_manual_resume(self):
        g = self.guard(max_signals=2)
        now = utc(2026, 11, 5, 15)
        for _ in range(2):
            self.assertTrue(g.check(now, 1000, 0, 0).allowed)
            g.register_signal()
        v = g.check(now, 1000, 0, 0)
        self.assertEqual((v.allowed, v.just_tripped), (False, True))
        self.assertIn("2 señales", v.reason)
        g.resume()
        self.assertTrue(g.check(now + 5, 1000, 0, 0).allowed)               # el usuario decidió seguir hoy
        g.register_signal()
        self.assertTrue(g.check(now + 10, 1000, -500, -500).allowed)         # con override ya no corta

    def test_disabled_never_blocks_and_broken_file_is_ignored(self):
        g = self.guard()
        self.assertFalse(g.enabled)
        self.assertTrue(g.check(utc(2026, 11, 5), 1000, -900, -90).allowed)
        g.path.write_text("{no es json", encoding="utf-8")
        self.assertEqual(RiskGuard(g.path).state.day, "")


class RiskExternalOverrideTests(unittest.TestCase):
    def test_bot_adopts_resume_written_by_the_panel_in_another_process(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "risk.json"
            bot = RiskGuard(path, loss_pct=1.0, tz_hours=0)
            bot.check(utc(2026, 11, 5, 15), 1000, 0, 0)
            self.assertFalse(bot.check(utc(2026, 11, 5, 15, 5), 980, -20, 0).allowed)
            RiskGuard(path, loss_pct=1.0, tz_hours=0).resume()                # el panel (otro proceso)
            v = bot.check(utc(2026, 11, 5, 15, 10), 980, -20, 0)
            self.assertTrue(v.allowed)
            self.assertFalse(bot.state.tripped)

    def test_override_from_a_previous_day_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "risk.json"
            old = RiskGuard(path, loss_pct=1.0, tz_hours=0)
            old.check(utc(2026, 11, 4, 15), 1000, 0, 0)
            old.resume()                                                       # override de AYER en el archivo
            bot = RiskGuard(path, loss_pct=1.0, tz_hours=0)
            bot.check(utc(2026, 11, 5, 15), 1000, 0, 0)                        # hoy: día nuevo, estado limpio
            self.assertFalse(bot.check(utc(2026, 11, 5, 15, 5), 980, -20, 0).allowed)


class ControlAndLogTests(unittest.TestCase):
    def test_pause_global_and_per_channel(self):
        with tempfile.TemporaryDirectory() as d:
            c = Control(Path(d) / "control.json")
            self.assertFalse(c.is_paused(1))
            c.set_paused(True, 2)
            self.assertEqual((c.is_paused(1), c.is_paused(2), c.paused_channels()), (False, True, [2]))
            c.set_paused(True)
            self.assertTrue(c.is_paused(1))
            c.set_paused(False)                                              # reanudar todo limpia también los canales
            self.assertEqual((c.is_paused(2), c.read()["paused_channels"]), (False, []))
            (Path(d) / "control.json").write_text("basura", encoding="utf-8")
            self.assertFalse(c.is_paused(1))                                 # archivo dañado = sin pausa

    def test_event_log_tail_rotation_and_bad_lines(self):
        with tempfile.TemporaryDirectory() as d:
            log = EventLog(Path(d) / "signals.jsonl")
            self.assertEqual(log.tail(), [])
            log.add(msg_id=1, status="operada", text="x" * 900)
            log.add(msg_id=2, status="ignorada")
            with open(log.path, "a", encoding="utf-8") as f:
                f.write("línea rota\n")
            rows = log.tail(10)
            self.assertEqual([r["msg_id"] for r in rows], [1, 2])
            self.assertEqual(len(rows[0]["text"]), 500)
            self.assertIn("ts", rows[0])
            from autotrade import eventlog
            old = (eventlog.MAX_BYTES, eventlog.KEEP_LINES)
            eventlog.MAX_BYTES, eventlog.KEEP_LINES = 200, 3
            try:
                for i in range(20):
                    log.add(msg_id=100 + i, status="ok")
            finally:
                eventlog.MAX_BYTES, eventlog.KEEP_LINES = old
            self.assertLessEqual(len(log.path.read_text(encoding="utf-8").splitlines()), 5)
            self.assertEqual(log.tail(1)[0]["msg_id"], 119)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.control = Control(Path(self.tmp.name) / "c.json")
        self.clock = [1000.0]
        self.calls = []
        chans = [{"num": 1, "title": "IC GOLD", "ref": "-1", "enabled": True},
                 {"num": 2, "title": "", "ref": "@pro", "enabled": False}]
        self.h = remote.RemoteHandler(self.control, chans, status_fn=lambda: "ESTADO OK",
                                      close_fn=lambda: (self.calls.append("close"), 3)[1],
                                      resume_fn=lambda: self.calls.append("resume"), now=lambda: self.clock[0])

    def test_parse_command(self):
        self.assertEqual(remote.parse_command("/Pausa 2"), ("pausa", ["2"]))
        self.assertEqual(remote.parse_command("/estado@mi_bot"), ("estado", []))
        self.assertEqual(remote.parse_command("/pause"), ("pausa", []))
        self.assertEqual(remote.parse_command("/xyz")[0], "?xyz")
        for t in ("hola", "", "/", "✅ Bot iniciado", "[DRY-RUN] 🟢 Bot iniciado"):
            self.assertIsNone(remote.parse_command(t), t)                   # nuestros propios avisos no son comandos

    def test_status_help_and_unknown(self):
        self.assertEqual(self.h.handle("/estado"), "ESTADO OK")
        self.assertIn("/cerrar", self.h.handle("/ayuda"))
        self.assertIn("No entiendo", self.h.handle("/volar"))
        self.assertIsNone(self.h.handle("buenos días"))

    def test_pause_and_resume_global_and_channel(self):
        self.assertIn("en pausa", self.h.handle("/pausa"))
        self.assertTrue(self.control.is_paused(1))
        self.assertIn("reanudado", self.h.handle("/reanudar"))
        self.assertEqual(self.calls, ["resume"])                            # reanudar todo levanta también el límite diario
        self.assertIn("Canal #1", self.h.handle("/pausa 1"))
        self.assertEqual(self.control.paused_channels(), [1])
        self.h.handle("/reanudar 1")
        self.assertEqual(self.control.paused_channels(), [])
        self.assertEqual(self.calls, ["resume"])                            # reanudar un canal no toca el límite
        self.assertIn("No encuentro el canal", self.h.handle("/pausa 9"))
        self.assertIn("No encuentro el canal", self.h.handle("/pausa abc"))

    def test_close_needs_fresh_confirmation(self):
        self.assertIn("confirmar", self.h.handle("/cerrar"))
        self.assertEqual(self.calls, [])                                    # nada se cerró aún
        self.clock[0] += 30
        self.assertIn("Cerradas 3", self.h.handle("/cerrar confirmar"))
        self.assertEqual(self.calls, ["close"])
        self.assertIn("venció", self.h.handle("/cerrar confirmar"))         # ya no hay petición pendiente
        self.h.handle("/cerrar")
        self.clock[0] += 61
        self.assertIn("venció", self.h.handle("/cerrar sí"))
        self.assertEqual(self.calls, ["close"])

    def test_channel_list(self):
        self.h.handle("/pausa 1")
        txt = self.h.handle("/canales")
        self.assertIn("#1 IC GOLD – pausado", txt)
        self.assertIn("#2 @pro – desactivado", txt)


class NewsTests(unittest.TestCase):
    CAL = [{"title": "Non-Farm Payrolls", "country": "USD", "date": "2026-11-06T08:30:00-05:00", "impact": "High"},
           {"title": "CPI", "country": "EUR", "date": "2026-11-06T04:00:00-05:00", "impact": "High"},
           {"title": "Speech", "country": "USD", "date": "2026-11-06T10:00:00-05:00", "impact": "Low"},
           {"title": "FOMC", "country": "USD", "date": "2026-11-07T19:00:00Z", "impact": "high"},
           {"bad": "entry"}, "ni es un dict"]

    def test_parse_calendar_filters_and_tolerates_garbage(self):
        ev = news.parse_calendar(self.CAL)
        self.assertEqual([e.title for e in ev], ["Non-Farm Payrolls", "FOMC"])
        self.assertEqual(ev[0].ts, utc(2026, 11, 6, 13, 30))                 # 08:30 -05:00 = 13:30 UTC
        self.assertEqual([e.currency for e in news.parse_calendar(self.CAL, ("USD", "EUR"))], ["EUR", "USD", "USD"])
        self.assertEqual(news.parse_calendar("no es lista"), [])

    def test_manual_entries(self):
        self.assertEqual(news.validate_manual("Fri 08:30 NFP; Mon-Fri 14:00"), [])
        self.assertEqual(len(news.validate_manual("viernes a las 8; Fri 25:00")), 2)
        now = utc(2026, 11, 6, 14, 20)                                       # viernes 08:20 en Honduras
        ev = news.manual_events("Fri 08:30 NFP", -6, now)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0].ts, utc(2026, 11, 6, 14, 30))                 # 08:30 local = 14:30 UTC
        e = news.find_blackout(ev, now, 15, 15)
        self.assertEqual(e.title, "NFP")
        self.assertIsNone(news.find_blackout(ev, now - 20 * 60, 15, 15))     # 08:00: aún fuera de la ventana
        self.assertIsNone(news.find_blackout(ev, utc(2026, 11, 6, 14, 46), 15, 15))

    def test_filter_manual_mode_message_and_off(self):
        f = news.NewsFilter("manual", 15, 15, -6, "Fri 08:30 NFP")
        msg = f.check(utc(2026, 11, 6, 14, 25))
        self.assertIn("NFP", msg)
        self.assertIn("08:30", msg)
        self.assertIsNone(f.check(utc(2026, 11, 6, 17)))
        self.assertIsNone(news.NewsFilter("off", 15, 15, -6, "Fri 08:30").check(utc(2026, 11, 6, 14, 25)))

    def test_auto_mode_downloads_caches_and_survives_failures(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "news.json"
            calls = []
            def ok(url):
                calls.append(url)
                return json.dumps(self.CAL).encode()
            f = news.NewsFilter("auto", 15, 15, -6, "", cache, fetch=ok)
            now = utc(2026, 11, 6, 13, 0)
            self.assertIn("actualizado", f.refresh(now))
            self.assertIn("Non-Farm", f.check(utc(2026, 11, 6, 13, 25)))
            self.assertEqual(f.refresh(now + 3600), "")                      # copia reciente: no vuelve a descargar
            self.assertEqual(len(calls), 1)
            # nuevo proceso: carga la copia guardada sin red
            def boom(url):
                raise urllib.error.URLError("sin red")
            g = news.NewsFilter("auto", 15, 15, -6, "", cache, fetch=boom)
            self.assertIn("Non-Farm", g.check(utc(2026, 11, 6, 13, 25)))
            self.assertIn("última copia", g.refresh(now + 10 * 3600))        # copia vieja + fallo: usa la guardada
            # sin copia y sin red: no bloquea nada
            h = news.NewsFilter("auto", 15, 15, -6, "", Path(d) / "otra.json", fetch=boom)
            self.assertIn("no está activo", h.refresh(now))
            self.assertIsNone(h.check(utc(2026, 11, 6, 13, 25)))

    def test_manual_text_is_combined_in_auto_mode(self):
        f = news.NewsFilter("auto", 10, 10, 0, "Fri 12:00 Mi noticia", fetch=lambda u: b"[]")
        self.assertIn("Mi noticia", f.check(utc(2026, 11, 6, 12, 5)))


class PerfTests(unittest.TestCase):
    def deals(self):
        t = lambda dh: utc(2026, 11, 6, 12) + dh * 3600
        return [
            {"position_id": 1, "entry": "in", "time": t(-30), "comment": "tg1.100:1"},
            {"position_id": 2, "entry": "in", "time": t(-30), "comment": "tg1.100:2"},
            {"position_id": 3, "entry": "in", "time": t(-2), "comment": "tg2.55:1"},
            {"position_id": 1, "entry": "out", "time": t(-29), "profit": 10, "swap": 0, "commission": -0.5, "comment": "[tp 3900]"},
            {"position_id": 2, "entry": "out", "time": t(-29), "profit": -20, "swap": -0.1, "commission": -0.5, "comment": "[sl 3880]"},
            {"position_id": 3, "entry": "out", "time": t(-1), "profit": 7, "swap": 0, "commission": -0.5, "comment": "[tp]"},
            {"position_id": 9, "entry": "out", "time": t(-1), "profit": 5, "swap": 0, "commission": 0, "comment": "[tp]"},   # sin entrada conocida
            {"position_id": 4, "entry": "in", "time": t(-1), "comment": "tg2.56:1"},                                          # aún abierta
        ]

    def test_aggregate_by_channel_and_period(self):
        rows = perf.aggregate(self.deals(), utc(2026, 11, 6, 12), tz_hours=0)
        by = {r["num"]: r for r in rows}
        self.assertEqual(set(by), {1, 2, None})
        c1 = by[1]
        self.assertEqual((c1["signals"], c1["closed"], c1["wins"]), (1, 2, 1))
        self.assertAlmostEqual(c1["pnl_total"], 9.5 - 20.6)
        self.assertAlmostEqual(c1["pnl_today"], 0.0)                         # esas salidas fueron hace ~29 h
        self.assertAlmostEqual(c1["pnl_7d"], c1["pnl_total"])
        self.assertAlmostEqual(c1["win_rate"], 50.0)
        c2 = by[2]
        self.assertEqual((c2["signals"], c2["closed"]), (2, 1))              # 2 señales, 1 posición ya cerrada
        self.assertAlmostEqual(c2["pnl_total"], 6.5)
        self.assertAlmostEqual(c2["pnl_today"], 6.5)
        self.assertEqual(by[None]["closed"], 1)                              # operaciones sin etiqueta: aparte
        self.assertEqual([r["num"] for r in rows], [1, 2, None])             # None siempre al final

    def test_server_offset_and_day_boundary(self):
        now = utc(2026, 11, 6, 12)
        d = [{"position_id": 1, "entry": "in", "time": now, "comment": "tg1.1:1"},
             {"position_id": 1, "entry": "out", "time": now - 600 + 3 * 3600, "profit": 4, "swap": 0, "commission": 0, "comment": ""}]
        r = perf.aggregate(d, now, tz_hours=0, server_offset=3 * 3600)[0]    # servidor UTC+3
        self.assertAlmostEqual(r["pnl_today"], 4.0)
        # hace 25 h no cuenta como «hoy» (empezó a las 00:00 locales)
        d[1]["time"] = now - 25 * 3600
        self.assertAlmostEqual(perf.aggregate(d, now, 0)[0]["pnl_today"], 0.0)

    def test_empty(self):
        self.assertEqual(perf.aggregate([], utc(2026, 11, 6)), [])


class BackupTests(unittest.TestCase):
    def test_export_import_roundtrip_and_session_is_opt_in(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            A = Path(a)
            (A / "config.ini").write_text("[telegram]\napi_id=1\n", encoding="utf-8")
            (A / "autotrade.session").write_bytes(b"secreto")
            (A / "autotrade.log").write_text("log", encoding="utf-8")
            (A / "history.json").write_text("[]", encoding="utf-8")
            z = A / "copia.zip"
            self.assertEqual(backup.export_zip(A, z), ["config.ini"])
            self.assertEqual(sorted(backup.export_zip(A, z, include_session=True)), ["autotrade.session", "config.ini"])
            B = Path(b)
            (B / "config.ini").write_text("viejo", encoding="utf-8")
            self.assertEqual(sorted(backup.import_zip(z, B)), ["autotrade.session", "config.ini"])
            self.assertEqual((B / "config.ini").read_text(encoding="utf-8"), "[telegram]\napi_id=1\n")
            baks = list(B.glob("config.ini.bak-*"))
            self.assertEqual(len(baks), 1)
            self.assertEqual(baks[0].read_text(encoding="utf-8"), "viejo")

    def test_no_config_and_hostile_zips(self):
        with tempfile.TemporaryDirectory() as d:
            D = Path(d)
            with self.assertRaises(FileNotFoundError):
                backup.export_zip(D, D / "x.zip")
            for name in ("../evil.txt", "sub/config.ini", "C:\\evil.exe", ".hidden.session", "run.bat"):
                zp = D / "h.zip"
                with zipfile.ZipFile(zp, "w") as z:
                    z.writestr("config.ini", "x")
                    z.writestr(name, "malo")
                with self.assertRaises(ValueError, msg=name):
                    backup.import_zip(zp, D)
            with zipfile.ZipFile(D / "n.zip", "w") as z:
                z.writestr("otra.txt", "x")
            with self.assertRaises(ValueError):
                backup.import_zip(D / "n.zip", D)
            self.assertFalse((D.parent / "evil.txt").exists())


class FetchDealsTests(unittest.TestCase):
    """mt5_helper.fetch_deals con un MT5 falso."""

    def fake(self, deals, init=True, tick_time=1_700_000_000 + 2 * 3600 + 30):
        from tests.fake_mt5 import FakeMT5, NS
        f = FakeMT5()
        f.tick = NS(bid=2650.0, ask=2650.3, time=tick_time)
        f.deals = deals
        f.initialize = lambda **kw: init
        f.symbol_select = lambda s, v: True
        f.symbol_info_tick = lambda s: f.tick
        f.history_deals_get = lambda a, b: (f.range.append((a, b)) or f.deals)
        f.range = []
        f.DEAL_ENTRY_IN, f.DEAL_ENTRY_OUT, f.DEAL_ENTRY_INOUT = 0, 1, 2
        return f

    def run_fetch(self, fake, **kw):
        from unittest import mock
        from autotrade import mt5_helper
        with mock.patch("autotrade.mt5_import.import_mt5", return_value=fake), \
             mock.patch("time.time", return_value=1_700_000_000.0):
            return mt5_helper.fetch_deals(symbol="XAUUSD", magic=777, **kw)

    def test_filters_by_magic_maps_entries_and_estimates_offset(self):
        from tests.fake_mt5 import NS
        mk = lambda **k: NS(**{"position_id": 1, "entry": 0, "time": 1, "profit": 0.0, "swap": 0.0, "commission": 0.0,
                               "fee": 0.0, "comment": "", "magic": 777, **k})
        f = self.fake([mk(entry=0, comment="tg1.5:1"), mk(entry=1, profit=9.5), mk(entry=2, profit=1.0),
                       mk(magic=1, entry=1, profit=999.0), mk(entry=3, profit=2.0)])
        r = self.run_fetch(f, days=30)
        self.assertTrue(r["ok"])
        self.assertEqual([d["entry"] for d in r["deals"]], ["in", "out", "inout", "out"])   # el de otro magic no está; el cierre «por contraria» (3) cuenta como salida
        self.assertEqual(r["server_offset"], 2 * 3600)                                         # servidor UTC+2
        a, b = f.range[0]
        self.assertAlmostEqual((b - a) / 86400, 30.0 + 1 / 24, places=6)                       # 30 días + 1 h de margen

    def test_errors_are_reported(self):
        r = self.run_fetch(self.fake([], init=False))
        self.assertFalse(r["ok"])
        self.assertIn("No se pudo conectar", r["error"])
        f = self.fake([])
        f.history_deals_get = lambda a, b: None
        self.assertIn("no devolvió historial", self.run_fetch(f)["error"])


class UpdateTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(updates.parse_version("v1.2.10"), (1, 2, 10))
        self.assertTrue(updates.is_newer("v1.2.0", "1.1.9"))
        self.assertTrue(updates.is_newer("1.10", "1.9.5"))
        self.assertFalse(updates.is_newer("1.1.0", "1.1"))
        self.assertFalse(updates.is_newer("", "1.0"))

    def test_check_outcomes(self):
        ok = lambda tag: (lambda u: json.dumps({"tag_name": tag, "html_url": "https://x/r"}).encode())
        self.assertEqual(updates.check("1.1.0", fetch=ok("v1.2.0"))["status"], "newer")
        self.assertEqual(updates.check("1.1.0", fetch=ok("v1.1.0"))["status"], "current")
        def http(code):
            def f(u):
                raise urllib.error.HTTPError(u, code, "x", {}, io.BytesIO())
            return f
        r = updates.check("1.1.0", fetch=http(404))
        self.assertEqual(r["status"], "none")
        self.assertIn("Todavía no hay", r["message"])
        self.assertEqual(updates.check("1.1.0", fetch=http(500))["status"], "error")
        def down(u):
            raise OSError("sin red")
        self.assertEqual(updates.check("1.1.0", fetch=down)["status"], "error")


if __name__ == "__main__":
    unittest.main()
