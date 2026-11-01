import asyncio
import unittest

from autotrade.notifier import Notifier
from autotrade.parser import Command, parse
from autotrade.trader import Trader
from tests.fake_mt5 import FakeMT5, NS, position, symbol_info

CFG = dict(dry_run=False, symbol="XAUUSD", magic=777, deviation=30, lot_size=0.02,
           risk_percent=0.0, max_tps=2, max_open_positions=6, market_tolerance=1.5,
           max_pending_distance=30.0, pending_expiry_min=60, allow_no_tp=False, be_offset=0.0,
           requote_retries=3, retry_delay_ms=0, retry_max_slippage=1.0, stops_policy="skip")


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
        self.assertEqual((r1["comment"], r2["comment"]), ("tg1.55:1", "tg1.55:2"))
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


SELL = "SELL GOLD 2650 SL 2662 TP 2640"
SELL_SIG = SELL
BUY = "BUY GOLD 2650 SL 2640 TP 2660"


CH_A = {"num": 1, "title": "IC GOLD", "lot_size": 0.0, "max_tps": 0}
CH_B = {"num": 2, "title": "Gold Pro", "lot_size": 0.04, "max_tps": 1}


class MultiChannelTests(unittest.TestCase):
    def test_tags_include_channel_so_message_ids_never_collide(self):
        t = make()
        sig = parse(SELL_SIG).signal
        t.open_signal(sig, 100, CH_A)
        t.open_signal(sig, 100, CH_B)                       # mismo id de mensaje, otro canal
        self.assertEqual([r["comment"] for r in t.mt5.sent if r["comment"].endswith(":1")],
                         ["tg1.100:1", "tg2.100:1"])

    def test_channel_overrides_lot_and_tps_and_titles_the_alerts(self):
        t = make()
        t.open_signal(parse("SELL GOLD 2650 SL 2662 TP1 2640 TP2 2630").signal, 7, CH_B)
        self.assertEqual(len(t.mt5.sent), 1)                # max_tps del canal = 1
        self.assertEqual(t.mt5.sent[0]["volume"], 0.04)     # lote propio del canal
        self.assertTrue(t.msgs[-1][1].startswith("✅ [Gold Pro] Señal 7"))
        t2 = make()
        t2.open_signal(parse("SELL GOLD 2650 SL 2662 TP1 2640 TP2 2630").signal, 7, CH_A)
        self.assertEqual([r["volume"] for r in t2.mt5.sent], [0.01, 0.01])   # usa los generales (0.02, 2 TPs)

    def test_command_without_reply_only_touches_its_own_channel(self):
        t = make()
        t.mt5.pos = [position(11, "SELL", 2650, volume=0.01, comment="tg1.5:1"),
                     position(12, "SELL", 2650, volume=0.01, comment="tg2.5:1"),
                     position(13, "SELL", 2650, volume=0.01, comment="tg2.9:1")]
        t.handle_command(Command("close"), None, CH_B)       # "close all" del canal B
        self.assertEqual(sorted(r["position"] for r in t.mt5.sent), [12, 13])

    def test_reply_scope_is_per_channel_and_message(self):
        t = make()
        t.mt5.pos = [position(11, "SELL", 2650, volume=0.01, comment="tg1.5:1"),
                     position(12, "SELL", 2650, volume=0.01, comment="tg2.5:1")]
        t.handle_command(Command("close"), 5, CH_B)          # reply al mensaje 5 DEL CANAL B
        self.assertEqual([r["position"] for r in t.mt5.sent], [12])

    def test_global_position_limit_counts_all_channels(self):
        t = make(max_open_positions=2)
        t.mt5.pos = [position(1, "SELL", 2650, comment="tg1.1:1"), position(2, "SELL", 2650, comment="tg2.1:1")]
        self.assertEqual(t.open_signal(parse(SELL_SIG).signal, 3, CH_A), 0)
        self.assertIn("máximo 2", t.msgs[-1][1])


