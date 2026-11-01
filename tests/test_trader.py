import asyncio
import unittest

from autotrade.notifier import Notifier
from autotrade.parser import Command, parse
from autotrade.trader import Trader
from tests.fake_mt5 import FakeMT5, NS, position, symbol_info

CFG = dict(dry_run=False, symbol="XAUUSD", magic=777, deviation=30, lot_size=0.02,
           risk_percent=0.0, max_tps=2, max_open_positions=6, market_tolerance=1.5,
           max_pending_distance=30.0, pending_expiry_min=60, allow_no_tp=False, be_offset=0.0)


def make(fake=None, **over):
    t = Trader({**CFG, **over})
    t.mt5 = fake or FakeMT5()
    t.si = symbol_info()
    t.msgs = []
    t.notify = lambda text, level="info": t.msgs.append((level, text))
    return t


class OpenSignalTests(unittest.TestCase):
    def test_sell_two_tps_splits_lot(self):
        t = make()
        sig = parse("SELL GOLD 2650 - 2652\nSL 2662\nTP1 2640\nTP2 2630").signal
        self.assertEqual(t.open_signal(sig, 55), 2)
        r1, r2 = t.mt5.sent
        self.assertEqual((r1["volume"], r2["volume"]), (0.01, 0.01))
        self.assertEqual((r1["tp"], r2["tp"]), (2640.0, 2630.0))
        self.assertEqual((r1["comment"], r2["comment"]), ("tg55:1", "tg55:2"))
        self.assertEqual((r1["action"], r1["type"], r1["price"]), (1, 1, 2650.0))
        self.assertEqual((r1["magic"], r1["sl"]), (777, 2662.0))
        self.assertEqual(t.msgs[-1][0], "info")
        self.assertIn("2/2 órdenes", t.msgs[-1][1])
        self.assertTrue(t.msgs[-1][1].startswith("✅"))

    def test_pending_limit_has_expiry(self):
        t = make()
        sig = parse("SELL GOLD limit 2655 - 2658 SL 2665 TP 2640").signal
        t.open_signal(sig, 9)
        r = t.mt5.sent[0]
        self.assertEqual((r["action"], r["type"], r["price"]), (5, 3, 2655.0))
        self.assertEqual(r["expiration"], 1_700_000_000 + 3600)

    def test_failed_order_notifies_error(self):
        t = make(FakeMT5(retcodes=[10013]))
        sig = parse("SELL GOLD 2650 SL 2662 TP 2640").signal
        self.assertEqual(t.open_signal(sig, 1), 0)
        levels = [l for l, _ in t.msgs]
        self.assertIn("error", levels)
        self.assertTrue(any("FALLÓ" in m for _, m in t.msgs))
        self.assertTrue(any(m.startswith("⚠️") for _, m in t.msgs))   # 0/1 órdenes

    def test_invalid_fill_retries_next_mode(self):
        t = make(FakeMT5(retcodes=[10030]))
        sig = parse("SELL GOLD 2650 SL 2662 TP 2640").signal
        self.assertEqual(t.open_signal(sig, 1), 1)
        self.assertEqual(len(t.mt5.sent), 2)
        self.assertNotEqual(t.mt5.sent[0]["type_filling"], t.mt5.sent[1]["type_filling"])

    def test_skip_and_limits_are_warnings(self):
        t = make()
        t.open_signal(parse("SELL GOLD 2650 SL 2662").signal, 1)          # sin TP
        self.assertEqual(t.msgs[-1][0], "warn")
        t.mt5.pos = [position(i, "SELL", 2650) for i in range(6)]
        t.open_signal(parse("SELL GOLD 2650 SL 2662 TP 2640").signal, 2)
        self.assertEqual(t.msgs[-1][0], "warn")
        self.assertIn("máximo 6", t.msgs[-1][1])
        self.assertEqual(t.mt5.sent, [])

    def test_dry_run_sends_nothing(self):
        t = make(dry_run=True)
        self.assertEqual(t.open_signal(parse("SELL GOLD 2650 SL 2662 TP 2640").signal, 1), 1)
        self.assertEqual(t.mt5.sent, [])

    def test_min_lot_reduces_orders(self):
        t = make(lot_size=0.01)
        self.assertEqual(t.open_signal(parse("SELL GOLD 2650 SL 2662 TP1 2640 TP2 2630").signal, 1), 1)
        self.assertEqual(t.mt5.sent[0]["tp"], 2640.0)


