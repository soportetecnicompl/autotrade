import unittest
from datetime import datetime, timezone

from autotrade.backtest import Msg, Prices, SimConfig, simulate
from autotrade.schedule import TradingSchedule, parse_days, parse_hours
from collections import Counter


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


class ScheduleTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_days("Mon-Fri"), {0, 1, 2, 3, 4})
        self.assertEqual(parse_days("Lun, Mié, Vie"), {0, 2, 4})
        self.assertEqual(parse_days("Fri-Mon"), {4, 5, 6, 0})
        self.assertEqual(parse_days(""), set(range(7)))
        self.assertEqual(parse_hours("08:00-12:00, 14:30-18:00"), [(480, 720), (870, 1080)])
        self.assertEqual(parse_hours("00:00-24:00"), [(0, 1440)])
        with self.assertRaises(ValueError):
            parse_hours("8-12")
        with self.assertRaises(ValueError):
            parse_hours("25:00-26:00")
        with self.assertRaises(ValueError):
            parse_days("Funday")

    def test_allows_with_timezone(self):
        # Honduras UTC-6: 08:00-12:00 local = 14:00-18:00 UTC. 2026-10-21 es miércoles.
        s = TradingSchedule.from_text("Mon-Fri", "08:00-12:00", -6)
        self.assertTrue(s.allows(utc(2026, 10, 21, 14, 0)))
        self.assertTrue(s.allows(utc(2026, 10, 21, 17, 59)))
        self.assertFalse(s.allows(utc(2026, 10, 21, 18, 0)))     # fin exclusivo
        self.assertFalse(s.allows(utc(2026, 10, 21, 13, 59)))
        self.assertFalse(s.allows(utc(2026, 10, 24, 15, 0)))     # sábado

    def test_local_day_uses_timezone(self):
        # Domingo 02:00 UTC = sábado 20:00 en Honduras -> sábado, no permitido
        s = TradingSchedule.from_text("Mon-Fri", "", -6)
        self.assertFalse(s.allows(utc(2026, 10, 25, 2)))
        # Sábado 02:00 UTC = viernes 20:00 en Honduras -> permitido
        self.assertTrue(s.allows(utc(2026, 10, 24, 2)))

    def test_wraps_midnight_and_always(self):
        s = TradingSchedule.from_text("", "22:00-02:00", 0)
        self.assertTrue(s.allows(utc(2026, 10, 21, 23)))
        self.assertTrue(s.allows(utc(2026, 10, 21, 1)))
        self.assertFalse(s.allows(utc(2026, 10, 21, 12)))
        self.assertTrue(TradingSchedule.from_text().always)
        self.assertTrue(TradingSchedule.from_text().allows(utc(2026, 10, 24, 3)))

    def test_backtest_skips_outside_hours(self):
        t0 = int(utc(2026, 10, 21, 20, 0))                       # 14:00 Honduras
        bars = Prices([(t0 - 600 + 60 * i, 2650, 2650, 2650, 2650) for i in range(40)])
        sig = Msg(1, t0, "XAUUSD BUY NOW SL 2640 TP 2670")
        for hours, expected in [("08:00-12:00", 0), ("13:00-17:00", 2 - 1)]:
            cfg = SimConfig(lot=0.01, spread=0, delay=0, max_tps=1,
                            schedule=TradingSchedule.from_text("", hours, -6))
            sk = Counter()
            trades = simulate([sig], bars, cfg, False, sk)
            if expected == 0:
                self.assertEqual((trades, sk["fuera de horario"]), ([], 1))
            else:
                self.assertEqual(len(trades), 1)


if __name__ == "__main__":
    unittest.main()
