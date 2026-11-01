import random
import unittest
from datetime import datetime, timedelta, timezone

from autotrade.analysis import (analyze, conclusions, hour_windows, prob_positive, render_html,
                                render_text, suggest_schedule, summarize, suggested_lot, wilson)
from autotrade.backtest import Msg, Prices, SimConfig, Trade
from autotrade.schedule import TradingSchedule

BASE = datetime(2026, 10, 19, tzinfo=timezone.utc)


def ts(day, hour):
    return (BASE + timedelta(days=day, hours=hour)).timestamp()


def tr(msg_id, t, pnl):
    return Trade("x", msg_id, t, "BUY", "market", 1, 0.01, outcome="TP" if pnl > 0 else "SL",
                 pnl=pnl, exit_utc=t + 3600)


def sigs_from(pnls_hours):
    """[(day, hour, pnl)] -> estructura de _stats()['sigs']"""
    return [(ts(d, h), i, p, [tr(i, ts(d, h), p)]) for i, (d, h, p) in enumerate(pnls_hours)]


class MetricsTests(unittest.TestCase):
    def test_wilson_and_bootstrap(self):
        lo, hi = wilson(50, 100)                                   # valor de referencia conocido
        self.assertAlmostEqual(lo, 40.38, places=1)
        self.assertAlmostEqual(hi, 59.62, places=1)
        lo, hi = wilson(6, 10)                                     # muestra pequeña: intervalo muy ancho
        self.assertAlmostEqual(lo, 31.27, places=1)
        self.assertAlmostEqual(hi, 83.18, places=1)
        self.assertEqual(wilson(0, 0), (0.0, 0.0))
        self.assertGreater(prob_positive([5.0] * 20), 99)
        self.assertLess(prob_positive([-5.0] * 20), 1)
        self.assertIsNone(prob_positive([1, 2]))

    def test_summarize_drawdown_and_months(self):
        pnls = [50, -30, -40, 20, 10]
        trades = [tr(i, ts(i, 10), p) for i, p in enumerate(pnls)]
        s = summarize(trades, 1000.0, tz=0)
        self.assertAlmostEqual(s["pnl"], 10)
        self.assertAlmostEqual(s["dd"], 70)                      # 50 -> -20
        self.assertAlmostEqual(s["dd_pct"], 70 / 1050 * 100)     # pico 50 sobre saldo 1000
        self.assertAlmostEqual(s["pnl_pct"], 1.0)
        self.assertEqual(s["months"]["2026-10"], (5, 3, 10.0))
        self.assertEqual(s["worst_streak"], 2)
        self.assertAlmostEqual(s["recovery"], 10 / 70)
        self.assertAlmostEqual(s["avg_hours"], 1.0)

    def test_suggested_lot(self):
        self.assertEqual(suggested_lot(0.02, 100, 1000), 0.02)    # 0.02*200/(200)=0.02
        self.assertEqual(suggested_lot(0.10, 400, 1000), 0.02)    # 0.10*200/800=0.025 -> 0.02
        self.assertEqual(suggested_lot(0.01, 5000, 1000), 0.01)   # nunca bajo el mínimo
        self.assertIsNone(suggested_lot(0.02, 0, 1000))


class ScheduleSuggestionTests(unittest.TestCase):
    def test_hour_windows_roundtrip_with_filter(self):
        txt = hour_windows({8, 9, 10, 14, 23})
        self.assertEqual(txt, "08:00-11:00, 14:00-15:00, 23:00-24:00")
        sch = TradingSchedule.from_text("", txt, 0)               # el filtro real debe aceptarlo
        self.assertTrue(sch.allows(ts(0, 10)) and not sch.allows(ts(0, 11)) and sch.allows(ts(0, 23)))

    def test_overfit_is_detected(self):
        # Entrenamiento: 10h gana y 15h pierde. Test: se invierte -> el filtro no se sostiene.
        rows = []
        for d in range(14):
            rows += [(d, 10, 10.0), (d, 15, -10.0)]               # 28 señales (70%)
        for d in range(14, 20):
            rows += [(d, 10, -10.0), (d, 15, 10.0)]               # 12 señales
        r = suggest_schedule(sigs_from(rows), tz=0)
        self.assertEqual(r["hours"]["train_sel"], [10])
        self.assertEqual(r["hours"]["verdict"], "no_sostiene")
        self.assertLess(r["hours"]["test_sel"], 0)

    def test_consistent_pattern_is_confirmed(self):
        rows = []
        for d in range(20):
            rows += [(d, 10, 10.0), (d, 15, -10.0)]
        r = suggest_schedule(sigs_from(rows), tz=0)
        self.assertEqual(r["hours"]["verdict"], "sostiene")
        self.assertEqual(r["hours"]["full_sel"], [10])
        self.assertEqual(hour_windows(r["hours"]["full_sel"]), "10:00-11:00")

    def test_small_sample_and_timezone(self):
        rows = [(d, 10, 10.0) for d in range(8)]
        self.assertEqual(suggest_schedule(sigs_from(rows), 0)["hours"]["verdict"], "insuficiente")
        # misma señal a las 10:00 UTC es 04:00 en Honduras (-6)
        r = suggest_schedule(sigs_from([(d, 10, 10.0) for d in range(30)]), tz=-6)
        self.assertEqual(r["hours"]["full_sel"], [4])


