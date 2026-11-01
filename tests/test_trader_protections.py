import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autotrade.control import Control
from autotrade.news import NewsFilter
from autotrade.parser import Command, parse
from autotrade.risk import RiskGuard
from tests.fake_mt5 import FakeMT5, NS, deal, position
from tests.test_trader import CH_A, CH_B, make

SIG = "SELL GOLD 2650 SL 2662 TP1 2640 TP2 2630"
NOW = 1_700_000_000          # = tick.time del MT5 falso (martes 14 nov 2023, 22:13:20 UTC)


def clock():
    return mock.patch("autotrade.trader.time.time", return_value=float(NOW))


class ProtectionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def trader(self, fake=None, **cfg):
        t = make(fake, **cfg)
        t.control = Control(self.dir / "control.json")
        return t


class PauseTests(ProtectionBase):
    def test_global_and_channel_pause_block_new_signals_only(self):
        t = self.trader()
        t.control.set_paused(True, 2)
        self.assertEqual(t.open_signal(parse(SIG).signal, 1, CH_B), 0)         # canal 2 en pausa
        self.assertEqual(t.outcome[0], "ignorada")
        self.assertIn("este canal está en pausa", t.msgs[-1][1])
        self.assertGreater(t.open_signal(parse(SIG).signal, 2, CH_A), 0)       # canal 1 sigue
        t.control.set_paused(True)
        n = len(t.mt5.sent)
        self.assertEqual(t.open_signal(parse(SIG).signal, 3, CH_A), 0)
        self.assertIn("el bot está en pausa", t.msgs[-1][1])
        self.assertEqual(len(t.mt5.sent), n)
        # los comandos de gestión NO se bloquean en pausa
        t.mt5.pos = [position(11, "SELL", 2650, volume=0.01, comment="tg1.2:1")]
        t.handle_command(Command("close"), None, CH_A)
        self.assertEqual(t.mt5.sent[-1]["position"], 11)


