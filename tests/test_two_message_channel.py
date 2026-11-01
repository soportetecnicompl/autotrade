"""Canales que mandan primero «compra/venta» (se abre al instante) y después el SL; sin TP (se usa uno en pips)."""
import tempfile
import unittest
from pathlib import Path

from autotrade import setup_logic as sl
from autotrade.backtest import Msg, SimConfig, simulate
from autotrade.channels import ChannelCfg, parse_pip_list
from autotrade.parser import Command, Signal, parse, parse_bare_entry, parse_late_sl
from autotrade.planner import fill_defaults
from tests.fake_mt5 import FakeMT5, NS, position
from tests.test_backtest import T0, bars, flat
from tests.test_trader import make

NOW = 1_700_000_000
CH = {"num": 1, "title": "FxStreet", "entry_then_sl": True, "emergency_sl_pips": 100.0, "tp_pips": "30",
      "pip_size": 0.10, "late_sl_min": 30, "lot_size": 0.0, "max_tps": 0}


class ParserTests(unittest.TestCase):
    def test_bare_entries_open_a_signal_only_when_the_channel_is_in_this_mode(self):
        for t in ("BUY", "SELL", "sell now", "🔴 SELL GOLD", "XAUUSD BUY", "Gold buy ahora", "COMPRA", "VENTA YA",
                  "  Sell!  ", "BUY NOW 🚀"):
            s = parse(t, entry_then_sl=True).signal
            self.assertIsNotNone(s, t)
            self.assertEqual((s.sl, s.tps, s.entry_low), (None, [], None), t)
            self.assertFalse(parse(t).signal, t)                           # canal normal: se ignora, como antes
        self.assertEqual(parse("SELL", True).signal.side, "SELL")
        self.assertEqual(parse("compra", True).signal.side, "BUY")

    def test_bare_entry_with_a_price(self):
        s = parse("SELL GOLD @3896.5", entry_then_sl=True).signal
        self.assertEqual((s.side, s.entry_low, s.entry_high), ("SELL", 3896.5, 3896.5))
        self.assertEqual(parse_bare_entry("BUY 3900").entry_low, 3900.0)

    def test_chatter_is_not_an_entry(self):
        for t in ("Sell the rumor, buy the news", "Buyers are strong today", "BUY THE DIP TODAY", "Sell? what do you think",
                  "Good morning, who is buying?", "buy and hold", "Great trade, sell was perfect +40 pips", ""):
            self.assertIsNone(parse_bare_entry(t), t)
            self.assertFalse(parse(t, entry_then_sl=True).signal, t)

    def test_full_signals_still_parse_normally_in_this_mode(self):
        r = parse("SELL GOLD 3912 SL 3932 TP1 3902", entry_then_sl=True)
        self.assertEqual((r.signal.sl, r.signal.tps), (3932.0, [3902.0]))

    def test_late_sl_formats(self):
        for t, value, pips in (("SL 3935", 3935.0, False), ("SL: 3935", 3935.0, False), ("Stop loss 3935", 3935.0, False),
                               ("STOPLOSS = 3935.5", 3935.5, False), ("sl@3935", 3935.0, False),
                               ("SL 20 pips", 20.0, True), ("sl 25", 25.0, True), ("Stop 15 pip", 15.0, True),
                               ("SL: 3935 TP: 3900", 3935.0, False)):
            r = parse(t, entry_then_sl=True)
            self.assertEqual([(c.kind, c.value, c.pips) for c in r.commands], [("set_sl", value, pips)], t)
            self.assertIsNone(r.signal)
        self.assertEqual(parse_late_sl("SL 3935").value, 3935.0)
        self.assertIsNone(parse_late_sl("nothing here"))
        self.assertIsNone(parse_late_sl("SL 0"))

    def test_late_sl_only_in_this_mode_and_other_commands_unchanged(self):
        self.assertEqual(parse("SL 3935").commands, [])                                # canal normal: sin cambios
        self.assertEqual([c.kind for c in parse("SL: 3935").commands], ["move_sl"])     # canal normal: «mover SL» de siempre
        self.assertEqual([c.kind for c in parse("Move SL to BE", True).commands], ["breakeven"])
        self.assertEqual([c.kind for c in parse("close all", True).commands], ["close", "cancel"])
        self.assertEqual([c.kind for c in parse("110 pips, now can set BE", True).commands], ["breakeven"])