def synthetic(seed=7, days=10):
    rnd = random.Random(seed)
    cur, end, p = BASE, BASE + timedelta(days=days), 3900.0
    bars, msgs, mid = [], [], 1
    while cur < end:
        if cur.weekday() < 5:
            o = p
            c = o + sum(rnd.gauss(0, 0.35) for _ in range(4))
            bars.append((int(cur.timestamp()), round(o, 2), round(max(o, c) + .2, 2), round(min(o, c) - .2, 2), round(c, 2)))
            p = c
        cur += timedelta(minutes=1)
    price = {b[0]: b for b in bars}
    for d in range(days):
        day = BASE + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for hr in (8, 14):
            t = day.replace(hour=hr, minute=3)
            px = price[int(t.timestamp())][1]
            side, sg = rnd.choice([("BUY", 1), ("SELL", -1)])
            msgs.append(Msg(mid, t.timestamp(), f"{side} GOLD {px:.0f} - {px + 2:.0f}\nTP1 {px + sg * 10:.0f}\n"
                                                f"TP2 {px + sg * 20:.0f}\nSL {px - sg * 20:.0f}"))
            mid += 1
            msgs.append(Msg(mid, (t + timedelta(minutes=90)).timestamp(), "close all"))
            mid += 1
    return msgs, Prices(bars)


class AnalyzeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        msgs, P = synthetic()
        cls.A = analyze(msgs, P, SimConfig(lot=0.02, spread=0.3, delay=5), balance=1000, tz=-6)

    def test_structure(self):
        A = self.A
        self.assertEqual(len(A["sweep"]), 6)
        self.assertEqual([x["delay"] for x in A["latency"]], [5, 15, 30, 60, 120, 300])
        self.assertIn("best", A)
        self.assertGreater(A["best_stats"]["traded"], 5)
        self.assertTrue(A["conclusions"])
        self.assertTrue(all(l in ("ok", "warn", "bad", "info") for l, _ in A["conclusions"]))
        # misma señal, mismas reglas: static/follow coinciden con el barrido de 2 TPs
        row = next(r for r in A["sweep"] if r["max_tps"] == 2 and not r["follow"])
        self.assertAlmostEqual(row["stats"]["pnl"], A["static"]["pnl"])

    def test_no_sweep_mode(self):
        msgs, P = synthetic(days=4)
        A = analyze(msgs, P, SimConfig(), sweep=False)
        self.assertEqual((A["sweep"], A["latency"]), ([], []))
        self.assertTrue(render_html(A).startswith("<!doctype html>"))

    def test_text_and_html(self):
        txt, page = render_text(self.A), render_html(self.A)
        for needle in ("ANÁLISIS DEL CANAL", "CONCLUSIONES", "Comparación de variantes", "Drawdown máximo"):
            self.assertIn(needle, txt)
        for needle in ("<svg", "Conclusiones", "Mejores formas de operar el canal", "Filtro de horario sugerido"):
            self.assertIn(needle, page)

    def test_html_escapes_content(self):
        A = dict(self.A)
        A["best_label"] = "<script>alert(1)</script>"
        A["conclusions"] = [("info", "<img src=x onerror=alert(1)>")]
        page = render_html(A)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)

    def test_empty_trades(self):
        A = dict(self.A)
        A["best_stats"] = summarize([], 1000.0)
        self.assertEqual(conclusions(A)[0][0], "bad")


if __name__ == "__main__":
    unittest.main()
