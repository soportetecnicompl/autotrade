import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace as NS
from unittest import mock

import backtest
from autotrade import mt5_history as h

DAY = 86400
T1 = 1_700_000_000              # «ahora» (hora del servidor, para los tests)


class FakeMT5:
    """MT5 que tiene velas (una por hora) solo dentro de `available` y que tarda en entregar cada tramo."""
    TIMEFRAME_M1 = 1

    def __init__(self, available, pending=0, maxbars=0):
        self.available, self.pending, self.maxbars = available, pending, maxbars
        self.calls, self._seen, self.sleeps = [], {}, []

    def copy_rates_range(self, symbol, tf, a, b):
        self.calls.append((a, b))
        n = self._seen[a] = self._seen.get(a, 0) + 1
        lo, hi = max(a, self.available[0]), min(b, self.available[1])
        if hi <= lo:
            return None                                   # MT5 devuelve None cuando no hay nada
        if n <= self.pending:
            return None                                   # aún descargando: la primera(s) vez(veces) viene vacío
        t0 = lo - lo % 3600 + 3600
        return [{"time": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5} for t in range(t0, hi, 3600)]

    def terminal_info(self):
        return NS(maxbars=self.maxbars)

    def last_error(self):
        return (1, "Success")


class FetchTests(unittest.TestCase):
    def fetch(self, fake, s0, s1, **kw):
        logs = []
        bars = h.fetch_m1(fake, "XAUUSD", s0, s1, sleep=fake.sleeps.append, wait=3.0, log=logs.append, **kw)
        return bars, logs

    def test_retries_until_mt5_finishes_downloading(self):
        f = FakeMT5((T1 - 40 * DAY, T1), pending=2)                  # los 2 primeros intentos de cada tramo vienen vacíos
        bars, logs = self.fetch(f, T1 - 25 * DAY, T1)
        self.assertGreater(len(bars), 24 * 24)
        self.assertEqual(len({b[0] for b in bars}), len(bars))        # sin duplicados entre tramos
        self.assertEqual([b[0] for b in bars], sorted(b[0] for b in bars))
        self.assertEqual(f.sleeps, [3.0, 3.0] * 3)                    # 3 tramos × 2 reintentos con espera
        self.assertTrue(any("esperando" in l for l in logs))
        self.assertTrue(any("tramo 3/3" in l for l in logs))

    def test_partial_history_is_accepted_and_search_stops_early(self):
        f = FakeMT5((T1 - 12 * DAY, T1))                              # el servidor solo tiene 12 días de 100 pedidos
        bars, logs = self.fetch(f, T1 - 100 * DAY, T1)
        self.assertTrue(bars)
        self.assertGreaterEqual(bars[0][0], T1 - 12 * DAY)            # nada de antes de lo disponible
        self.assertLessEqual(len(f.calls), 2 + 2 * 4)                 # 2 tramos con datos + 2 vacíos × 4 intentos: no recorre los 10 tramos
        self.assertTrue(any("no tiene más historial" in l for l in logs))

    def test_nothing_available(self):
        f = FakeMT5((T1 + 10 * DAY, T1 + 20 * DAY))                   # nada en el rango pedido
        bars, _ = self.fetch(f, T1 - 30 * DAY, T1)
        self.assertEqual(bars, [])

    def test_advice_is_actionable(self):
        txt = h.advice("XAUUSD", (1, "Success"), 90)
        for needle in ("XAUUSD", "(1, 'Success')", "Inicio", "Ilimitado", "30", "nombre exacto"):
            self.assertIn(needle, txt)

    def test_maxbars_warning(self):
        self.assertIn("50,000", h.maxbars_warning(FakeMT5((0, 1), maxbars=50_000), 90))
        self.assertIn("90 días", h.maxbars_warning(FakeMT5((0, 1), maxbars=50_000), 90))
        self.assertEqual(h.maxbars_warning(FakeMT5((0, 1), maxbars=500_000), 90), "")
        self.assertEqual(h.maxbars_warning(FakeMT5((0, 1), maxbars=0), 90), "")          # no se sabe: sin aviso
        broken = FakeMT5((0, 1))
        broken.terminal_info = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        self.assertEqual(h.maxbars_warning(broken, 90), "")


class LoadMt5Tests(unittest.TestCase):
    def load(self, fake, t0, t1):
        fake.initialize = lambda: True
        fake.symbol_select = lambda s, v: True
        fake.shutdown = lambda: setattr(fake, "closed", True)
        out = io.StringIO()
        with mock.patch("autotrade.mt5_import.import_mt5", return_value=fake), \
             mock.patch.object(h, "WAIT_SECONDS", 0.0), mock.patch("time.sleep"), redirect_stdout(out):
            try:
                return backtest.load_mt5("XAUUSD", t0, t1, 0, False), out.getvalue()
            except SystemExit as e:
                return e, out.getvalue()

    def test_full_history_loads_and_closes_the_connection(self):
        f = FakeMT5((T1 - 60 * DAY, T1 + 5 * DAY))
        prices, out = self.load(f, T1 - 30 * DAY, T1)
        self.assertGreater(len(prices.bars), 600)
        self.assertTrue(f.closed)
        self.assertIn("velas M1 de", out)
        self.assertNotIn("solo tiene velas", out)

    def test_partial_history_warns_but_still_returns_prices(self):
        f = FakeMT5((T1 - 10 * DAY, T1 + 5 * DAY))
        prices, out = self.load(f, T1 - 60 * DAY, T1)
        self.assertGreater(len(prices.bars), 100)
        self.assertIn("MT5 solo tiene velas M1 desde", out)
        self.assertIn("se omiten del análisis", out)

    def test_no_history_gives_actionable_error_and_closes(self):
        f = FakeMT5((T1 + 10 * DAY, T1 + 20 * DAY))
        err, out = self.load(f, T1 - 30 * DAY, T1)
        self.assertIsInstance(err, SystemExit)
        self.assertIn("tecla «Inicio»", str(err))
        self.assertIn("Ilimitado", str(err))
        self.assertTrue(f.closed)

    def test_maxbars_warning_is_printed_before_downloading(self):
        f = FakeMT5((T1 - 60 * DAY, T1 + 5 * DAY), maxbars=20_000)
        _, out = self.load(f, T1 - 60 * DAY, T1)
        self.assertLess(out.index("limita los gráficos"), out.index("Descargando"))


if __name__ == "__main__":
    unittest.main()