class DefaultsTests(unittest.TestCase):
    def test_sell_and_buy_levels_in_pips(self):
        s = fill_defaults(Signal("SELL"), bid=3896.58, ask=3896.84, emergency_sl_pips=100, tp_pips=[30], pip=0.10)
        self.assertEqual((s.sl, s.tps), (3906.58, [3893.58]))                          # 100 pips = 10 USD arriba; 30 pips = 3 USD abajo
        b = fill_defaults(Signal("BUY"), bid=3896.58, ask=3896.84, emergency_sl_pips=100, tp_pips=[30, 60], pip=0.10)
        self.assertEqual((b.sl, b.tps), (3886.84, [3899.84, 3902.84]))

    def test_never_overrides_what_the_signal_brings(self):
        s = fill_defaults(Signal("SELL", sl=3910.0, tps=[3890.0]), 3896.0, 3896.3, 100, [30], 0.10)
        self.assertEqual((s.sl, s.tps), (3910.0, [3890.0]))
        only_tp = fill_defaults(Signal("SELL", tps=[3890.0]), 3896.0, 3896.3, 100, [30], 0.10)
        self.assertEqual((only_tp.sl, only_tp.tps), (3906.0, [3890.0]))                # trae TP, falta SL
        none = fill_defaults(Signal("SELL"), 3896.0, 3896.3, 0, [], 0.10)
        self.assertEqual((none.sl, none.tps), (None, []))                              # sin defaults: no inventa nada

    def test_entry_price_is_the_reference_when_given(self):
        s = fill_defaults(Signal("BUY", entry_low=3900.0, entry_high=3900.0), 3896.0, 3896.3, 50, [30], 0.10)
        self.assertEqual((s.sl, s.tps), (3895.0, [3903.0]))

    def test_pip_list(self):
        self.assertEqual(parse_pip_list("30"), [30.0])
        self.assertEqual(parse_pip_list("30, 60;90"), [30.0, 60.0, 90.0])
        self.assertEqual(parse_pip_list(""), [])
        for bad in ("abc", "-5", "0", "30, x"):
            with self.assertRaises(ValueError, msg=bad):
                parse_pip_list(bad)


class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_validation(self):
        s = sl.Settings(api_id="1", api_hash="a" * 32, phone="+50499998888",
                        channels=[ChannelCfg(1, "-1001", "FxStreet", entry_then_sl=True, emergency_sl_pips=80,
                                             tp_pips="30, 60", pip_size=0.1, late_sl_min=45),
                                  ChannelCfg(2, "@x", "Normal")])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text(sl.render_config(s), encoding="utf-8")
            back = sl.load_settings(p)
            self.assertEqual(back.channels, s.channels)
            import main as bot_main
            ch = bot_main.load_config(str(p))["channels"]
            self.assertEqual((ch[0]["entry_then_sl"], ch[0]["emergency_sl_pips"], ch[0]["tp_pips"], ch[0]["late_sl_min"]),
                             (True, 80.0, "30, 60", 45))
            self.assertFalse(ch[1]["entry_then_sl"])
            self.assertEqual(ch[1]["pip_size"], 0.10)                                  # valores por defecto en un canal normal
        self.assertEqual(sl.validate_channel(s), [])
        bad = sl.Settings(channels=[ChannelCfg(1, "-1", "A", entry_then_sl=True, emergency_sl_pips=9999, tp_pips="treinta",
                                               pip_size=0.0, late_sl_min=0)])
        self.assertEqual(len(sl.validate_channel(bad)), 4)

    def test_old_channel_sections_load_with_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text("[telegram]\napi_id=1\n[channel.1]\nref = @a\ntitle = A\nlot_size = 0.03\n", encoding="utf-8")
            c = sl.load_settings(p).channels[0]
            self.assertEqual((c.entry_then_sl, c.emergency_sl_pips, c.tp_pips, c.pip_size, c.late_sl_min),
                             (False, 100.0, "", 0.10, 30))