class RetryTests(unittest.TestCase):
    def test_requote_retries_with_fresh_price(self):
        f = FakeMT5(retcodes=[10004, 10020])               # 2 requotes y luego DONE
        f.tick_after = [(2649.8, 2650.1), (2649.7, 2650.0)]
        t = make(f)
        self.assertEqual(t.open_signal(parse(SELL).signal, 1), 1)
        self.assertEqual([r["price"] for r in f.sent], [2650.0, 2649.8, 2649.7])
        self.assertTrue(any("tras 2 reintento" in m for _, m in t.msgs))

    def test_requote_adverse_move_aborts_open(self):
        f = FakeMT5(retcodes=[10004])
        f.tick_after = [(2648.0, 2648.3)]                  # SELL: precio 2 USD peor (bid más bajo)
        t = make(f)
        self.assertEqual(t.open_signal(parse(SELL).signal, 1), 0)
        self.assertEqual(len(f.sent), 1)                   # no reintentó
        self.assertTrue(any("NO ejecutada" in m for l, m in t.msgs if l == "error"))

    def test_favorable_move_is_accepted(self):
        f = FakeMT5(retcodes=[10004])
        f.tick_after = [(2651.5, 2651.8)]                  # SELL: precio mejor
        t = make(f)
        self.assertEqual(t.open_signal(parse(SELL).signal, 1), 1)
        self.assertEqual(f.sent[1]["price"], 2651.5)

    def test_retries_are_limited(self):
        f = FakeMT5(retcodes=[10004] * 10)
        t = make(f, requote_retries=2)
        self.assertEqual(t.open_signal(parse(SELL).signal, 1), 0)
        self.assertEqual(len(f.sent), 3)                   # 1 intento + 2 reintentos
        self.assertTrue(any("FALLÓ" in m for _, m in t.msgs))

    def test_close_is_not_blocked_by_slippage_guard(self):
        f = FakeMT5(retcodes=[10004])
        f.tick_after = [(2640.0, 2640.3)]                  # cierre de SELL: ask 10 USD distinto
        t = make(f)
        f.pos = [position(11, "SELL", 2650, volume=0.01)]
        t.handle_command(Command("close"), None)
        self.assertEqual(len(f.sent), 2)
        self.assertEqual(f.sent[1]["price"], 2640.3)

    def test_no_retry_when_disabled(self):
        f = FakeMT5(retcodes=[10004])
        self.assertEqual(make(f, requote_retries=0).open_signal(parse(SELL).signal, 1), 0)
        self.assertEqual(len(f.sent), 1)


class StopsLevelTests(unittest.TestCase):
    def make_lvl(self, level, **over):
        t = make(**over)
        t.si = symbol_info(stops_level=level)              # 300 puntos = 3.00 USD
        return t

    def test_skip_when_sl_too_close(self):
        t = self.make_lvl(300)
        sig = parse("SELL GOLD 2650 SL 2652 TP 2640").signal      # SL a 2.30 del ask 2650.3
        self.assertEqual(t.open_signal(sig, 1), 0)
        self.assertEqual(t.mt5.sent, [])
        self.assertEqual(t.msgs[-1][0], "warn")
        self.assertIn("demasiado cerca", t.msgs[-1][1])

    def test_adjust_moves_levels_and_warns(self):
        t = self.make_lvl(300, stops_policy="adjust")
        sig = parse("SELL GOLD 2650 SL 2652 TP 2640").signal
        self.assertEqual(t.open_signal(sig, 1), 1)
        r = t.mt5.sent[0]
        self.assertAlmostEqual(r["sl"], round(2650.3 + 3.01, 2))  # SELL: ref = ask
        self.assertEqual(r["tp"], 2640.0)
        self.assertTrue(any("ajustados" in m for l, m in t.msgs if l == "warn"))

    def test_ok_when_far_enough(self):
        t = self.make_lvl(300)
        self.assertEqual(t.open_signal(parse(SELL).signal, 1), 1)

    def test_be_skipped_when_too_close_to_price(self):
        f = FakeMT5(bid=2650.5, ask=2650.8)                # precio apenas pasó la entrada
        t = make(f); t.si = symbol_info(stops_level=300)
        f.pos = [position(11, "BUY", 2650, sl=2640, tp=2670)]
        t.handle_command(Command("breakeven"), None)       # SL 2650: bid - sl = 0.50 < 3.00
        self.assertEqual(f.sent, [])
        self.assertTrue(any("no se movió el SL" in m for l, m in t.msgs if l == "warn"))


class CommandTests(unittest.TestCase):
    def test_close_all_and_cancel(self):
        t = make()
        t.mt5.pos = [position(11, "SELL", 2650, volume=0.01), position(12, "BUY", 2640, volume=0.01),
                     position(13, "BUY", 2640, magic=1)]                        # otro magic: no se toca
        t.mt5.ords = [NS(ticket=21, magic=777, comment="tg1.1:2")]
        for c in parse("230 pips , close all").commands:
            t.handle_command(c, None)
        reqs = {r.get("position") or r.get("order"): r for r in t.mt5.sent}
        self.assertEqual(set(reqs), {11, 12, 21})
        self.assertEqual((reqs[11]["type"], reqs[11]["price"]), (0, 2650.3))   # cierra SELL comprando al ask
        self.assertEqual((reqs[12]["type"], reqs[12]["price"]), (1, 2650.0))   # cierra BUY vendiendo al bid
        self.assertEqual(reqs[21]["action"], 8)

    def test_scope_by_reply(self):
        t = make()
        t.mt5.pos = [position(11, "SELL", 2650, comment="tg1.1:1"), position(12, "SELL", 2650, comment="tg1.2:1")]
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
