"""Ejecución en MetaTrader 5.

El paquete `MetaTrader5` de Python solo funciona en Windows, con el terminal
MT5 instalado. Se importa de forma perezosa para que el resto del proyecto
(parser, planner, tests) funcione en cualquier sistema.

Las órdenes/posiciones se identifican por `magic` y por el comentario
`tg<id_mensaje>:<n>`, así que no hace falta guardar estado en disco.
"""
from __future__ import annotations

import logging
import math
import re
import time
from typing import List, Optional

from .parser import Command, Signal
from .planner import OrderPlan, Skip, apply_stops_level, plan_orders
from .risk import seconds_into_day

log = logging.getLogger("trader")


class Trader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dry_run: bool = cfg["dry_run"]
        self.symbol: str = cfg["symbol"]
        self.magic: int = cfg["magic"]
        self.mt5 = None
        self.si = None
        self.account_desc = ""
        self._init_kw: dict = {}
        # protecciones y control: los asigna main.py (None = función desactivada)
        self.control = None            # autotrade.control.Control   (pausa)
        self.guard = None              # autotrade.risk.RiskGuard    (límite diario)
        self.news = None               # autotrade.news.NewsFilter   (noticias)
        self.outcome = ("", "")        # (estado, detalle) de la última señal, para la bandeja de señales
        self._tp1_seen: set = set()
        self._be_done: set = set()
        # notify(texto, nivel) la asigna main.py; por defecto no hace nada
        self.notify = lambda text, level="info": None

    # ---------------------------------------------------------------- conexión
    def connect(self) -> None:
        from .mt5_import import import_mt5
        try:
            mt5 = import_mt5()
        except ImportError as e:  # pragma: no cover
            raise SystemExit(
                f"No se pudo importar MetaTrader5 ({type(e).__name__}: {e}). Solo funciona en "
                "Windows y requiere la librería MetaTrader5 (pip install MetaTrader5)."
            ) from e
        self.mt5 = mt5
        kw = {}
        if self.cfg.get("path"):
            kw["path"] = self.cfg["path"]
        if self.cfg.get("login"):
            kw.update(login=int(self.cfg["login"]), password=self.cfg["password"],
                      server=self.cfg["server"])
        self._init_kw = kw
        if not mt5.initialize(**kw):
            raise SystemExit(f"mt5.initialize falló: {mt5.last_error()}")
        self.si = mt5.symbol_info(self.symbol)
        if self.si is None:
            raise SystemExit(
                f"Símbolo '{self.symbol}' no existe en este broker "
                "(revisa sufijos: XAUUSDm, XAUUSD.a, GOLD...)."
            )
        if not self.si.visible:
            mt5.symbol_select(self.symbol, True)
            self.si = mt5.symbol_info(self.symbol)
        acc = mt5.account_info()
        kind = "DEMO" if acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "REAL/CONCURSO"
        self.account_desc = (f"cuenta {acc.login} en {acc.server} ({kind}), "
                             f"balance {acc.balance:.2f} {acc.currency}")
        log.info("MT5 conectado: %s", self.account_desc)
        if not mt5.terminal_info().trade_allowed:
            log.warning("El botón 'Algo Trading' del terminal está apagado: "
                        "no se podrán enviar órdenes.")

    def health(self) -> tuple:
        """(ok, motivo, reconectable)"""
        m = self.mt5
        ti, ai = m.terminal_info(), m.account_info()
        if ti is None or ai is None:
            return False, "el terminal MT5 no responde", True
        if not ti.connected:
            return False, "MT5 perdió la conexión con el broker", False
        if not ti.trade_allowed:
            return False, "el botón 'Algo Trading' está apagado en MT5", False
        return True, "ok", False

    def reconnect(self) -> bool:
        try:
            self.mt5.shutdown()
            return bool(self.mt5.initialize(**self._init_kw))
        except Exception:  # noqa: BLE001
            log.exception("Reconexión a MT5 falló")
            return False

    def shutdown(self) -> None:
        if self.mt5:
            self.mt5.shutdown()

    # --------------------------------------------------------------- utilidades
    def _tag(self, ch_num: int, msg_id: int) -> str:
        """Etiqueta de una señal: tg<canal>.<mensaje> (los ids de mensaje se repiten entre canales)."""
        return f"tg{ch_num}.{msg_id}"

    def _mine(self, items, ch_num: Optional[int] = None, msg_id: Optional[int] = None):
        """Operaciones del bot. ch_num: solo las de ese canal; msg_id: solo las de esa señal."""
        out = []
        for it in items or []:
            if it.magic != self.magic:
                continue
            if ch_num is not None and not it.comment.startswith(f"tg{ch_num}."):
                continue
            if msg_id is not None and it.comment.split(":")[0] != self._tag(ch_num or 1, msg_id):
                continue
            out.append(it)
        return out

    def positions(self, ch_num: Optional[int] = None, msg_id: Optional[int] = None):
        return self._mine(self.mt5.positions_get(symbol=self.symbol), ch_num, msg_id)

    def orders(self, ch_num: Optional[int] = None, msg_id: Optional[int] = None):
        return self._mine(self.mt5.orders_get(symbol=self.symbol), ch_num, msg_id)

    def min_stop_distance(self) -> float:
        """Distancia mínima (USD) entre precio y SL/TP que exige el broker."""
        return float(getattr(self.si, "trade_stops_level", 0) or 0) * self.si.point

    def _norm_volume(self, vol: float) -> float:
        step, vmin, vmax = self.si.volume_step, self.si.volume_min, self.si.volume_max
        vol = math.floor(vol / step + 1e-9) * step
        vol = min(max(vol, vmin), vmax)
        return round(vol, max(0, -int(math.floor(math.log10(step)))))

    def _total_volume(self, plan: OrderPlan, lot: Optional[float] = None) -> float:
        """Lote TOTAL de la señal (fijo, o por % de riesgo sobre la equity). `lot`: lote propio del canal."""
        risk_pct = self.cfg["risk_percent"]
        if risk_pct > 0:
            loss_per_lot = (abs(plan.price - plan.sl) / self.si.trade_tick_size
                            * self.si.trade_tick_value)
            if loss_per_lot > 0:
                equity = self.mt5.account_info().equity
                return equity * risk_pct / 100 / loss_per_lot
        return lot or self.cfg["lot_size"]

    def _split_volumes(self, total: float, n: int) -> List[float]:
        """Reparte `total` en hasta n órdenes, cada una >= volume_min.
        El sobrante por redondeo va a la primera orden (TP más cercano)."""
        step, vmin = self.si.volume_step, self.si.volume_min
        n = max(1, min(n, int(math.floor(total / vmin + 1e-9))))
        per = self._norm_volume(total / n)
        vols = [per] * n
        extra = math.floor((total - per * n) / step + 1e-9) * step
        if extra > 0:
            vols[0] = self._norm_volume(vols[0] + extra)
        return vols

    def _fillings(self) -> list:
        m, mask = self.mt5, self.si.filling_mode
        modes = []
        if mask & 2:
            modes.append(m.ORDER_FILLING_IOC)
        if mask & 1:
            modes.append(m.ORDER_FILLING_FOK)
        modes.append(m.ORDER_FILLING_RETURN)
        return modes

    def _send(self, req: dict, label: str, guard: bool = False) -> bool:
        """Envía la solicitud. Ante requote / precio cambiado en órdenes a mercado
        reintenta con el precio actual (hasta `requote_retries`). Con guard=True
        (aperturas) aborta si el precio empeoró más de `retry_max_slippage` USD
        respecto al precio original; los cierres no se bloquean por eso."""
        if self.dry_run:
            log.info("[DRY-RUN] %s -> %s", label, req)
            return True
        m = self.mt5
        is_deal = req["action"] == m.TRADE_ACTION_DEAL
        fillings = self._fillings() if is_deal else [m.ORDER_FILLING_RETURN]
        retries = self.cfg["requote_retries"] if is_deal else 0
        requote = {m.TRADE_RETCODE_REQUOTE, m.TRADE_RETCODE_PRICE_CHANGED,
                   m.TRADE_RETCODE_PRICE_OFF}
        orig_price, attempt, fi = req.get("price"), 0, 0
        while fi < len(fillings):
            req = {**req, "type_filling": fillings[fi]}
            res = m.order_send(req)
            if res is None:
                log.error("%s: order_send devolvió None (%s)", label, m.last_error())
                self.notify(f"❌ {label}: MT5 no respondió ({m.last_error()})", "error")
                return False
            if res.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED):
                extra = f" tras {attempt} reintento(s)" if attempt else ""
                log.info("%s OK ticket=%s%s", label, res.order or res.deal, extra)
                if attempt:
                    self.notify(f"🔁 {label}: ejecutada{extra} (requote)", "info")
                return True
            if res.retcode == m.TRADE_RETCODE_INVALID_FILL:
                fi += 1
                continue
            if res.retcode in requote and attempt < retries:
                attempt += 1
                tick = m.symbol_info_tick(self.symbol)
                if tick is None:
                    break
                buy = req["type"] == m.ORDER_TYPE_BUY
                new = tick.ask if buy else tick.bid
                worse = (new - orig_price) if buy else (orig_price - new)
                if guard and worse > self.cfg["retry_max_slippage"]:
                    log.error("%s: precio empeoró %.2f USD (máx %.2f): se aborta", label, worse,
                              self.cfg["retry_max_slippage"])
                    self.notify(f"❌ {label} NO ejecutada: el precio se movió {worse:.2f} USD en "
                                f"contra (máx. permitido {self.cfg['retry_max_slippage']}). "
                                "Se cancela para no entrar a mal precio.", "error")
                    return False
                log.warning("%s: requote (código %s), reintento %s/%s a %s", label, res.retcode,
                            attempt, retries, new)
                req = {**req, "price": new}
                time.sleep(self.cfg["retry_delay_ms"] / 1000)
                continue
            log.error("%s FALLÓ retcode=%s %s", label, res.retcode, res.comment)
            self.notify(f"❌ {label} FALLÓ: {res.comment} (código {res.retcode})", "error")
            return False
        self.notify(f"❌ {label} FALLÓ: ningún modo de llenado aceptado o sin precio", "error")
        log.error("%s FALLÓ: ningún modo de llenado aceptado o sin precio", label)
        return False

    # -------------------------------------------------------------- señales
    def open_signal(self, sig: Signal, msg_id: int, channel: Optional[dict] = None) -> int:
        """Abre las órdenes de una señal de `channel`. Devuelve cuántas se enviaron."""
        m, c = self.mt5, self.cfg
        ch = channel or {"num": 1, "title": ""}
        num = ch.get("num", 1)
        who = f"[{ch['title']}] " if ch.get("title") else ""
        max_tps = ch.get("max_tps") or c["max_tps"]
        self.outcome = ("", "")
        if self._gate(num, who, msg_id):
            return 0
        tick = m.symbol_info_tick(self.symbol)
        if tick is None:
            self.outcome = ("error", "sin precio")
            log.error("Sin precio para %s", self.symbol)
            self.notify(f"❌ {who}Señal {msg_id} no operada: sin precio de {self.symbol}", "error")
            return 0
        max_spread = c.get("max_spread_usd") or 0
        if max_spread and tick.ask - tick.bid > max_spread:
            why = f"spread {tick.ask - tick.bid:.2f} USD > máximo {max_spread:g} USD"
            self.outcome = ("ignorada", why)
            self.notify(f"⚠️ {who}Señal {msg_id} ignorada: {why}", "warn")
            return 0
        try:
            plans = plan_orders(
                sig, tick.bid, tick.ask,
                market_tolerance=c["market_tolerance"],
                max_pending_distance=c["max_pending_distance"],
                max_tps=max_tps, allow_no_tp=c["allow_no_tp"],
            )
            plans, adjustments = apply_stops_level(
                plans, tick.bid, tick.ask, self.min_stop_distance(),
                c["stops_policy"], self.si.point)
            if adjustments:
                self.notify(f"⚠️ {who}Señal {msg_id}: niveles ajustados a la distancia mínima del "
                            f"broker: {'; '.join(sorted(set(adjustments)))}", "warn")
        except Skip as e:
            log.warning("Señal %s ignorada: %s", msg_id, e)
            self.outcome = ("ignorada", str(e))
            self.notify(f"⚠️ {who}Señal {msg_id} ignorada: {e}", "warn")
            return 0

        open_now = len(self.positions()) + len(self.orders())
        if open_now + len(plans) > c["max_open_positions"]:
            log.warning("Señal %s ignorada: límite max_open_positions=%s (hay %s)",
                        msg_id, c["max_open_positions"], open_now)
            self.outcome = ("ignorada", f"límite de posiciones abiertas ({open_now}/{c['max_open_positions']})")
            self.notify(f"⚠️ {who}Señal {msg_id} ignorada: ya hay {open_now} operaciones/pendientes "
                        f"(máximo {c['max_open_positions']})", "warn")
            return 0

        vols = self._split_volumes(self._total_volume(plans[0], ch.get("lot_size") or None), len(plans))
        if len(vols) < len(plans):
            log.warning("Lote total insuficiente para %s órdenes (mínimo %s): se abren %s",
                        len(plans), self.si.volume_min, len(vols))
        plans = plans[:len(vols)]

        sent = 0
        digits = self.si.digits
        for n, (p, vol) in enumerate(zip(plans, vols), 1):
            buy = p.side == "BUY"
            req = {
                "symbol": self.symbol, "volume": vol,
                "sl": round(p.sl, digits), "tp": round(p.tp, digits),
                "magic": self.magic, "comment": f"{self._tag(num, msg_id)}:{n}",
                "type_time": m.ORDER_TIME_GTC,
            }
            if p.kind == "market":
                req.update(
                    action=m.TRADE_ACTION_DEAL,
                    type=m.ORDER_TYPE_BUY if buy else m.ORDER_TYPE_SELL,
                    price=tick.ask if buy else tick.bid,
                    deviation=c["deviation"],
                )
            else:
                otype = {
                    ("BUY", "limit"): m.ORDER_TYPE_BUY_LIMIT,
                    ("BUY", "stop"): m.ORDER_TYPE_BUY_STOP,
                    ("SELL", "limit"): m.ORDER_TYPE_SELL_LIMIT,
                    ("SELL", "stop"): m.ORDER_TYPE_SELL_STOP,
                }[(p.side, p.kind)]
                req.update(action=m.TRADE_ACTION_PENDING, type=otype,
                           price=round(p.price, digits))
                if c["pending_expiry_min"] > 0:
                    req.update(type_time=m.ORDER_TIME_SPECIFIED,
                               expiration=int(tick.time + c["pending_expiry_min"] * 60))
            label = f"{p.side} {p.kind} {vol} lotes TP={p.tp or '-'} (msg {msg_id})"
            sent += self._send(req, label, guard=(p.kind == "market"))
        tps = ", ".join(str(p.tp) if p.tp else "-" for p in plans)
        self.notify(f"{'✅' if sent == len(plans) else '⚠️'} {who}Señal {msg_id}: {plans[0].side} "
                    f"{plans[0].kind} | {sent}/{len(plans)} órdenes | lotes {vols} | "
                    f"SL {plans[0].sl} | TP {tps}", "info")
        self.outcome = (("operada" if sent == len(plans) else "error" if sent == 0 else "parcial"),
                        f"{sent}/{len(plans)} órdenes {plans[0].side} {plans[0].kind} | SL {plans[0].sl} | TP {tps}")
        if sent and self.guard is not None:
            self.guard.register_signal()
        return sent

    # ------------------------------------------------------- protecciones y control
    def today_stats(self) -> tuple:
        """(saldo, ganancia realizada hoy por el bot, ganancia flotante de lo abierto del bot)."""
        m = self.mt5
        acc = m.account_info()
        floating = sum(float(getattr(p, "profit", 0) or 0) + float(getattr(p, "swap", 0) or 0)
                       for p in self.positions())
        realized = 0.0
        tick = m.symbol_info_tick(self.symbol)
        if tick is not None:
            tz = self.cfg.get("tz_offset_hours", 0.0)
            day_start = tick.time - seconds_into_day(time.time(), tz)      # inicio del día local, en hora del servidor
            try:
                deals = m.history_deals_get(int(tick.time - 36 * 3600), int(tick.time + 3600)) or []
            except Exception:  # noqa: BLE001 - sin historial no se corta el trading
                deals = []
            out_kinds = {m.DEAL_ENTRY_OUT, m.DEAL_ENTRY_INOUT, getattr(m, "DEAL_ENTRY_OUT_BY", 3)}
            for d in deals:
                if d.magic == self.magic and d.entry in out_kinds and d.time >= day_start:
                    realized += float(d.profit) + float(d.swap) + float(d.commission) + float(getattr(d, "fee", 0) or 0)
        return float(getattr(acc, "balance", 0) or 0), realized, floating

    def risk_check(self):
        bal, realized, floating = self.today_stats()
        return self.guard.check(time.time(), bal, realized, floating)

    def _on_trip(self, reason: str) -> None:
        self.notify(f"🛑 Límite diario alcanzado: {reason}.\nNo se abrirán más señales hoy. "
                    "Escribe /reanudar en «Mensajes guardados» (o usa el panel) si quieres seguir.", "error")
        if self.cfg.get("close_on_limit"):
            n = self.close_all()
            self.notify(f"🔒 Se cerraron {n} operación(es) abiertas por el límite diario.", "error")

    def risk_tick(self) -> None:
        """Revisión periódica: corta aunque no llegue ninguna señal (p. ej. por pérdida flotante)."""
        if self.guard is not None and self.guard.enabled:
            v = self.risk_check()
            if v.just_tripped:
                self._on_trip(v.reason)

    def _gate(self, num, who: str, msg_id) -> Optional[str]:
        """Motivo por el que NO se debe abrir esta señal (pausa, límite diario, noticias) o None."""
        if self.control is not None and self.control.is_paused(num):
            why = "el bot está en pausa" if self.control.read()["paused"] else "este canal está en pausa"
            self.outcome = ("ignorada", why)
            self.notify(f"⏸ {who}Señal {msg_id} ignorada: {why}", "warn")
            return why
        if self.guard is not None and self.guard.enabled:
            v = self.risk_check()
            if not v.allowed:
                self.outcome = ("ignorada", f"límite diario: {v.reason}")
                if v.just_tripped:
                    self._on_trip(v.reason)
                else:
                    self.notify(f"🛑 {who}Señal {msg_id} ignorada: límite diario alcanzado ({v.reason})", "warn")
                return v.reason
        if self.news is not None and self.news.enabled:
            why = self.news.check(time.time())
            if why:
                self.outcome = ("ignorada", why)
                self.notify(f"📰 {who}Señal {msg_id} ignorada por {why}", "warn")
                return why
        return None

    def close_all(self) -> int:
        """Cierra todas las posiciones del bot y cancela sus pendientes. Devuelve cuántas posiciones había."""
        n = len(self.positions())
        self._cancel(None, None)
        self._close(None, None, 1.0)
        return n

    def auto_be_pass(self) -> int:
        """Break-even automático: cuando la orden del TP1 de una señal ya se cerró y quedan otras de la
        misma señal, mueve su SL al precio de entrada (solo si ya están en ganancia). Devuelve cuántas señales."""
        if not self.cfg.get("auto_be"):
            return 0
        groups: dict = {}
        for p in self.positions():
            tag = p.comment.split(":")[0]
            if re.fullmatch(r"tg\d+\.\d+", tag):
                groups.setdefault(tag, []).append(p)
        applied = 0
        for tag, ps in groups.items():
            if any(p.comment.endswith(":1") for p in ps):
                self._tp1_seen.add(tag)             # el TP1 sigue abierto: aún no toca
            elif tag in self._tp1_seen and tag not in self._be_done:
                num, msg = (int(x) for x in tag[2:].split("."))
                if self._set_sl(num, msg, None):
                    self._be_done.add(tag)
                    applied += 1
                    self.notify(f"🛡 Break-even automático: se cumplió el TP1 de la señal {msg} "
                                f"(canal #{num}); el SL de las demás quedó en la entrada.", "info")
        alive = set(groups)                         # olvida las señales que ya no existen
        self._tp1_seen &= alive
        self._be_done &= alive
        return applied

    def status_text(self, channels: list) -> str:
        """Resumen para el comando /estado de Telegram."""
        bal, realized, floating = self.today_stats()
        pos, orders = self.positions(), self.orders()
        ctl = self.control.read() if self.control is not None else {"paused": False, "paused_channels": []}
        lines = [f"{'SIMULACIÓN' if self.dry_run else 'OPERANDO'} | {self.account_desc or 'MT5'}",
                 "⏸ EN PAUSA" if ctl["paused"] else
                 ("▶️ Activo" + (f" (canales en pausa: {ctl['paused_channels']})" if ctl["paused_channels"] else "")),
                 f"Hoy: realizado {realized:+.2f} | flotante {floating:+.2f} | saldo {bal:,.2f}"]
        if self.guard is not None and self.guard.enabled:
            st = self.guard.state
            lines.append("🛑 Límite diario ALCANZADO: " + st.reason if st.tripped else
                         f"Límite diario: no alcanzado ({st.signals} señales hoy)")
        lines.append(f"Abiertas: {len(pos)} posición(es), {len(orders)} pendiente(s)")
        for ch in channels:
            n = len(self.positions(ch["num"]))
            if n:
                lines.append(f"  #{ch['num']} {ch.get('title') or ch['ref']}: {n}")
        if self.news is not None and self.news.enabled:
            now_block = self.news.check(time.time())
            lines.append("📰 Noticias: " + (f"BLOQUEADO por {now_block}" if now_block else f"filtro activo ({self.news.mode})"))
        return "\n".join(lines)

    # ------------------------------------------------------------- comandos
    def handle_command(self, cmd: Command, msg_id: Optional[int] = None,
                       channel: Optional[dict] = None) -> None:
        """msg_id = mensaje de la señal original (si el comando es una respuesta).
        Sin msg_id aplica a todas las operaciones del canal `channel` (o del bot, si no se indica)."""
        num = channel.get("num") if channel else None
        who = f"[{channel['title']}] " if channel and channel.get("title") else ""
        if msg_id:
            scope = f"señal {msg_id}"
        else:
            scope = "todas las de este canal" if num is not None else "todas las del bot"
        log.info("%sComando %s %s (%s)", who, cmd.kind, cmd.value or "", scope)
        self.notify(f"🔧 {who}Comando del canal: {cmd.kind} {cmd.value or ''} ({scope})", "info")
        if cmd.kind == "cancel":
            self._cancel(num, msg_id)
        elif cmd.kind == "close":
            self._close(num, msg_id, 1.0)
        elif cmd.kind == "partial":
            self._close(num, msg_id, cmd.value)
        elif cmd.kind == "breakeven":
            self._set_sl(num, msg_id, None)
        elif cmd.kind == "move_sl":
            self._set_sl(num, msg_id, cmd.value)

    def _cancel(self, num, msg_id) -> None:
        m = self.mt5
        for o in self.orders(num, msg_id):
            self._send({"action": m.TRADE_ACTION_REMOVE, "order": o.ticket},
                       f"cancelar orden {o.ticket}")

    def _close(self, num, msg_id, fraction: float) -> None:
        m = self.mt5
        tick = m.symbol_info_tick(self.symbol)
        for p in self.positions(num, msg_id):
            vol = p.volume if fraction >= 1 else self._norm_volume(p.volume * fraction)
            if fraction < 1 and p.volume - vol < self.si.volume_min:
                log.info("Posición %s muy pequeña para cierre parcial", p.ticket)
                continue
            buy = p.type == m.POSITION_TYPE_BUY
            self._send({
                "action": m.TRADE_ACTION_DEAL, "symbol": self.symbol, "volume": vol,
                "type": m.ORDER_TYPE_SELL if buy else m.ORDER_TYPE_BUY,
                "position": p.ticket, "price": tick.bid if buy else tick.ask,
                "deviation": self.cfg["deviation"], "magic": self.magic,
                "comment": p.comment[:31],
            }, f"cerrar {vol} de posición {p.ticket}")

    def _set_sl(self, num, msg_id, price: Optional[float]) -> None:
        """price=None -> break-even (precio de apertura + be_offset a favor)."""
        m = self.mt5
        tick = m.symbol_info_tick(self.symbol)
        digits = self.si.digits
        ok = 0                                   # posiciones que ya quedaron con el SL pedido
        for p in self.positions(num, msg_id):
            buy = p.type == m.POSITION_TYPE_BUY
            if price is None:
                off = self.cfg["be_offset"]
                new_sl = p.price_open + (off if buy else -off)
                cur = tick.bid if buy else tick.ask
                if (buy and cur <= new_sl) or (not buy and cur >= new_sl):
                    log.info("Posición %s aún no está en ganancia: BE omitido", p.ticket)
                    continue
            else:
                new_sl = price
            new_sl = round(new_sl, digits)
            if abs(new_sl - p.sl) < 10 ** -digits:
                ok += 1                          # ya estaba así
                continue
            md = self.min_stop_distance()
            gap = (tick.bid - new_sl) if buy else (new_sl - tick.ask)
            if gap < md:
                log.warning("Posición %s: SL %s demasiado cerca del precio (mín. %.2f): omitido",
                            p.ticket, new_sl, md)
                self.notify(f"⚠️ Posición {p.ticket}: no se movió el SL a {new_sl} (queda a "
                            f"{gap:.2f} del precio; el broker exige {md:.2f})", "warn")
                continue
            ok += bool(self._send({"action": m.TRADE_ACTION_SLTP, "symbol": self.symbol,
                                   "position": p.ticket, "sl": new_sl, "tp": p.tp,
                                   "magic": self.magic},
                                  f"SL posición {p.ticket} -> {new_sl}"))
        return ok
