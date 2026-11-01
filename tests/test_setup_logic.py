import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import main as bot_main
from autotrade import paths, setup_logic as sl
from autotrade.channels import ChannelCfg
from autotrade.supervisor import supervise, read_pid


def good() -> sl.Settings:
    return sl.Settings(api_id="12345678", api_hash="a" * 32, phone="+50499998888",
                       channels=[ChannelCfg(1, "-1001234567890", "Mi canal VIP")])


class ConfigTests(unittest.TestCase):
    def roundtrip(self, s):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text(sl.render_config(s), encoding="utf-8")
            return sl.load_settings(p), bot_main.load_config(str(p))

    def test_roundtrip_defaults(self):
        s = good()
        back, cfg = self.roundtrip(s)
        self.assertEqual(back, s)
        self.assertTrue(cfg["dry_run"] and cfg["schedule"].always)       # sin filtro => sin restricción
        self.assertEqual((cfg["max_tps"], cfg["lot_size"], cfg["api_id"]), (2, 0.02, 12345678))

    def test_roundtrip_custom_values_and_bot_reads_them(self):
        s = good()
        s.dry_run, s.lot_size, s.max_tps, s.stops_policy = False, 0.05, 3, "adjust"
        s.filter_enabled, s.trading_days, s.trading_hours, s.tz_offset_hours = True, "Mon-Thu", "08:00-12:00, 14:00-18:00", -5.0
        s.mt5_login, s.mt5_password, s.mt5_server = "123456", "p@ss%word;x", "Broker-Live"
        s.heartbeat_url, s.notify_skips, s.symbol = "https://hc-ping.com/abc", False, "XAUUSDm"
        back, cfg = self.roundtrip(s)
        self.assertEqual(back, s)                                        # incluye contraseña con % y ;
        self.assertFalse(cfg["dry_run"])
        self.assertEqual(cfg["password"], "p@ss%word;x")
        self.assertFalse(cfg["schedule"].always)
        self.assertEqual(cfg["heartbeat_url"], "https://hc-ping.com/abc")
        self.assertEqual((cfg["stops_policy"], cfg["symbol"], cfg["notify_skips"]), ("adjust", "XAUUSDm", False))

    def test_filter_disabled_keeps_form_defaults(self):
        s = good()
        s.filter_enabled, s.trading_days = False, "Mon-Fri"
        back, _ = self.roundtrip(s)
        self.assertFalse(back.filter_enabled)
        self.assertEqual((back.trading_days, back.trading_hours), ("Mon-Fri", "08:00-17:00"))

    def test_multiple_channels_roundtrip_and_reach_the_bot(self):
        s = good()
        s.channels = [ChannelCfg(1, "-1001", "IC GOLD VIP"),
                      ChannelCfg(3, "@goldpro", "Gold Pro", enabled=False, lot_size=0.05, max_tps=3),
                      ChannelCfg(7, "-1009", "Otro", lot_size=0.01, max_tps=1)]
        back, cfg = self.roundtrip(s)
        self.assertEqual(back.channels, s.channels)                       # incluye números estables 1, 3, 7
        self.assertEqual([(c["num"], c["ref"], c["enabled"], c["lot_size"], c["max_tps"])
                          for c in cfg["channels"]],
                         [(1, "-1001", True, 0.0, 0), (3, "@goldpro", False, 0.05, 3), (7, "-1009", True, 0.01, 1)])

    def test_old_single_channel_config_still_loads(self):
        old = ("[telegram]\napi_id = 1\napi_hash = a\nchannel = @viejo\nchannel_title = Canal viejo\n"
               "[mt5]\n[trading]\n")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text(old, encoding="utf-8")
            self.assertEqual(sl.load_settings(p).channels, [ChannelCfg(1, "@viejo", "Canal viejo")])
            self.assertEqual(bot_main.load_config(str(p))["channels"][0]["ref"], "@viejo")

    def test_channel_validation(self):
        s = good()
        s.channels = [ChannelCfg(1, "-1", "A", enabled=False)]
        self.assertTrue(sl.validate_channel(s))                           # ninguno activo
        s.channels = [ChannelCfg(1, "-1", "A", lot_size=500.0, max_tps=5)]
        self.assertEqual(len(sl.validate_channel(s)), 2)
        s.channels = [ChannelCfg(1, "-1", "A", lot_size=0.03, max_tps=2), ChannelCfg(2, "-2", "B")]
        self.assertEqual(sl.validate_channel(s), [])

    def test_missing_file_gives_defaults(self):
        self.assertEqual(sl.load_settings("/nonexistent/config.ini"), sl.Settings())


