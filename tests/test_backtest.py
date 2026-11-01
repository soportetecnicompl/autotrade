import unittest
from collections import Counter

from autotrade.backtest import Msg, Prices, SimConfig, is_us_dst, make_offset_fn, report, simulate

T0 = 1_700_000_000 - (1_700_000_000 % 60)   # minuto exacto


def bars(path, start=T0 - 600, spread_free=True):
    """path: lista de (open, high, low, close) por minuto desde `start`."""
    return Prices([(start + 60 * i, *b) for i, b in enumerate(path)])


def flat(price, n):
    return [(price, price, price, price)] * n


CFG = SimConfig(lot=0.02, spread=0.0, delay=0, pending_expiry_min=30, max_hold_hours=2)


def run(msgs, path, follow=False, cfg=CFG):
    sk = Counter()
    return simulate(msgs, bars(path), cfg, follow, sk), sk


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.pre = flat(2650, 10)             # 10 min antes de la señal

    def test_buy_hits_both_tps(self):
        sig = Msg(1, T0, "XAUUSD BUY 2650 SL 2640 TP1 2655 TP2 2660")
        path = self.pre + flat(2650, 2) + [(2650, 2656, 2650, 2656)] + [(2656, 2661, 2656, 2661)]
        t, _ = run([sig], path)
        self.assertEqual([(x.tp_n, x.outcome) for x in t], [(1, "TP"), (2, "TP")])
        self.assertAlmostEqual(sum(x.pnl for x in t), 0.01 * 5 * 100 + 0.01 * 10 * 100)

    def test_sl_wins_when_same_bar(self):
        sig = Msg(1, T0, "XAUUSD BUY 2650 SL 2640 TP1 2655 TP2 2660")
        path = self.pre + [(2650, 2661, 2639, 2650)]
        t, _ = run([sig], path)
        self.assertTrue(all(x.outcome == "SL" for x in t))
        self.assertAlmostEqual(sum(x.pnl for x in t), -0.02 * 10 * 100)

    def test_sell_sl(self):
        sig = Msg(1, T0, "XAUUSD SELL 2650 SL 2660 TP1 2640 TP2 2630")
        path = self.pre + flat(2650, 2) + [(2650, 2661, 2650, 2660)]
        t, _ = run([sig], path)
        self.assertTrue(all(x.outcome == "SL" for x in t))

    def test_spread_costs_money(self):
        sig = Msg(1, T0, "XAUUSD BUY 2650 SL 2640 TP1 2655")
        path = self.pre + [(2650, 2656, 2650, 2656)]
        cfg = SimConfig(lot=0.01, spread=0.5, delay=0, max_tps=1)
        t, _ = run([sig], path, cfg=cfg)
        self.assertEqual(t[0].outcome, "TP")
        self.assertAlmostEqual(t[0].pnl, (2655 - 2650.5) * 0.01 * 100)

    def test_pending_limit_fill_then_tp(self):
        sig = Msg(1, T0, "XAUUSD BUY LIMIT 2645 SL 2635 TP 2655")
        path = self.pre + flat(2650, 3) + [(2650, 2650, 2645, 2646)] + [(2646, 2656, 2646, 2655)]
        t, _ = run([sig], path, cfg=SimConfig(lot=0.01, spread=0, delay=0))
        self.assertEqual(t[0].outcome, "TP")
        self.assertAlmostEqual(t[0].pnl, 10 * 0.01 * 100)

    def test_pending_expires(self):
        sig = Msg(1, T0, "XAUUSD BUY LIMIT 2645 SL 2635 TP 2655")
        t, _ = run([sig], self.pre + flat(2650, 60))
        self.assertEqual(t[0].outcome, "EXPIRED")
        self.assertEqual(t[0].pnl, 0.0)

    def test_skipped_without_price_data(self):
        sig = Msg(1, T0 + 10 * 86400, "XAUUSD BUY 2650 SL 2640 TP 2655")
        t, sk = run([sig], self.pre + flat(2650, 5))
        self.assertEqual(t, [])
        self.assertEqual(sk["sin datos de precio"], 1)

    def test_follow_breakeven_then_stopped_at_entry(self):
        sig = Msg(1, T0, "XAUUSD BUY 2650 SL 2640 TP1 2670")
        be = Msg(2, T0 + 300, "110 pips, now can set BE")
        path = self.pre + flat(2650, 4) + flat(2656, 2) + [(2656, 2656, 2649, 2650)]
        cfg = SimConfig(lot=0.01, spread=0, delay=0, max_tps=1)
        t_static, _ = run([sig, be], path, follow=False, cfg=cfg)
        t_follow, _ = run([sig, be], path, follow=True, cfg=cfg)
        self.assertEqual(t_static[0].outcome, "OPEN")  # datos terminan antes del timeout
        self.assertEqual((t_follow[0].outcome, round(t_follow[0].pnl, 2)), ("BE", 0.0))

    def test_follow_close_all_and_partial(self):
        sig = Msg(1, T0, "XAUUSD BUY 2650 SL 2640 TP1 2670")
        half = Msg(2, T0 + 240, "Close half")
        close = Msg(3, T0 + 420, "close all")
        path = self.pre + flat(2650, 4) + flat(2654, 3) + flat(2658, 5)
        cfg = SimConfig(lot=0.02, spread=0, delay=0, max_tps=1)
        t, _ = run([sig, half, close], path, follow=True, cfg=cfg)
        self.assertEqual(t[0].outcome, "CLOSED")
        self.assertAlmostEqual(t[0].pnl, 0.01 * 4 * 100 + 0.01 * 8 * 100)

    def test_reply_to_other_signal_is_ignored(self):
        sig = Msg(1, T0, "XAUUSD BUY 2650 SL 2640 TP1 2670")
        other = Msg(2, T0 + 120, "close all", reply_to=99)
        t, _ = run([sig, other], self.pre + flat(2650, 20), follow=True,
                   cfg=SimConfig(lot=0.01, spread=0, delay=0, max_tps=1))
        self.assertNotEqual(t[0].outcome, "CLOSED")

    def test_timeout_closes_at_max_hold(self):
        sig = Msg(1, T0, "XAUUSD BUY NOW SL 2640 TP1 2670")
        t, _ = run([sig], self.pre[:9] + [(2650, 2650, 2650, 2650)] + flat(2650, 1) + flat(2652, 200),
                   cfg=SimConfig(lot=0.01, spread=0, delay=0, max_tps=1, max_hold_hours=1))
        self.assertEqual(t[0].outcome, "TIMEOUT")
        self.assertAlmostEqual(t[0].pnl, 2 * 0.01 * 100)

    def test_dst_and_report(self):
        self.assertTrue(is_us_dst(1_720_000_000))      # julio 2024
        self.assertFalse(is_us_dst(1_705_000_000))     # enero 2024
        off = make_offset_fn(2, True)
        self.assertEqual((off(1_720_000_000), off(1_705_000_000)), (3 * 3600, 2 * 3600))
        sig = Msg(1, T0, "XAUUSD BUY 2650 SL 2640 TP1 2655 TP2 2660")
        path = self.pre + [(2650, 2661, 2650, 2661)]
        t, sk = run([sig], path)
        txt = report({"static": t, "follow": t}, sk, CFG)
        self.assertIn("% acierto", txt)
        self.assertIn("100.0%", txt)


if __name__ == "__main__":
    unittest.main()
