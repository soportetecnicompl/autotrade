"""MT5 falso mínimo para probar autotrade.trader sin Windows."""
from types import SimpleNamespace as NS

C = dict(
    TRADE_ACTION_DEAL=1, TRADE_ACTION_PENDING=5, TRADE_ACTION_SLTP=6, TRADE_ACTION_REMOVE=8,
    ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, ORDER_TYPE_BUY_LIMIT=2, ORDER_TYPE_SELL_LIMIT=3,
    ORDER_TYPE_BUY_STOP=4, ORDER_TYPE_SELL_STOP=5,
    ORDER_FILLING_FOK=0, ORDER_FILLING_IOC=1, ORDER_FILLING_RETURN=2,
    ORDER_TIME_GTC=0, ORDER_TIME_SPECIFIED=2,
    TRADE_RETCODE_DONE=10009, TRADE_RETCODE_PLACED=10008, TRADE_RETCODE_INVALID_FILL=10030,
    TRADE_RETCODE_REQUOTE=10004, TRADE_RETCODE_PRICE_CHANGED=10020, TRADE_RETCODE_PRICE_OFF=10021,
    POSITION_TYPE_BUY=0, POSITION_TYPE_SELL=1, ACCOUNT_TRADE_MODE_DEMO=0,
    DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_INOUT=2, DEAL_ENTRY_OUT_BY=3,
)


class FakeMT5(NS):
    def __init__(self, bid=2650.0, ask=2650.3, retcodes=None):
        super().__init__(**C)
        self.tick = NS(bid=bid, ask=ask, time=1_700_000_000)
        self.sent = []
        self.retcodes = list(retcodes or [])   # retcodes a devolver en orden; luego DONE
        self.pos, self.ords, self.deals = [], [], []
        self.term = NS(connected=True, trade_allowed=True)
        self.acc = NS(login=1, server="Demo", trade_mode=0, balance=1000.0, equity=1000.0, currency="USD")

    def symbol_info_tick(self, s): return self.tick
    def positions_get(self, symbol=None): return list(self.pos)
    def orders_get(self, symbol=None): return list(self.ords)
    def history_deals_get(self, a, b): return list(self.deals)
    def terminal_info(self): return self.term
    def account_info(self): return self.acc
    def last_error(self): return (1, "fake")
    def shutdown(self): pass
    def initialize(self, **kw): return True

    def order_send(self, req):
        self.sent.append(req)
        if getattr(self, "tick_after", None):
            bid, ask = self.tick_after.pop(0)
            self.tick = NS(bid=bid, ask=ask, time=self.tick.time)
        code = self.retcodes.pop(0) if self.retcodes else self.TRADE_RETCODE_DONE
        return NS(retcode=code, comment="fake comment", order=len(self.sent), deal=len(self.sent))


def symbol_info(filling_mode=3, stops_level=0):
    return NS(digits=2, point=0.01, trade_stops_level=stops_level, volume_step=0.01, volume_min=0.01, volume_max=100.0,
              filling_mode=filling_mode, trade_tick_size=0.01, trade_tick_value=1.0,
              visible=True)


def position(ticket, side, price_open, sl=0.0, tp=0.0, volume=0.02, comment="tg1.1:1", magic=777,
             profit=0.0, swap=0.0, time=1_700_000_000 - 60):
    return NS(ticket=ticket, type=0 if side == "BUY" else 1, price_open=price_open, sl=sl,
              tp=tp, volume=volume, comment=comment, magic=magic, profit=profit, swap=swap, time=time)


def deal(entry, time, profit, magic=777, swap=0.0, commission=0.0):
    return NS(entry=entry, time=time, profit=profit, swap=swap, commission=commission, fee=0.0, magic=magic)