class CommandTests(unittest.TestCase):
    def test_close_all_and_cancel(self):
        t = make()
        t.mt5.pos = [position(11, "SELL", 2650, volume=0.01), position(12, "BUY", 2640, volume=0.01),
                     position(13, "BUY", 2640, magic=1)]                        # otro magic: no se toca
        t.mt5.ords = [NS(ticket=21, magic=777, comment="tg1:2")]
        for c in parse("230 pips , close all").commands:
            t.handle_command(c, None)
        reqs = {r.get("position") or r.get("order"): r for r in t.mt5.sent}
        self.assertEqual(set(reqs), {11, 12, 21})
        self.assertEqual((reqs[11]["type"], reqs[11]["price"]), (0, 2650.3))   # cierra SELL comprando al ask
        self.assertEqual((reqs[12]["type"], reqs[12]["price"]), (1, 2650.0))   # cierra BUY vendiendo al bid
        self.assertEqual(reqs[21]["action"], 8)

    def test_scope_by_reply(self):
        t = make()
        t.mt5.pos = [position(11, "SELL", 2650, comment="tg1:1"), position(12, "SELL", 2650, comment="tg2:1")]
        t.handle_command(Command("close"), 2)
        self.assertEqual([r["position"] for r in t.mt5.sent], [12])

    def test_breakeven_only_when_in_profit(self):
        t = make(FakeMT5(bid=2640.0, ask=2640.3))
        t.mt5.pos = [position(11, "SELL", 2650, sl=2662, tp=2630),    # en ganancia -> BE
                     position(12, "SELL", 2630, sl=2640, tp=2620)]    # en pérdida -> omitido
        t.handle_command(Command("breakeven"), None)
        self.assertEqual([(r["position"], r["sl"], r["tp"]) for r in t.mt5.sent], [(11, 2650.0, 2630)])

    def test_partial_close_and_move_sl(self):
        t = make()
        t.mt5.pos = [position(11, "BUY", 2640, volume=0.04)]
        t.handle_command(Command("partial", 0.5), None)
        self.assertEqual(t.mt5.sent[0]["volume"], 0.02)
        t.handle_command(Command("move_sl", 2645.0), None)
        self.assertEqual((t.mt5.sent[1]["action"], t.mt5.sent[1]["sl"]), (6, 2645.0))


class HealthTests(unittest.TestCase):
    def test_health_states(self):
        t = make()
        self.assertEqual(t.health()[0], True)
        t.mt5.term.trade_allowed = False
        ok, why, fix = t.health()
        self.assertEqual((ok, fix), (False, False)); self.assertIn("Algo Trading", why)
        t.mt5.term.trade_allowed, t.mt5.term.connected = True, False
        self.assertIn("broker", t.health()[1])
        t.mt5.term = None
        self.assertEqual((t.health()[0], t.health()[2]), (False, True))
        self.assertTrue(t.reconnect())


class NotifierTests(unittest.TestCase):
    class Client:
        def __init__(self, fail=False):
            self.sent, self.fail = [], fail
        async def send_message(self, target, text):
            if self.fail:
                raise RuntimeError("sin red")
            self.sent.append((target, text))

    def test_levels_and_prefix(self):
        async def go():
            c = self.Client()
            n = Notifier(c, "me", True, signals=False, skips=True, prefix="[DRY-RUN] ")
            n.notify("info oculto", "info"); n.notify("skip", "warn"); n.notify("error", "error")
            await asyncio.sleep(0.05)
            return c.sent
        self.assertEqual(asyncio.run(go()), [("me", "[DRY-RUN] skip"), ("me", "[DRY-RUN] error")])

    def test_failure_never_raises_and_disabled_is_silent(self):
        async def go():
            n = Notifier(self.Client(fail=True), "me")
            n.notify("x", "error"); await n.send("y")
            c = self.Client(); Notifier(c, "me", enabled=False).notify("z", "error")
            await asyncio.sleep(0.05)
            return c.sent
        self.assertEqual(asyncio.run(go()), [])

    def test_no_loop_is_safe_and_numeric_target(self):
        Notifier(self.Client(), "me").notify("sin loop", "error")   # no debe lanzar
        self.assertEqual(Notifier(self.Client(), "123456").target, 123456)


if __name__ == "__main__":
    unittest.main()