class RiskTests(ProtectionBase):
    def guard(self, **kw):
        return RiskGuard(self.dir / "risk.json", tz_hours=0, **kw)

    def test_floating_loss_trips_alerts_once_and_blocks(self):
        t = self.trader(close_on_limit=False)
        t.guard = self.guard(loss_pct=3.0)
        t.mt5.pos = [position(1, "SELL", 2650, profit=-35.0, comment="tg1.1:1")]          # -3.5% de 1000
        with clock():
            self.assertEqual(t.open_signal(parse(SIG).signal, 9, CH_A), 0)
            self.assertEqual(t.outcome[0], "ignorada")
            errors = [m for l, m in t.msgs if l == "error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("Límite diario alcanzado", errors[0])
            self.assertIn("/reanudar", errors[0])
            t.open_signal(parse(SIG).signal, 10, CH_A)                                    # segunda señal: aviso normal, no repetido
            self.assertEqual(len([m for l, m in t.msgs if l == "error"]), 1)
            self.assertIn("límite diario", t.msgs[-1][1])
        self.assertEqual(t.mt5.sent, [])

    def test_realized_loss_from_todays_deals_counts_and_old_or_foreign_deals_do_not(self):
        t = self.trader()
        t.guard = self.guard(loss_pct=2.0)
        t.mt5.deals = [deal(1, NOW - 3600, -15.0),                  # hoy, del bot
                       deal(1, NOW - 600, -8.0, swap=-0.5, commission=-0.5),   # hoy: total -9
                       deal(1, NOW - 90_000, -500.0),               # de ayer: no cuenta
                       deal(1, NOW - 60, -999.0, magic=1),          # de otro EA: no cuenta
                       deal(0, NOW - 60, 0.0)]                      # entrada: no cuenta
        with clock():
            bal, realized, floating = t.today_stats()
            self.assertAlmostEqual(realized, -24.0)
            self.assertEqual(floating, 0.0)
            self.assertEqual(t.open_signal(parse(SIG).signal, 1, CH_A), 0)                # 24 >= 20 (2% de 1000)

    def test_close_on_limit_closes_positions_and_cancels_pending(self):
        t = self.trader(close_on_limit=True)
        t.guard = self.guard(loss_pct=1.0)
        t.mt5.pos = [position(1, "SELL", 2650, profit=-12.0, volume=0.01, comment="tg1.1:1"),
                     position(2, "BUY", 2640, profit=0.0, volume=0.01, comment="tg2.4:1")]
        t.mt5.ords = [NS(ticket=50, magic=777, comment="tg1.8:1")]
        with clock():
            t.risk_tick()
        self.assertEqual(sorted(r.get("position") or r.get("order") for r in t.mt5.sent), [1, 2, 50])
        self.assertTrue(any("Se cerraron 2" in m for _, m in t.msgs))
        with clock():
            n = len(t.mt5.sent)
            t.risk_tick()                                            # no vuelve a cerrar ni avisar
            self.assertEqual(len(t.mt5.sent), n)

    def test_signal_counter_and_manual_resume(self):
        t = self.trader()
        t.guard = self.guard(max_signals=1)
        with clock():
            self.assertGreater(t.open_signal(parse(SIG).signal, 1, CH_A), 0)
            self.assertEqual(t.guard.state.signals, 1)
            self.assertEqual(t.open_signal(parse(SIG).signal, 2, CH_A), 0)
            t.guard.resume()
            self.assertGreater(t.open_signal(parse(SIG).signal, 3, CH_A), 0)

    def test_disabled_guard_is_never_consulted(self):
        t = self.trader()
        t.guard = self.guard()                       # sin límites
        t.mt5.pos = [position(1, "SELL", 2650, profit=-900.0, comment="tg1.1:1")]
        with clock():
            self.assertGreater(t.open_signal(parse(SIG).signal, 1, CH_A), 0)


class FilterTests(ProtectionBase):
    def test_spread_filter(self):
        t = self.trader(FakeMT5(bid=2650.0, ask=2651.2), max_spread_usd=0.5)
        self.assertEqual(t.open_signal(parse(SIG).signal, 1, CH_A), 0)
        self.assertIn("spread 1.20 USD > máximo 0.5 USD", t.outcome[1])
        t2 = self.trader(FakeMT5(bid=2650.0, ask=2650.3), max_spread_usd=0.5)
        self.assertGreater(t2.open_signal(parse(SIG).signal, 1, CH_A), 0)
        t3 = self.trader(FakeMT5(bid=2650.0, ask=2660.0), max_spread_usd=0)       # 0 = filtro apagado
        self.assertGreater(t3.open_signal(parse(SIG).signal, 1, CH_A), 0)

    def test_news_blackout(self):
        t = self.trader()
        # NOW = 22:13 UTC del martes; noticia manual «Tue 22:20» en UTC -> dentro de la ventana de ±15 min
        t.news = NewsFilter("manual", 15, 15, 0.0, "Tue 22:20 CPI")
        with clock():
            self.assertEqual(t.open_signal(parse(SIG).signal, 1, CH_A), 0)
        self.assertIn("CPI", t.outcome[1])
        self.assertIn("📰", t.msgs[-1][1])
        t.news = NewsFilter("manual", 15, 15, 0.0, "Tue 09:00 CPI")               # lejos: opera
        with clock():
            self.assertGreater(t.open_signal(parse(SIG).signal, 2, CH_A), 0)


class OutcomeTests(ProtectionBase):
    def test_outcome_describes_what_happened(self):
        t = self.trader()
        t.open_signal(parse(SIG).signal, 1, CH_A)
        self.assertEqual(t.outcome[0], "operada")
        self.assertIn("2/2 órdenes", t.outcome[1])
        t.open_signal(parse("SELL GOLD 2650 SL 2662").signal, 2, CH_A)             # sin TP
        self.assertEqual(t.outcome[0], "ignorada")
        self.assertIn("no trae TP", t.outcome[1])
        t2 = self.trader(FakeMT5(retcodes=[10013, 10013]))
        t2.open_signal(parse(SIG).signal, 3, CH_A)
        self.assertEqual(t2.outcome[0], "error")


class AutoBreakEvenTests(ProtectionBase):
    def trader_be(self, bid=2640.0, ask=2640.3):
        return self.trader(FakeMT5(bid=bid, ask=ask), auto_be=True)

    def test_moves_remaining_orders_to_entry_after_tp1_closes(self):
        t = self.trader_be()
        tp1 = position(1, "SELL", 2650, sl=2662, tp=2640, comment="tg1.5:1")
        tp2 = position(2, "SELL", 2650, sl=2662, tp=2630, comment="tg1.5:2")
        t.mt5.pos = [tp1, tp2]
        self.assertEqual(t.auto_be_pass(), 0)                       # TP1 abierto: todavía nada
        self.assertEqual(t.mt5.sent, [])
        t.mt5.pos = [tp2]                                           # TP1 se cerró (cumplió su objetivo)
        self.assertEqual(t.auto_be_pass(), 1)
        req = t.mt5.sent[-1]
        self.assertEqual((req["action"], req["position"], req["sl"], req["tp"]), (6, 2, 2650.0, 2630))
        self.assertTrue(any("Break-even automático" in m for _, m in t.msgs))
        n = len(t.mt5.sent)
        tp2.sl = 2650.0
        self.assertEqual(t.auto_be_pass(), 0)                       # no repite
        self.assertEqual(len(t.mt5.sent), n)

    def test_waits_until_the_position_is_in_profit(self):
        t = self.trader_be(bid=2652.0, ask=2652.3)                  # SELL: precio por encima de la entrada = pérdida
        tp1 = position(1, "SELL", 2650, sl=2662, comment="tg1.5:1")
        tp2 = position(2, "SELL", 2650, sl=2662, comment="tg1.5:2")
        t.mt5.pos = [tp1, tp2]
        t.auto_be_pass()
        t.mt5.pos = [tp2]
        self.assertEqual(t.auto_be_pass(), 0)                       # no está en ganancia: no se mueve
        self.assertEqual(t.mt5.sent, [])
        t.mt5.tick = NS(bid=2642.0, ask=2642.3, time=NOW)           # el precio vuelve a favor
        self.assertEqual(t.auto_be_pass(), 1)                       # reintenta y ahora sí

    def test_signal_closed_entirely_or_other_signals_are_untouched(self):
        t = self.trader_be()
        a1 = position(1, "SELL", 2650, sl=2662, comment="tg1.5:1")
        a2 = position(2, "SELL", 2650, sl=2662, comment="tg1.5:2")
        b1 = position(3, "SELL", 2650, sl=2662, comment="tg2.7:1")
        b2 = position(4, "SELL", 2650, sl=2662, comment="tg2.7:2")
        t.mt5.pos = [a1, a2, b1, b2]
        t.auto_be_pass()
        t.mt5.pos = [b1, b2]                                        # toda la señal A cerró (p. ej. por SL): nada que hacer
        self.assertEqual(t.auto_be_pass(), 0)
        self.assertEqual(t.mt5.sent, [])
        self.assertEqual(t._tp1_seen, {"tg2.7"})                    # olvida lo que ya no existe
        t.mt5.pos = [b2]                                            # TP1 de B cerró: solo B se protege
        t.auto_be_pass()
        self.assertEqual([r["position"] for r in t.mt5.sent], [4])

    def test_disabled_and_foreign_positions(self):
        t = self.trader(FakeMT5(), auto_be=False)
        t.mt5.pos = [position(1, "SELL", 2650, comment="tg1.5:1")]
        t.auto_be_pass()
        t.mt5.pos = [position(2, "SELL", 2650, comment="tg1.5:2")]
        self.assertEqual(t.auto_be_pass(), 0)
        t2 = self.trader_be()
        t2.mt5.pos = [position(1, "SELL", 2650, comment="manual", magic=777), position(2, "SELL", 2650, comment="tg1.5:1", magic=1)]
        self.assertEqual(t2.auto_be_pass(), 0)                      # comentarios ajenos / otro magic: se ignoran


class StatusAndCloseAllTests(ProtectionBase):
    def test_close_all_counts_and_cancels_pending(self):
        t = self.trader()
        t.mt5.pos = [position(1, "SELL", 2650, volume=0.01, comment="tg1.1:1"),
                     position(2, "BUY", 2640, volume=0.01, comment="tg2.1:1"),
                     position(3, "BUY", 2640, volume=0.01, comment="x", magic=5)]
        t.mt5.ords = [NS(ticket=70, magic=777, comment="tg2.2:1")]
        self.assertEqual(t.close_all(), 2)
        self.assertEqual(sorted(r.get("position") or r.get("order") for r in t.mt5.sent), [1, 2, 70])

    def test_status_text(self):
        t = self.trader()
        t.guard = RiskGuard(self.dir / "r.json", loss_pct=3.0, tz_hours=0)
        t.news = NewsFilter("manual", 15, 15, 0.0, "Tue 09:00 CPI")
        t.control.set_paused(True, 2)
        t.mt5.pos = [position(1, "SELL", 2650, profit=-4.0, comment="tg1.1:1")]
        t.account_desc = "cuenta 1 (DEMO)"
        with clock():
            txt = t.status_text([{"num": 1, "title": "IC GOLD", "ref": "-1"}, {"num": 2, "title": "", "ref": "@p"}])
        for needle in ("SIMULACIÓN" if t.dry_run else "OPERANDO", "canales en pausa: [2]", "flotante -4.00",
                       "Límite diario: no alcanzado", "1 posición(es)", "#1 IC GOLD: 1", "filtro activo"):
            self.assertIn(needle, txt)


if __name__ == "__main__":
    unittest.main()
