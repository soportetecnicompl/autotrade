"""Análisis del backtest: métricas en %, comparación de estrategias, sensibilidad
al retraso, horario sugerido (validado fuera de muestra) y conclusiones.

Lógica pura (sin MT5 ni Telegram). Reutiliza el simulador de `backtest.py`.
"""
from __future__ import annotations

import html
import math
import random
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .backtest import Msg, Prices, SimConfig, Trade, _stats, simulate

DAY_NAMES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
MIN_RELIABLE = 30      # señales mínimas para fiarse de las conclusiones
LATENCIES = (5, 15, 30, 60, 120, 300)


# ------------------------------------------------------------------ métricas
def wilson(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Intervalo de confianza 95% (%) de una proporción."""
    if n == 0:
        return 0.0, 0.0
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    a = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - a) / d * 100, (c + a) / d * 100


def prob_positive(pnls: List[float], iters: int = 2000, seed: int = 1) -> Optional[float]:
    """% de remuestreos (bootstrap) cuya suma es > 0: cuán robusto es el resultado."""
    if len(pnls) < 5:
        return None
    rng = random.Random(seed)
    n = len(pnls)
    return sum(sum(rng.choice(pnls) for _ in range(n)) > 0 for _ in range(iters)) / iters * 100


def _local(ts: float, tz: float) -> datetime:
    return datetime.fromtimestamp(ts + tz * 3600, timezone.utc)


def summarize(trades: List[Trade], balance: float, tz: float = 0.0) -> dict:
    st = _stats(trades)
    sigs = st["sigs"]                                   # (ts, msg_id, pnl, trades), cronológico
    pnls = [s[2] for s in sigs]
    cum = peak = dd = dd_pct = 0.0
    equity = []
    for ts, _, p, _ in sigs:
        cum += p
        equity.append((ts, cum))
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        dd_pct = max(dd_pct, (peak - cum) / (balance + peak) * 100)
    months: Dict[str, List[float]] = defaultdict(list)
    for ts, _, p, _ in sigs:
        months[_local(ts, tz).strftime("%Y-%m")].append(p)
    durs = []
    for ts, _, _, ts_trades in sigs:
        ends = [t.exit_utc for t in ts_trades if t.exit_utc]
        if ends:
            durs.append((max(ends) - ts) / 3600)
    lo, hi = wilson(st["wins"], st["traded"])
    st.update(
        pnl_pct=st["pnl"] / balance * 100, dd=dd, dd_pct=dd_pct, equity=equity,
        win_ci=(lo, hi), p_pos=prob_positive(pnls),
        recovery=(st["pnl"] / dd) if dd > 0 else (float("inf") if st["pnl"] > 0 else 0.0),
        months={k: (len(v), sum(x > 0 for x in v), sum(v)) for k, v in sorted(months.items())},
        avg_hours=sum(durs) / len(durs) if durs else 0.0,
    )
    return st


# --------------------------------------------------------------------- horario
def hour_windows(hours) -> str:
    """{8,9,10,14} -> '08:00-11:00, 14:00-15:00' (fin exclusivo, formato del filtro)."""
    hs, out, i = sorted(set(hours)), [], 0
    while i < len(hs):
        j = i
        while j + 1 < len(hs) and hs[j + 1] == hs[j] + 1:
            j += 1
        out.append(f"{hs[i]:02d}:00-{hs[j] + 1:02d}:00")
        i = j + 1
    return ", ".join(out)


def _group(sigs, key):
    d = defaultdict(list)
    for s in sigs:
        d[key(s[0])].append(s[2])
    return d


def suggest_schedule(sigs, tz: float, min_n: int = 3, split: float = 0.7) -> dict:
    """Elige horas/días rentables con el 70% inicial del historial y los valida con
    el 30% final. Si no se sostienen fuera de muestra, el filtro es sobreajuste."""
    hour = lambda ts: _local(ts, tz).hour
    day = lambda ts: _local(ts, tz).weekday()
    n = len(sigs)
    k = int(n * split)
    train, test = sigs[:k], sigs[k:]
    out = {"n": n, "n_train": len(train), "n_test": len(test)}

    for name, key in (("hours", hour), ("days", day)):
        def pick(ss):
            return {h for h, v in _group(ss, key).items() if len(v) >= min_n and sum(v) > 0}
        sel_train = pick(train)
        test_all = sum(s[2] for s in test)
        test_sel = [s[2] for s in test if key(s[0]) in sel_train]
        res = {"train_sel": sorted(sel_train), "test_all": test_all, "test_sel": sum(test_sel),
               "test_sel_n": len(test_sel), "full_sel": sorted(pick(sigs))}
        if n < 20:
            res["verdict"] = "insuficiente"
        elif not sel_train:
            res["verdict"] = "ninguna"
        elif len(test_sel) >= 5 and sum(test_sel) > 0 and sum(test_sel) > test_all:
            res["verdict"] = "sostiene"
        else:
            res["verdict"] = "no_sostiene"
        out[name] = res
    return out


# ---------------------------------------------------------- estrategias/latencia
def run_sweep(msgs: List[Msg], P: Prices, base: SimConfig, balance: float, tz: float,
              tps_options=(1, 2, 3)) -> List[dict]:
    rows = []
    for k in tps_options:
        for follow in (False, True):
            tr = simulate(msgs, P, replace(base, max_tps=k), follow)
            s = summarize(tr, balance, tz)
            rows.append({"max_tps": k, "follow": follow, "stats": s, "trades": tr,
                         "label": f"{k} TP{'s' if k > 1 else ''} · {'sigue mensajes' if follow else 'solo SL/TP'}"})
    return rows


def run_latency(msgs, P, cfg: SimConfig, follow: bool, balance, tz) -> List[dict]:
    out = []
    for d in LATENCIES:
        s = summarize(simulate(msgs, P, replace(cfg, delay=d), follow), balance, tz)
        out.append({"delay": d, "pnl": s["pnl"], "win_rate": s["win_rate"], "traded": s["traded"]})
    return out


def suggested_lot(lot: float, max_dd: float, balance: float, target_dd_pct: float = 20.0,
                  safety: float = 2.0, step: float = 0.01) -> Optional[float]:
    """Lote total para que un drawdown `safety` veces el histórico no pase de target_dd_pct."""
    if max_dd <= 0:
        return None
    raw = lot * (balance * target_dd_pct / 100) / (max_dd * safety)
    return max(step, math.floor(raw / step + 1e-9) * step)


# ------------------------------------------------------------------- análisis
def analyze(msgs: List[Msg], P: Prices, cfg: SimConfig, balance: float = 1000.0,
            tz: float = -6.0, min_n: int = 3, sweep: bool = True) -> dict:
    skipped: Counter = Counter()
    static = simulate(msgs, P, cfg, False, skipped)
    follow = simulate(msgs, P, cfg, True)
    A = {"cfg": cfg, "balance": balance, "tz": tz, "skipped": skipped,
         "static": summarize(static, balance, tz), "follow": summarize(follow, balance, tz),
         "static_trades": static, "follow_trades": follow, "sweep": [], "latency": []}

    best_follow = A["follow"]["pnl"] >= A["static"]["pnl"]
    best_trades = follow if best_follow else static
    best_label = f"{cfg.max_tps} TPs · {'sigue mensajes' if best_follow else 'solo SL/TP'}"
    if sweep:
        rows = run_sweep(msgs, P, cfg, balance, tz)
        A["sweep"] = rows
        valid = [r for r in rows if r["stats"]["traded"] > 0]
        if valid:
            best = max(valid, key=lambda r: (r["stats"]["recovery"] if math.isfinite(r["stats"]["recovery"]) else 1e9,
                                             r["stats"]["pnl"]))
            A["best"] = best
            best_trades, best_follow = best["trades"], best["follow"]
            best_label = best["label"]
            A["latency"] = run_latency(msgs, P, replace(cfg, max_tps=best["max_tps"]),
                                       best_follow, balance, tz)
    A["best_label"], A["best_follow"] = best_label, best_follow
    A["best_stats"] = summarize(best_trades, balance, tz)
    A["schedule"] = suggest_schedule(A["best_stats"]["sigs"], tz, min_n)
    A["conclusions"] = conclusions(A)
    return A


# ---------------------------------------------------------------- conclusiones
def _money(x: float) -> str:
    return f"{x:+,.2f}"


def conclusions(A: dict) -> List[Tuple[str, str]]:
    """Lista de (nivel, texto). Nivel: ok | warn | bad | info."""
    out: List[Tuple[str, str]] = []
    s, cfg, bal = A["best_stats"], A["cfg"], A["balance"]
    n = s["traded"]

    if n == 0:
        return [("bad", "No se simuló ninguna operación: revisa precios, offset horario y formato de señales.")]
    if n < 10:
        out.append(("bad", f"Solo {n} señales operadas: la muestra es demasiado pequeña para sacar conclusiones."))
    elif n < MIN_RELIABLE:
        out.append(("warn", f"Solo {n} señales operadas (se recomiendan {MIN_RELIABLE}+): trata las conclusiones como orientativas."))

    lo, hi = s["win_ci"]
    p_pos = s["p_pos"]
    pf = s["profit_factor"]
    robust = f"; en {p_pos:.0f}% de los remuestreos el resultado fue positivo" if p_pos is not None else ""
    if s["expectancy"] > 0 and pf >= 1.3 and (p_pos or 0) >= 90 and n >= MIN_RELIABLE:
        lvl, verdict = "ok", "Rentable en este periodo y con una evidencia razonable"
    elif s["expectancy"] > 0 and pf > 1:
        lvl, verdict = "warn", "Resultado positivo pero no concluyente (puede ser suerte)"
    else:
        lvl, verdict = "bad", "No rentable con estas reglas: no la operes con dinero real"
    out.append((lvl, f"{verdict}. Mejor forma probada: {A['best_label']} → {_money(s['pnl'])} USD "
                     f"({s['pnl_pct']:+.1f}% sobre {bal:,.0f}), acierto {s['win_rate']:.0f}% "
                     f"(IC 95%: {lo:.0f}–{hi:.0f}%), factor de beneficio {pf:.2f}{robust}."))

    out.append(("bad" if s["dd_pct"] > 30 else "warn" if s["dd_pct"] > 15 else "info",
                f"Drawdown máximo: {s['dd']:.2f} USD ({s['dd_pct']:.1f}% del saldo), peor racha de "
                f"{s['worst_streak']} señales perdedoras. El drawdown futuro puede superar al histórico."))
    lot = suggested_lot(cfg.lot, s["dd"], bal, step=cfg.volume_step)
    if lot is not None:
        out.append(("info", f"Lote total sugerido: {lot:.2f} (con ese lote, un drawdown 2× el histórico "
                            f"costaría ~20% de {bal:,.0f}). Tu lote actual: {cfg.lot:.2f}."))

    if A.get("sweep") and A.get("best"):
        b = A["best"]
        out.append(("info",
                    f"Comparación de variantes: la mejor relación ganancia/drawdown es «{b['label']}» "
                    f"(max_tps = {b['max_tps']}, enable_commands = {'true' if b['follow'] else 'false'})."))
        out.append(("warn", f"Ojo: «{b['label']}» se eligió entre {len(A['sweep'])} variantes sobre los mismos datos, "
                            "así que su resultado está algo inflado (sesgo de selección). Si las diferencias entre "
                            "variantes son pequeñas, no hay una «mejor» real: quédate con la más simple."))
        f, st_ = A["follow"]["pnl"], A["static"]["pnl"]
        if abs(f - st_) > 0.1 * max(abs(f), abs(st_), 1):
            out.append(("info", "Obedecer los mensajes del canal " +
                        ("MEJORA" if f > st_ else "EMPEORA") +
                        f" el resultado ({_money(f)} frente a {_money(st_)} USD sin seguirlos)."))
        else:
            out.append(("info", "Seguir o no los mensajes del canal casi no cambia el resultado."))

    lat = A.get("latency") or []
    if len(lat) >= 2 and lat[0]["pnl"] != 0:
        p0 = lat[0]["pnl"]
        late = next((x for x in lat if x["delay"] == 60), lat[-1])
        if p0 > 0 and late["pnl"] <= 0:
            out.append(("bad", f"Muy sensible al retraso: con {lat[0]['delay']} s ganas {_money(p0)} USD pero con "
                               f"{late['delay']} s pasa a {_money(late['pnl'])}. Necesitas ejecución rápida y estable."))
        elif p0 > 0 and late["pnl"] < 0.7 * p0:
            out.append(("warn", f"El resultado cae de {_money(p0)} a {_money(late['pnl'])} USD si tardas "
                                f"{late['delay']} s en entrar: cuida la latencia (VPS, internet estable)."))
        else:
            out.append(("info", "El resultado no depende mucho del retraso de ejecución (hasta 5 minutos)."))

    sc = A["schedule"]
    for name, label, fmt in (("hours", "horario", lambda v: hour_windows(v)),
                             ("days", "días", lambda v: ",".join(DAY_NAMES[i] for i in v))):
        r = sc[name]
        if r["verdict"] == "insuficiente":
            out.append(("info", f"Filtro de {label}: hay muy pocas señales para validarlo; no filtres todavía."))
        elif r["verdict"] == "ninguna":
            out.append(("info", f"Filtro de {label}: ninguna franja fue consistentemente rentable; no filtres."))
        elif r["verdict"] == "sostiene":
            out.append(("ok", f"Filtro de {label} validado fuera de muestra: elegido con el 70% inicial ({fmt(r['train_sel'])}), "
                              f"en el 30% final dio {_money(r['test_sel'])} USD frente a {_money(r['test_all'])} sin filtro "
                              f"({r['test_sel_n']} señales). Sugerencia con todos los datos: {fmt(r['full_sel'])}."))
        else:
            out.append(("warn", f"Filtro de {label} NO se sostiene fuera de muestra (elegido con el 70% inicial dio "
                                f"{_money(r['test_sel'])} USD en el 30% final frente a {_money(r['test_all'])} sin filtro, "
                                f"{r['test_sel_n']} señales): probablemente es sobreajuste; no lo uses."))
    return out


# --------------------------------------------------------------------- salida
def render_text(A: dict) -> str:
    s, bal = A["best_stats"], A["balance"]
    L = ["", "=" * 74, "ANÁLISIS DEL CANAL", "=" * 74,
         f"Saldo de referencia: {bal:,.0f} USD | variante analizada: {A['best_label']}", "",
         f"Retorno total        {s['pnl']:>+10.2f} USD  ({s['pnl_pct']:+.1f}%)",
         f"Acierto              {s['win_rate']:>9.1f}%   IC 95%: {s['win_ci'][0]:.0f}–{s['win_ci'][1]:.0f}%  (n={s['traded']})",
         f"Factor de beneficio  {s['profit_factor']:>10.2f}",
         f"Drawdown máximo      {s['dd']:>10.2f} USD  ({s['dd_pct']:.1f}% del saldo)",
         f"Factor de recuperación {s['recovery']:>8.2f}   (ganancia / drawdown)",
         f"Duración media       {s['avg_hours']:>9.1f} h"]
    if s["p_pos"] is not None:
        L.append(f"Robustez             {s['p_pos']:>9.0f}%   de remuestreos con resultado positivo")
    if s["months"]:
        L += ["", "Por mes:", f"  {'mes':<9}{'señales':>8}{'ganadoras':>11}{'USD':>11}"]
        L += [f"  {k:<9}{n:>8}{w:>11}{p:>+11.2f}" for k, (n, w, p) in s["months"].items()]
    if A["sweep"]:
        L += ["", "Comparación de variantes (mismo lote total):",
              f"  {'variante':<30}{'señales':>8}{'acierto':>9}{'USD':>10}{'PF':>7}{'DD USD':>9}{'ganancia/DD':>13}"]
        for r in A["sweep"]:
            x = r["stats"]
            star = " ◄ mejor" if r is A.get("best") else ""
            rec = f"{x['recovery']:.2f}" if math.isfinite(x["recovery"]) else "∞"
            pf = f"{x['profit_factor']:.2f}" if math.isfinite(x["profit_factor"]) else "∞"
            L.append(f"  {r['label']:<30}{x['traded']:>8}{x['win_rate']:>8.0f}%{x['pnl']:>+10.2f}{pf:>7}"
                     f"{x['dd']:>9.2f}{rec:>13}{star}")
    if A["latency"]:
        L += ["", f"Sensibilidad al retraso de ejecución [{A['best_label']}]:",
              f"  {'retraso':<10}{'acierto':>9}{'USD':>10}"]
        L += [f"  {x['delay']:>5} s   {x['win_rate']:>8.0f}%{x['pnl']:>+10.2f}" for x in A["latency"]]
    sc = A["schedule"]
    L += ["", f"Horario sugerido (elegido con {sc['n_train']} señales, validado con las {sc['n_test']} siguientes):"]
    for name, label, fmt in (("hours", "horas", hour_windows), ("days", "días", lambda v: ",".join(DAY_NAMES[i] for i in v))):
        r = sc[name]
        L.append(f"  {label:<6} propuesta {fmt(r['full_sel']) or '—':<28} validación: {r['verdict']}")
    L += ["", "CONCLUSIONES"]
    icon = {"ok": "✔", "warn": "⚠", "bad": "✖", "info": "•"}
    L += [f" {icon[l]} {t}" for l, t in A["conclusions"]]
    L += ["", "Límites: velas M1 (orden intravela desconocido), sin slippage/swaps ni límite de posiciones simultáneas;",
          "el pasado no garantiza el futuro. Úsalo para descartar canales malos, no como promesa."]
    return "\n".join(L)


_CSS = """
:root{--bg:#fff;--fg:#1a1f2b;--mut:#667085;--card:#f5f6f8;--line:#e3e6ec;--ok:#0f8a4f;--bad:#c4321f;--warn:#b26b00;--acc:#2a5bd7}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e8ebf2;--mut:#98a2b3;--card:#1b2029;--line:#2a303c;--ok:#3ecf8e;--bad:#ff7a66;--warn:#f0b34a;--acc:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif;padding:24px 16px}
main{max-width:900px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px}
.sub{color:var(--mut);margin:0 0 20px}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:var(--card);border-radius:10px;padding:12px 14px}.kpi b{display:block;font-size:22px}.kpi span{color:var(--mut);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:6px 8px;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}th{color:var(--mut);font-weight:600}.tw{overflow-x:auto}
.pos{color:var(--ok)}.neg{color:var(--bad)}tr.best td{font-weight:700;background:var(--card)}
.bar{display:inline-block;height:10px;border-radius:3px;vertical-align:middle;min-width:1px}
.c{background:var(--card);border-left:4px solid var(--line);padding:10px 14px;border-radius:6px;margin:8px 0}
.c.ok{border-color:var(--ok)}.c.warn{border-color:var(--warn)}.c.bad{border-color:var(--bad)}.c.info{border-color:var(--acc)}
svg{width:100%;height:auto;background:var(--card);border-radius:10px}small{color:var(--mut)}
"""


def _cls(x: float) -> str:
    return "pos" if x > 0 else "neg" if x < 0 else ""


def _equity_svg(eq, balance) -> str:
    if len(eq) < 2:
        return "<p><small>Datos insuficientes para la curva.</small></p>"
    W, H, pl, pr, pt, pb = 760, 220, 56, 12, 12, 24
    xs, ys = [e[0] for e in eq], [0.0] + [e[1] for e in eq]
    x0, x1 = xs[0], xs[-1]
    ymin, ymax = min(ys), max(ys)
    if ymax == ymin:
        ymax = ymin + 1
    X = lambda t: pl + (t - x0) / max(1, x1 - x0) * (W - pl - pr)
    Y = lambda v: pt + (ymax - v) / (ymax - ymin) * (H - pt - pb)
    pts = f"{X(x0):.1f},{Y(0):.1f} " + " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in eq)
    d0 = datetime.fromtimestamp(x0, timezone.utc).strftime("%Y-%m-%d")
    d1 = datetime.fromtimestamp(x1, timezone.utc).strftime("%Y-%m-%d")
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Curva de capital acumulada en USD">'
            f'<line x1="{pl}" x2="{W - pr}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="currentColor" opacity=".25" stroke-dasharray="4 4"/>'
            f'<polyline points="{pts}" fill="none" stroke="var(--acc)" stroke-width="2"/>'
            f'<text x="4" y="{Y(ymax) + 10:.0f}" font-size="11" fill="currentColor" opacity=".7">{ymax:+.0f}</text>'
            f'<text x="4" y="{Y(ymin) - 2:.0f}" font-size="11" fill="currentColor" opacity=".7">{ymin:+.0f}</text>'
            f'<text x="{pl}" y="{H - 6}" font-size="11" fill="currentColor" opacity=".7">{d0}</text>'
            f'<text x="{W - pr}" y="{H - 6}" font-size="11" text-anchor="end" fill="currentColor" opacity=".7">{d1}</text></svg>')


def _bucket_html(sigs, key, labels, tz) -> str:
    g = _group(sigs, key)
    if not g:
        return ""
    mx = max(abs(sum(v)) for v in g.values()) or 1
    rows = []
    for k in sorted(g):
        v = g[k]
        p = sum(v)
        w = abs(p) / mx * 120
        col = "var(--ok)" if p > 0 else "var(--bad)"
        rows.append(f"<tr><td>{labels(k)}</td><td>{len(v)}</td><td>{sum(x > 0 for x in v) / len(v) * 100:.0f}%</td>"
                    f'<td class="{_cls(p)}">{p:+.2f}</td><td style="text-align:left"><span class="bar" style="width:{w:.0f}px;background:{col}"></span></td></tr>')
    return ('<div class="tw"><table><tr><th></th><th>señales</th><th>acierto</th><th>USD</th><th></th></tr>'
            + "".join(rows) + "</table></div>")


def render_html(A: dict, title: str = "Informe del canal de señales") -> str:
    e = html.escape
    s, bal, tz = A["best_stats"], A["balance"], A["tz"]
    k = lambda v, lab, c="": f'<div class="kpi"><b class="{c}">{e(v)}</b><span>{e(lab)}</span></div>'
    pf = f"{s['profit_factor']:.2f}" if math.isfinite(s["profit_factor"]) else "∞"
    h = [f"<!doctype html><html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{e(title)}</title><style>{_CSS}</style></head><body><main>",
         f"<h1>{e(title)}</h1>",
         f"<p class='sub'>Variante analizada: <b>{e(A['best_label'])}</b> · saldo de referencia {bal:,.0f} USD · "
         f"lote total {A['cfg'].lot} · spread {A['cfg'].spread} · retraso {A['cfg'].delay}s</p>",
         "<div class='kpis'>",
         k(f"{s['pnl']:+,.2f} USD", f"Retorno ({s['pnl_pct']:+.1f}%)", _cls(s["pnl"])),
         k(f"{s['win_rate']:.0f}%", f"Acierto (IC 95%: {s['win_ci'][0]:.0f}–{s['win_ci'][1]:.0f}%)"),
         k(pf, "Factor de beneficio"),
         k(f"{s['dd_pct']:.1f}%", f"Drawdown máx. ({s['dd']:.2f} USD)", "neg" if s["dd_pct"] > 15 else ""),
         k(str(s["traded"]), f"Señales operadas ({s['not_filled']} sin llenar)"),
         k(f"{s['worst_streak']}", "Peor racha perdedora"),
         "</div>", "<h2>Conclusiones</h2>"]
    h += [f"<div class='c {l}'>{e(t)}</div>" for l, t in A["conclusions"]]
    h += ["<h2>Curva de capital</h2>", _equity_svg(s["equity"], bal)]

    if s["months"]:
        h.append("<h2>Por mes</h2><div class='tw'><table><tr><th>Mes</th><th>Señales</th><th>Ganadoras</th><th>USD</th></tr>"
                 + "".join(f"<tr><td>{e(m)}</td><td>{n}</td><td>{w}</td><td class='{_cls(p)}'>{p:+.2f}</td></tr>"
                           for m, (n, w, p) in s["months"].items()) + "</table></div>")
    if A["sweep"]:
        rows = []
        for r in A["sweep"]:
            x = r["stats"]
            rec = f"{x['recovery']:.2f}" if math.isfinite(x["recovery"]) else "∞"
            pfx = f"{x['profit_factor']:.2f}" if math.isfinite(x["profit_factor"]) else "∞"
            rows.append(f"<tr class='{'best' if r is A.get('best') else ''}'><td>{e(r['label'])}</td><td>{x['traded']}</td>"
                        f"<td>{x['win_rate']:.0f}%</td><td class='{_cls(x['pnl'])}'>{x['pnl']:+.2f}</td><td>{pfx}</td>"
                        f"<td>{x['dd']:.2f}</td><td>{rec}</td></tr>")
        h.append("<h2>Mejores formas de operar el canal</h2><p class='sub'>Mismo lote total repartido entre los TPs. "
                 "En negrita: mejor relación ganancia/drawdown.</p><div class='tw'><table><tr><th>Variante</th><th>Señales</th>"
                 "<th>Acierto</th><th>USD</th><th>PF</th><th>DD USD</th><th>Ganancia/DD</th></tr>" + "".join(rows) + "</table></div>")
    if A["latency"]:
        h.append("<h2>Sensibilidad al retraso de ejecución</h2><div class='tw'><table><tr><th>Retraso</th><th>Acierto</th><th>USD</th></tr>"
                 + "".join(f"<tr><td>{x['delay']} s</td><td>{x['win_rate']:.0f}%</td><td class='{_cls(x['pnl'])}'>{x['pnl']:+.2f}</td></tr>"
                           for x in A["latency"]) + "</table></div>")
    sigs = s["sigs"]
    h.append(f"<h2>Mejor horario (hora local UTC{tz:+g})</h2>")
    h.append(_bucket_html(sigs, lambda t: _local(t, tz).hour, lambda hh: f"{hh:02d}:00", tz))
    h.append("<h2>Por día de la semana</h2>")
    h.append(_bucket_html(sigs, lambda t: _local(t, tz).weekday(), lambda d: DAY_NAMES[d], tz))
    sc = A["schedule"]
    h.append("<h2>Filtro de horario sugerido</h2><p class='sub'>Se elige con las primeras "
             f"{sc['n_train']} señales y se valida con las {sc['n_test']} siguientes. Solo úsalo si dice «sostiene».</p>"
             "<div class='tw'><table><tr><th></th><th>Propuesta</th><th>Validación fuera de muestra</th></tr>")
    names = {"sostiene": "se sostiene ✔", "no_sostiene": "NO se sostiene ✖ (sobreajuste)",
             "ninguna": "sin franja consistente", "insuficiente": "muestra insuficiente"}
    for nm, lab, fmt in (("hours", "Horas", hour_windows), ("days", "Días", lambda v: ",".join(DAY_NAMES[i] for i in v))):
        r = sc[nm]
        h.append(f"<tr><td>{lab}</td><td>{e(fmt(r['full_sel']) or '—')}</td><td>{names[r['verdict']]}</td></tr>")
    h.append("</table></div>")
    if sc["hours"]["verdict"] == "sostiene" and sc["hours"]["full_sel"]:
        h.append(f"<p><small>En config.ini: <code>trading_hours = {e(hour_windows(sc['hours']['full_sel']))}</code> · "
                 f"<code>tz_offset_hours = {tz:g}</code></small></p>")
    sk = A["skipped"]
    if sk:
        h.append("<h2>Señales descartadas</h2><div class='tw'><table>" +
                 "".join(f"<tr><td>{e(m)}</td><td>{n}</td></tr>" for m, n in sk.most_common()) + "</table></div>")
    h.append("<h2>Límites del análisis</h2><p><small>Velas M1 (se desconoce el orden dentro del minuto; si SL y TP caen en la misma vela "
             "gana el SL), spread fijo, sin slippage ni swaps, sin tope de posiciones simultáneas. Los mensajes borrados o editados "
             "no aparecen como se vieron en vivo. El pasado no garantiza el futuro: úsalo para descartar canales malos.</small></p>")
    h.append("</main></body></html>")
    return "".join(h)
