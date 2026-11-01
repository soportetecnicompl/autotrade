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
from typing import List, Optional

from .parser import Command, Signal
from .planner import OrderPlan, Skip, plan_orders

log = logging.getLogger("trader")


class Trader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dry_run: bool = cfg["dry_run"]
        self.symbol: str = cfg["symbol"]
        self.magic: int = cfg["magic"]
        self.mt5 = None
        self.si = None

    # ---------------------------------------------------------------- conexión
    def connect(self) -> None:
        try:
            import MetaTrader5 as mt5
        except ImportError as e:  # pragma: no cover
            raise SystemExit(
                "No se pudo importar MetaTrader5. Solo funciona en Windows: "
                "pip install MetaTrader5"
            ) from e
        self.mt5 = mt5
        kw = {}
        if self.cfg.get("path"):
            kw["path"] = self.cfg["path"]
        if self.cfg.get("login"):
            kw.update(login=int(self.cfg["login"]), password=self.cfg["password"],
                      server=self.cfg["server"])
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
        log.info("MT5 conectado: cuenta %s en %s (%s) balance=%.2f %s",
                 acc.login, acc.server,
                 "DEMO" if acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "REAL/CONCURSO",
                 acc.balance, acc.currency)
        if not mt5.terminal_info().trade_allowed:
            log.warning("El botón 'Algo Trading' del terminal está apagado: "
                        "no se podrán enviar órdenes.")

    def shutdown(self) -> None:
        if self.mt5:
            self.mt5.shutdown()

    # --------------------------------------------------------------- utilidades
    def _tag(self, msg_id: int) -> str:
        return f"tg{msg_id}"

    def _mine(self, items, msg_id: Optional[int]):
        out = []
        for it in items or []:
            if it.magic != self.magic:
                continue
            if msg_id is not None and it.comment.split(":")[0] != self._tag(msg_id):
                continue
            out.append(it)
        return out

    def positions(self, msg_id: Optional[int] = None):
        return self._mine(self.mt5.positions_get(symbol=self.symbol), msg_id)

    def orders(self, msg_id: Optional[int] = None):
        return self._mine(self.mt5.orders_get(symbol=self.symbol), msg_id)

    def _norm_volume(self, vol: float) -> float:
        step, vmin, vmax = self.si.volume_step, self.si.volume_min, self.si.volume_max
        vol = math.floor(vol / step + 1e-9) * step
        vol = min(max(vol, vmin), vmax)
        return round(vol, max(0, -int(math.floor(math.log10(step)))))

    def _total_volume(self, plan: OrderPlan) -> float:
        """Lote TOTAL de la señal (fijo, o por % de riesgo sobre la equity)."""
        risk_pct = self.cfg["risk_percent"]
        if risk_pct > 0:
            loss_per_lot = (abs(plan.price - plan.sl) / self.si.trade_tick_size
                            * self.si.trade_tick_value)
            if loss_per_lot > 0:
                equity = self.mt5.account_info().equity
                return equity * risk_pct / 100 / loss_per_lot
        return self.cfg["lot_size"]

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

    def _send(self, req: dict, label: str) -> bool:
        if self.dry_run:
            log.info("[DRY-RUN] %s -> %s", label, req)
            return True
        m = self.mt5
        for filling in (self._fillings() if req["action"] == m.TRADE_ACTION_DEAL
                        else [m.ORDER_FILLING_RETURN]):
            req = {**req, "type_filling": filling}
            res = m.order_send(req)
            if res is None:
                log.error("%s: order_send devolvió None (%s)", label, m.last_error())
                return False
            if res.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED):
                log.info("%s OK ticket=%s", label, res.order or res.deal)
                return True
            if res.retcode == m.TRADE_RETCODE_INVALID_FILL:
                continue
            log.error("%s FALLÓ retcode=%s %s", label, res.retcode, res.comment)
            return False
        log.error("%s FALLÓ: ningún modo de llenado aceptado", label)
        return False

    # -------------------------------------------------------------- señales
    def open_signal(self, sig: Signal, msg_id: int) -> int:
        """Abre las órdenes de una señal. Devuelve cuántas se enviaron."""
        m, c = self.mt5, self.cfg
        tick = m.symbol_info_tick(self.symbol)
        if tick is None:
            log.error("Sin precio para %s", self.symbol)
            return 0
        try:
            plans = plan_orders(
                sig, tick.bid, tick.ask,
                market_tolerance=c["market_tolerance"],
                max_pending_distance=c["max_pending_distance"],
                max_tps=c["max_tps"], allow_no_tp=c["allow_no_tp"],
            )
        except Skip as e:
            log.warning("Señal %s ignorada: %s", msg_id, e)
            return 0

        open_now = len(self.positions()) + len(self.orders())
        if open_now + len(plans) > c["max_open_positions"]:
            log.warning("Señal %s ignorada: límite max_open_positions=%s (hay %s)",
                        msg_id, c["max_open_positions"], open_now)
            return 0

        vols = self._split_volumes(self._total_volume(plans[0]), len(plans))
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
                "magic": self.magic, "comment": f"{self._tag(msg_id)}:{n}",
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
            sent += self._send(req, label)
        return sent

    # ------------------------------------------------------------- comandos
    def handle_command(self, cmd: Command, msg_id: Optional[int]) -> None:
        """msg_id = mensaje de la señal original (si el comando es una respuesta);
        None = aplica a todas las posiciones del bot."""
        scope = f"señal {msg_id}" if msg_id else "todas las del bot"
        log.info("Comando %s %s (%s)", cmd.kind, cmd.value or "", scope)
        if cmd.kind == "cancel":
            self._cancel(msg_id)
        elif cmd.kind == "close":
            self._close(msg_id, 1.0)
        elif cmd.kind == "partial":
            self._close(msg_id, cmd.value)
        elif cmd.kind == "breakeven":
            self._set_sl(msg_id, None)
        elif cmd.kind == "move_sl":
            self._set_sl(msg_id, cmd.value)

    def _cancel(self, msg_id) -> None:
        m = self.mt5
        for o in self.orders(msg_id):
            self._send({"action": m.TRADE_ACTION_REMOVE, "order": o.ticket},
                       f"cancelar orden {o.ticket}")

    def _close(self, msg_id, fraction: float) -> None:
        m = self.mt5
        tick = m.symbol_info_tick(self.symbol)
        for p in self.positions(msg_id):
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

    def _set_sl(self, msg_id, price: Optional[float]) -> None:
        """price=None -> break-even (precio de apertura + be_offset a favor)."""
        m = self.mt5
        tick = m.symbol_info_tick(self.symbol)
        digits = self.si.digits
        for p in self.positions(msg_id):
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
                continue
            self._send({"action": m.TRADE_ACTION_SLTP, "symbol": self.symbol,
                        "position": p.ticket, "sl": new_sl, "tp": p.tp,
                        "magic": self.magic},
                       f"SL posición {p.ticket} -> {new_sl}")