class TraderTests(unittest.TestCase):
    def trader(self, **kw):
        t = make(FakeMT5(bid=3896.58, ask=3896.84), **kw)
        t.si.digits = 2
        return t

    def test_bare_sell_opens_immediately_with_emergency_sl_and_default_tp(self):
        t = self.trader()
        sig = parse("SELL", entry_then_sl=True).signal
        self.assertEqual(t.open_signal(sig, 500, CH), 1)                              # lote 0.02, 1 TP → 1 orden
        r = t.mt5.sent[0]
        self.assertEqual((r["action"], r["type"], r["price"], r["volume"]), (1, 1, 3896.58, 0.02))
        self.assertEqual((r["sl"], r["tp"], r["comment"]), (3906.58, 3893.58, "tg1.500:1"))
        self.assertEqual(t.outcome[0], "operada")
        self.assertIn("(emergencia)", t.outcome[1])
        msg = t.msgs[-1][1]
        self.assertIn("SL de EMERGENCIA a 100 pips (10 USD)", msg)
        self.assertIn("[FxStreet]", msg)

    def test_two_default_tps_split_the_lot(self):
        t = self.trader()
        t.open_signal(parse("BUY NOW", True).signal, 7, {**CH, "tp_pips": "30, 60"})
        self.assertEqual([(r["volume"], r["sl"], r["tp"]) for r in t.mt5.sent], [(0.01, 3886.84, 3899.84), (0.01, 3886.84, 3902.84)])

    def test_normal_channels_still_reject_a_signal_without_sl(self):
        t = self.trader()
        normal = {"num": 2, "title": "Normal", "entry_then_sl": False, "tp_pips": "30"}
        t.open_signal(Signal("SELL", tps=[3890.0]), 1, normal)                       # trae TP pero no SL: no se inventa un SL
        self.assertEqual(t.mt5.sent, [])
        self.assertIn("no trae SL", t.outcome[1])

    def test_tp_default_applies_to_normal_channels_too_when_configured(self):
        t = self.trader()
        t.open_signal(parse("SELL NOW SL 3910").signal, 1, {"num": 2, "title": "N", "tp_pips": "30", "pip_size": 0.1})
        self.assertEqual((t.mt5.sent[0]["sl"], t.mt5.sent[0]["tp"]), (3910.0, 3893.58))

    # ---- SL tardío
    def open_position(self, t, **kw):
        t.mt5.pos = [position(11, "SELL", 3896.58, sl=3906.58, tp=3893.58, volume=0.02, comment="tg1.500:1", **kw)]

    def test_late_sl_price_replaces_the_emergency_sl(self):
        t = self.trader()
        self.open_position(t)
        t.handle_command(Command("set_sl", 3900.0), None, CH)
        r = t.mt5.sent[-1]
        self.assertEqual((r["action"], r["position"], r["sl"], r["tp"]), (6, 11, 3900.0, 3893.58))
        self.assertTrue(any("SL del canal aplicado" in m and "[FxStreet]" in m for _, m in t.msgs))

    def test_late_sl_in_pips_is_measured_from_the_open_price(self):
        t = self.trader()
        self.open_position(t)
        t.handle_command(Command("set_sl", 25.0, pips=True), None, CH)                # 25 pips = 2.5 USD sobre 3896.58
        self.assertEqual(t.mt5.sent[-1]["sl"], 3899.08)
        t2 = self.trader()
        t2.mt5.pos = [position(12, "BUY", 3896.84, sl=3886.84, comment="tg1.9:1")]
        t2.mt5.tick = NS(bid=3897.5, ask=3897.8, time=NOW)
        t2.handle_command(Command("set_sl", 20.0, pips=True), None, CH)
        self.assertEqual(t2.mt5.sent[-1]["sl"], 3894.84)                              # BUY: por debajo de la entrada

    def test_without_reply_it_goes_to_the_most_recent_entry_of_that_channel_only(self):
        t = self.trader()
        t.mt5.pos = [position(1, "SELL", 3890.0, comment="tg1.100:1", time=NOW - 1200),
                     position(2, "SELL", 3896.0, comment="tg1.200:1", time=NOW - 300),          # la más reciente del canal 1
                     position(3, "SELL", 3896.0, comment="tg2.999:1", time=NOW - 10)]           # otro canal: no se toca
        t.handle_command(Command("set_sl", 3902.0), None, CH)
        self.assertEqual([(r["position"], r["sl"]) for r in t.mt5.sent], [(2, 3902.0)])

    def test_reply_targets_that_exact_signal(self):
        t = self.trader()
        t.mt5.pos = [position(1, "SELL", 3890.0, comment="tg1.100:1", time=NOW - 1200),
                     position(2, "SELL", 3896.0, comment="tg1.200:1", time=NOW - 60)]
        t.handle_command(Command("set_sl", 3895.0), 100, CH)                                 # responde al mensaje 100
        self.assertEqual([r["position"] for r in t.mt5.sent], [1])

    def test_old_entry_or_no_entry_is_not_touched_and_you_are_told(self):
        t = self.trader()
        t.mt5.pos = [position(1, "SELL", 3896.0, comment="tg1.100:1", time=NOW - 31 * 60)]   # 31 min > ventana de 30
        t.handle_command(Command("set_sl", 3902.0), None, CH)
        self.assertEqual(t.mt5.sent, [])
        self.assertIn("más de 30 min", t.msgs[-1][1])
        t2 = self.trader()
        t2.handle_command(Command("set_sl", 3902.0), None, CH)
        self.assertIn("no hay una operación abierta reciente", t2.msgs[-1][1])

    def test_price_already_beyond_the_sl_closes_the_trade(self):
        t = self.trader()
        t.mt5.tick = NS(bid=3903.0, ask=3903.3, time=NOW)                                    # SELL y el precio ya subió a 3903.3
        self.open_position(t)
        t.handle_command(Command("set_sl", 3902.0), None, CH)
        r = t.mt5.sent[-1]
        self.assertEqual((r["action"], r["position"], r["type"], r["price"]), (1, 11, 0, 3903.3))   # cierre a mercado (compra)
        self.assertTrue(any("ya superó el SL" in m for _, m in t.msgs))
        self.assertFalse(any(r.get("action") == 6 for r in t.mt5.sent))

    def test_sl_too_close_to_price_is_not_applied(self):
        t = self.trader()
        t.si.trade_stops_level = 300                                                          # 3 USD mínimo
        self.open_position(t)
        t.handle_command(Command("set_sl", 3898.0), None, CH)                                 # a 1.16 USD del ask 3896.84
        self.assertEqual(t.mt5.sent, [])
        self.assertTrue(any("el broker exige 3.00" in m for _, m in t.msgs))