class ValidationTests(unittest.TestCase):
    def test_telegram(self):
        self.assertEqual(sl.validate_telegram(good()), [])
        bad = sl.Settings(api_id="abc", api_hash="123", phone="99998888")
        self.assertEqual(len(sl.validate_telegram(bad)), 3)

    def test_channel_mt5_trading_alerts(self):
        self.assertTrue(sl.validate_channel(sl.Settings()))               # sin canales
        self.assertEqual(sl.validate_channel(good()), [])
        s = good(); s.mt5_login = "12ab"
        self.assertTrue(any("dígitos" in e for e in sl.validate_mt5(s)))
        s.mt5_login = "123"
        self.assertTrue(any("contraseña" in e for e in sl.validate_mt5(s)))
        s = good(); s.lot_size, s.max_tps = 0.0, 5
        self.assertEqual(len(sl.validate_trading(s)), 2)
        s = good(); s.filter_enabled, s.trading_hours = True, "8 a 12"
        self.assertTrue(any("Filtro de horario" in e for e in sl.validate_trading(s)))
        s = good(); s.heartbeat_url = "hc-ping.com/x"
        self.assertTrue(sl.validate_alerts(s))
        self.assertEqual(sl.validate_all(good()), [])


class DetectionTests(unittest.TestCase):
    def test_detect_mt5_paths(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("MetaTrader 5", "IC Markets MetaTrader 5", "Otro"):
                p = Path(d) / name
                p.mkdir()
                (p / "terminal64.exe").write_text("x")
            found = sl.detect_mt5_paths({"ProgramFiles": d})
            self.assertEqual(len(found), 3)
            self.assertEqual(len(set(found)), 3)                        # sin duplicados
        self.assertEqual(sl.detect_mt5_paths({}), [])

    def test_estimate_server_offset(self):
        now = 1_700_000_000.0
        self.assertEqual(sl.estimate_server_offset(now + 3 * 3600 + 20, now), 3)
        self.assertEqual(sl.estimate_server_offset(now + 2 * 3600 - 90, now), 2)
        self.assertEqual(sl.estimate_server_offset(now - 5 * 3600, now), -5)
        self.assertIsNone(sl.estimate_server_offset(now + 3 * 3600 - 86400 + 1800, now))   # tick viejo
        self.assertIsNone(sl.estimate_server_offset(now + 40 * 3600, now))

    def test_base_offset(self):
        summer, winter = 1_720_000_000.0, 1_705_000_000.0              # jul y ene de 2024
        self.assertEqual(sl.base_offset(3, summer, True), 2)
        self.assertEqual(sl.base_offset(2, winter, True), 2)
        self.assertEqual(sl.base_offset(3, summer, False), 3)


class StartupTests(unittest.TestCase):
    def test_install_status_remove(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"APPDATA": d}
            self.assertFalse(sl.startup_installed(env))
            p = sl.install_startup([r"C:\Program Files\AutoTrade\AutoTrade.exe", "--bot"], env)
            self.assertTrue(sl.startup_installed(env))
            self.assertTrue(str(p).endswith(os.path.join("Startup", "AutoTrade.cmd")))
            txt = p.read_bytes().decode()
            self.assertIn('start "" /min "C:\\Program Files\\AutoTrade\\AutoTrade.exe" --bot', txt)
            self.assertIn("ping -n 31", txt)
            self.assertIn("\r\n", txt)                                  # CRLF para cmd
            self.assertTrue(sl.remove_startup(env))
            self.assertFalse(sl.startup_installed(env))
            self.assertFalse(sl.remove_startup(env))

    def test_no_appdata(self):
        with self.assertRaises(RuntimeError):
            sl.install_startup(["x"], {})


class SupervisorAndPathsTests(unittest.TestCase):
    def test_supervise_restarts_until_clean_exit(self):
        calls, sleeps = [], []
        codes = iter([2, 1, 2, 0])
        def run_once(n):
            calls.append(n)
            return next(codes)
        self.assertEqual(supervise(run_once, delay=15, sleep=sleeps.append), 0)
        self.assertEqual(calls, [0, 1, 2, 3])                           # contador de reinicios
        self.assertEqual(sleeps, [15, 15, 15])

    def test_supervise_clean_exit_first_time_and_max(self):
        self.assertEqual(supervise(lambda n: 0, sleep=lambda s: None), 0)
        self.assertEqual(supervise(lambda n: 2, sleep=lambda s: None, max_restarts=2), 2)

    def test_data_dir_env_and_launcher(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["AUTOTRADE_HOME"] = os.path.join(d, "x")
            try:
                self.assertTrue(paths.data_dir().is_dir())
            finally:
                del os.environ["AUTOTRADE_HOME"]
        cmd = paths.launcher_cmd(["--bot"])
        self.assertEqual((cmd[0], cmd[-1]), (sys.executable, "--bot"))
        self.assertTrue(cmd[1].endswith("app.py"))

    def test_pid_alive_and_stop_tree(self):
        self.assertTrue(paths.pid_alive(os.getpid()))
        self.assertFalse(paths.pid_alive(0))
        if os.name == "posix":
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 **paths.popen_kwargs(detached=True))
            self.assertTrue(paths.pid_alive(p.pid))
            paths.stop_tree(p.pid)
            p.wait(timeout=10)
            self.assertFalse(paths.pid_alive(p.pid))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(read_pid(d), 0)
            (Path(d) / "bot.pid").write_text("4242")
            self.assertEqual(read_pid(d), 4242)


if __name__ == "__main__":
    unittest.main()
