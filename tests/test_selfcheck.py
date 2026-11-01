import sys
import types
import unittest
from unittest import mock

from autotrade import mt5_helper, selfcheck


def fake_mod(name, version="1.2.3"):
    m = types.ModuleType(name)
    m.__version__ = version
    return m


class SelfCheckTests(unittest.TestCase):
    def test_all_present(self):
        mods = {n: fake_mod(n) for n in ("telethon", "tkinter", "numpy", "MetaTrader5")}
        with mock.patch.dict(sys.modules, mods):
            res = selfcheck.check_imports(windows=True)
        self.assertEqual([r[0] for r in res], ["telethon", "tkinter", "numpy", "MetaTrader5"])
        self.assertTrue(all(ok for _, ok, _, _ in res))
        self.assertEqual(selfcheck.failed_required(res), [])
        self.assertIn("1.2.3", selfcheck.format_report(res))

    def test_missing_numpy_is_reported_with_the_real_error(self):
        # el caso real del .exe: MetaTrader5 está pero numpy no se empaquetó
        mods = {n: fake_mod(n) for n in ("telethon", "tkinter", "MetaTrader5")}
        mods["numpy"] = None                                   # import numpy -> ImportError
        with mock.patch.dict(sys.modules, mods):
            res = selfcheck.check_imports(windows=True)
        bad = selfcheck.failed_required(res)
        self.assertIn("numpy", [b[0] for b in bad])
        self.assertTrue(any("Error" in b[2] and "numpy" in b[2] for b in bad))   # ModuleNotFoundError: ...numpy...
        self.assertIn("FALLO", selfcheck.format_report(res))

    def test_mt5_and_numpy_not_required_off_windows(self):
        mods = {n: fake_mod(n) for n in ("telethon", "tkinter")}
        mods.update(numpy=None, MetaTrader5=None)
        with mock.patch.dict(sys.modules, mods):
            res = selfcheck.check_imports(windows=False)
        self.assertEqual(selfcheck.failed_required(res), [])
        self.assertIn("n/a", selfcheck.format_report(res))


class ProbeImportErrorTests(unittest.TestCase):
    def test_windows_shows_the_real_cause_not_a_misleading_message(self):
        err = ImportError("DLL load failed while importing _core: no se puede encontrar el modulo")
        with mock.patch.object(mt5_helper, "import_mt5", side_effect=err, create=True), \
             mock.patch("autotrade.mt5_import.import_mt5", side_effect=err), \
             mock.patch.object(sys, "platform", "win32"):
            res = mt5_helper.probe()
        self.assertFalse(res["ok"])
        self.assertIn("DLL load failed", res["error"])
        self.assertIn("ImportError", res["error"])
        self.assertNotIn("solo funciona en Windows", res["error"])   # el usuario SÍ está en Windows
        self.assertIn("no de tu cuenta", res["error"])

    def test_non_windows_keeps_the_platform_message(self):
        with mock.patch("autotrade.mt5_import.import_mt5", side_effect=ImportError("x")), \
             mock.patch.object(sys, "platform", "linux"):
            res = mt5_helper.probe()
        self.assertIn("solo funciona en Windows", res["error"])


if __name__ == "__main__":
    unittest.main()