class BacktestTests(unittest.TestCase):
    PRE = flat(2650, 10)

    def run_sim(self, msgs, path, follow=True, **kw):
        cfg = SimConfig(lot=0.01, spread=0.0, delay=0, max_tps=1, entry_then_sl=True, emergency_sl_pips=50,
                        tp_pips=(30.0,), pip=0.10, late_sl_min=30, max_hold_hours=2, **kw)
        return simulate(msgs, bars(path), cfg, follow)

    def test_default_tp_is_hit(self):
        t = self.run_sim([Msg(1, T0, "SELL")], self.PRE + flat(2650, 3) + [(2650, 2650, 2646.5, 2647)])
        self.assertEqual((t[0].outcome, t[0].tp, t[0].sl), ("TP", 2647.0, 2655.0))        # 30 pips = 3 USD; emergencia 50 pips = 5 USD
        self.assertAlmostEqual(t[0].pnl, 3.0)

    def test_emergency_sl_protects_until_the_channels_sl_arrives(self):
        msgs = [Msg(1, T0, "SELL"), Msg(2, T0 + 600, "SL 2660")]                               # el SL llega a los 10 min
        spike = self.PRE + flat(2650, 2) + [(2650, 2656, 2650, 2655)]                          # antes de eso toca la emergencia (2655)
        t = self.run_sim(msgs, spike)
        self.assertEqual(t[0].outcome, "SL")
        self.assertAlmostEqual(t[0].pnl, -5.0)

    def test_late_sl_replaces_the_emergency_and_trade_survives_the_spike(self):
        msgs = [Msg(1, T0, "SELL"), Msg(2, T0 + 60, "SL 2660")]                                # SL a 10 USD: llega en el minuto 1
        path = self.PRE + flat(2650, 2) + [(2650, 2657, 2650, 2656)] + [(2656, 2656, 2646, 2647)]
        for follow in (True, False):                                                           # forma parte de la señal: ambos modos
            t = self.run_sim(msgs, path, follow=follow)
            self.assertEqual((t[0].outcome, round(t[0].pnl, 2)), ("TP", 3.0), follow)

    def test_late_sl_in_pips_and_sl_hit(self):
        msgs = [Msg(1, T0, "SELL"), Msg(2, T0 + 60, "SL 20 pips")]                              # 20 pips = 2 USD → 2652
        t = self.run_sim(msgs, self.PRE + flat(2650, 2) + [(2650, 2652.5, 2650, 2652)])
        self.assertEqual(t[0].outcome, "SL")
        self.assertAlmostEqual(t[0].pnl, -2.0)

    def test_sl_that_is_already_crossed_closes_at_once(self):
        msgs = [Msg(1, T0, "SELL"), Msg(2, T0 + 180, "SL 2653")]
        path = self.PRE + flat(2650, 2) + [(2650, 2654, 2650, 2654)] + flat(2654, 3)            # a los 3 min el precio ya está en 2654
        t = self.run_sim(msgs, path)
        self.assertEqual(t[0].outcome, "SL")
        self.assertAlmostEqual(t[0].pnl, -4.0)

    def test_sl_after_the_window_is_ignored(self):
        msgs = [Msg(1, T0, "SELL"), Msg(2, T0 + 3600, "SL 2653")]                             # 60 min > ventana de 30
        spike = self.PRE + flat(2650, 62) + [(2650, 2654, 2650, 2654)]                         # a los 62 min sube a 2654
        t = self.run_sim(msgs, spike)
        # Si el SL tardío se hubiera aceptado, habría cerrado en 2653/2654 (pérdida ≤ 4). Con la emergencia (2655)
        # el precio 2654 no la toca: la operación sigue abierta hasta el cierre por tiempo.
        self.assertNotEqual(t[0].outcome, "SL")
        self.assertGreater(t[0].pnl, -4.0 + 1e-9)

    def test_a_reply_applies_to_that_entry_only(self):
        msgs = [Msg(1, T0, "SELL"), Msg(2, T0 + 300, "BUY"), Msg(3, T0 + 360, "SL 2653", reply_to=1)]
        # minuto 7: el precio sube a 2653.5. SELL (SL 2653 por la respuesta) salta; BUY (emergencia 2645) llega a su TP 2653.
        path = self.PRE + flat(2650, 6) + [(2650, 2650, 2650, 2650), (2650, 2653.5, 2649, 2653.2)]
        t = self.run_sim(msgs, path, follow=False)
        sell = next(x for x in t if x.side == "SELL")
        buy = next(x for x in t if x.side == "BUY")
        self.assertEqual((sell.outcome, round(sell.pnl, 2)), ("SL", -3.0))                      # el SL del canal (2653) se aplicó
        self.assertEqual((buy.outcome, round(buy.pnl, 2)), ("TP", 3.0))                         # el BUY NO recibió ese SL (le habría cerrado a 0)

    def test_without_reply_the_sl_goes_to_the_latest_entry(self):
        msgs = [Msg(1, T0, "SELL"), Msg(2, T0 + 300, "BUY"), Msg(3, T0 + 360, "SL 2646")]      # sin reply: es del BUY (el último)
        path = self.PRE + flat(2650, 6) + [(2650, 2650, 2650, 2650), (2650, 2650, 2645.5, 2646), (2646, 2646, 2646, 2646)]
        t = self.run_sim(msgs, path, follow=False)
        buy = next(x for x in t if x.side == "BUY")
        sell = next(x for x in t if x.side == "SELL")
        self.assertEqual((buy.outcome, round(buy.pnl, 2)), ("SL", -4.0))                        # SL 2646 del BUY (entró en 2650)
        self.assertEqual(sell.outcome, "TP")                                                    # el SELL sigue con su TP (2647) y lo cumple

    def test_a_normal_channel_ignores_bare_messages(self):
        cfg = SimConfig(lot=0.01, spread=0.0, delay=0, entry_then_sl=False)
        self.assertEqual(simulate([Msg(1, T0, "SELL")], bars(self.PRE + flat(2650, 5)), cfg, True), [])


if __name__ == "__main__":
    unittest.main()
