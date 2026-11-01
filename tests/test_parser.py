import unittest

from autotrade.parser import parse
from autotrade.planner import Skip, plan_orders


class ParserTests(unittest.TestCase):
    def test_classic_signal(self):
        r = parse("XAUUSD BUY NOW @ 2650.50\nSL: 2640\nTP1: 2655\nTP2: 2660\nTP3: 2670")
        s = r.signal
        self.assertEqual((s.side, s.kind), ("BUY", None))
        self.assertEqual((s.entry_low, s.entry_high), (2650.5, 2650.5))
        self.assertEqual(s.sl, 2640.0)
        self.assertEqual(s.tps, [2655.0, 2660.0, 2670.0])

    def test_range_and_tp_without_number(self):
        s = parse("🔥GOLD SELL 2650-2655 SL 2662 TP 2640 TP 2630").signal
        self.assertEqual(s.side, "SELL")
        self.assertEqual((s.entry_low, s.entry_high), (2650.0, 2655.0))
        self.assertEqual(s.sl, 2662.0)
        self.assertEqual(s.tps, [2640.0, 2630.0])

    def test_pending_explicit(self):
        s = parse("XAUUSD BUY LIMIT 2640 SL 2632 TP 2655").signal
        self.assertEqual(s.kind, "limit")
        self.assertEqual(s.entry_low, 2640.0)

    def test_spanish(self):
        s = parse("ORO XAUUSD COMPRA 2650 STOP LOSS 2640 TAKE PROFIT 2660").signal
        self.assertEqual((s.side, s.entry_low, s.sl, s.tps), ("BUY", 2650.0, 2640.0, [2660.0]))

    def test_thousands_comma(self):
        s = parse("XAUUSD BUY 2,650.5 SL 2,640 TP 2,660").signal
        self.assertEqual((s.entry_low, s.sl, s.tps), (2650.5, 2640.0, [2660.0]))

    def test_no_entry_price(self):
        s = parse("XAUUSD SELL NOW SL 2662 TP1 2640").signal
        self.assertIsNone(s.entry_low)

    def test_real_channel_formats(self):
        s = parse("SELL GOLD  3912 -  3917\n\nTakeprofit1 = 3902\nTakeprofit2 = 3892\n"
                  "Takeprofit3 = 3812\n\nStoploss = 3932").signal
        self.assertEqual((s.side, s.entry_low, s.entry_high, s.sl), ("SELL", 3912.0, 3917.0, 3932.0))
        self.assertEqual(s.tps, [3902.0, 3892.0, 3812.0])
        s = parse("IC GOLD👑️ VIP:\nSELL GOLD limit 3915.5 - 3919\n\nStoploss 3925\n\n"
                  "TP 3902 - 3890\n\nNote : use small lot").signal
        self.assertEqual((s.kind, s.entry_low, s.entry_high, s.sl), ("limit", 3915.5, 3919.0, 3925.0))
        self.assertEqual(s.tps, [3902.0, 3890.0])

    def test_real_channel_updates(self):
        for txt in ["45 pips, running SELL", "80 pips , 🔥🔥", "Not miss again 😡😡😡😡",
                    "160 pips ✅✅✅"]:
            self.assertFalse(parse(txt), txt)
        self.assertEqual([c.kind for c in parse("110 pips, now can set BE").commands], ["breakeven"])
        self.assertEqual([c.kind for c in parse("230 pips , close all").commands], ["close", "cancel"])
        self.assertEqual([c.kind for c in parse("Close all signal limit").commands], ["close", "cancel"])

    def test_chatter_is_ignored(self):
        self.assertFalse(parse("Buyers are strong today, good morning team!"))
        self.assertFalse(parse("Sell the rumor, buy the news"))

    def test_commands(self):
        self.assertEqual([c.kind for c in parse("Move SL to BE").commands], ["breakeven"])
        self.assertEqual([c.kind for c in parse("close all").commands], ["close", "cancel"])
        c = parse("TP1 hit. Close half and move to BE").commands
        self.assertEqual([(x.kind, x.value) for x in c], [("partial", 0.5), ("breakeven", None)])
        c = parse("Move SL to 2652").commands
        self.assertEqual((c[0].kind, c[0].value), ("move_sl", 2652.0))
        self.assertEqual([c.kind for c in parse("Cancel pending").commands], ["cancel"])

    def test_informational_not_a_command(self):
        self.assertFalse(parse("TP1 closed +50 pips"))


class PlannerTests(unittest.TestCase):
    def plan(self, text, bid=2650.0, ask=2650.3, **kw):
        return plan_orders(parse(text).signal, bid, ask, **kw)

    def test_market_with_split_tps(self):
        p = self.plan("XAUUSD BUY 2650 SL 2640 TP1 2655 TP2 2660 TP3 2670 TP4 2680")
        self.assertEqual([o.kind for o in p], ["market"] * 3)
        self.assertEqual([o.tp for o in p], [2655.0, 2660.0, 2670.0])

    def test_sell_tp_order(self):
        p = self.plan("XAUUSD SELL 2650 SL 2660 TP 2630 TP 2640")
        self.assertEqual([o.tp for o in p], [2640.0, 2630.0])

    def test_auto_pending(self):
        p = self.plan("XAUUSD BUY 2640 SL 2632 TP 2655")
        self.assertEqual((p[0].kind, p[0].price), ("limit", 2640.0))
        p = self.plan("XAUUSD BUY 2660 SL 2650 TP 2680")
        self.assertEqual(p[0].kind, "stop")

    def test_wrong_side_pending_skipped(self):
        with self.assertRaises(Skip):
            self.plan("XAUUSD BUY LIMIT 2660 SL 2650 TP 2680")

    def test_too_far(self):
        with self.assertRaises(Skip):
            self.plan("XAUUSD BUY 2500 SL 2490 TP 2520")

    def test_bad_sl_and_missing_sl(self):
        with self.assertRaises(Skip):
            self.plan("XAUUSD BUY 2650 SL 2660 TP 2670")
        with self.assertRaises(Skip):
            self.plan("XAUUSD BUY 2650 TP 2670")

    def test_no_tp_requires_flag(self):
        with self.assertRaises(Skip):
            self.plan("XAUUSD BUY 2650 SL 2640")
        p = self.plan("XAUUSD BUY 2650 SL 2640", allow_no_tp=True)
        self.assertEqual(p[0].tp, 0.0)


if __name__ == "__main__":
    unittest.main()
