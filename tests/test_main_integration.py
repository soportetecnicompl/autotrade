"""Ejecuta main.run() de punta a punta con un Telegram y un MT5 simulados."""
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import main as bot_main
from autotrade import setup_logic as sl
from autotrade.channels import ChannelCfg
from autotrade.trader import Trader
from tests.fake_mt5 import FakeMT5, position as _position, symbol_info

MAGIC = 20260101          # el que escribe el asistente en config.ini
position = lambda *a, **k: _position(*a, magic=MAGIC, **k)

SIG = "SELL GOLD 2650 SL 2662 TP1 2640 TP2 2630"


class FakeClient:
    """Imita lo mínimo de telethon.TelegramClient que usa main.run()."""
    instance = None

    def __init__(self, *a, **k):
        FakeClient.instance = self
        self.handlers, self.sent = [], []
        self.scenario = None

    async def start(self, phone=None): ...
    async def disconnect(self): ...
    def is_connected(self): return True

    async def get_entity(self, ref):
        ids = {"-1001": -1001, "-1002": -1002, "@pro": -1002}
        if str(ref) not in ids:
            raise ValueError(f"no encuentro {ref}")
        return NS(id=ids[str(ref)], title={"-1001": "IC GOLD", "-1002": "Gold Pro", "@pro": "Gold Pro"}[str(ref)])

    def on(self, builder):
        def deco(fn):
            self.handlers.append((builder, fn))
            return fn
        return deco

    async def send_message(self, target, text):
        self.sent.append((target, text))

    async def run_until_disconnected(self):
        await self.scenario(self)


def build_modules():
    tele = types.ModuleType("telethon")
    tele.TelegramClient = FakeClient
    tele.events = types.SimpleNamespace(NewMessage=lambda chats=None, outgoing=False: NS(chats=chats, outgoing=outgoing))
    tele.utils = types.SimpleNamespace(get_peer_id=lambda ent: ent.id)
    return {"telethon": tele}


def channel_event(chat_id, text, msg_id, age=0, reply_to=None):
    date = datetime.now(timezone.utc) - timedelta(seconds=age)
    return NS(chat_id=chat_id, raw_text=text, message=NS(id=msg_id, date=date, reply_to_msg_id=reply_to))


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)
        self.fake = FakeMT5()

    def config(self, **over):
        s = sl.Settings(api_id="1", api_hash="a" * 32, phone="+50499998888", dry_run=False, lot_size=0.02,
                        channels=[ChannelCfg(1, "-1001", "IC GOLD"), ChannelCfg(2, "@pro", "Gold Pro", lot_size=0.04, max_tps=1),
                                  ChannelCfg(3, "-9999", "Inaccesible")])
        for k, v in over.items():
            setattr(s, k, v)
        Path("config.ini").write_text(sl.render_config(s), encoding="utf-8")
        return bot_main.load_config("config.ini")

    def run_bot(self, scenario, **over):
        cfg = self.config(**over)

        def fake_connect(trader):
            trader.mt5, trader.si, trader.account_desc = self.fake, symbol_info(), "cuenta de prueba"
        FakeClient.scenario = None
        orig_init = FakeClient.__init__

        def init(client, *a, **k):
            orig_init(client, *a, **k)
            client.scenario = scenario
        with mock.patch.dict(sys.modules, build_modules()), \
             mock.patch.object(Trader, "connect", fake_connect), \
             mock.patch.object(FakeClient, "__init__", init):
            asyncio.run(bot_main.run(cfg, False))
        return FakeClient.instance

    @staticmethod
    def handler(client, outgoing):
        return next(fn for b, fn in client.handlers if b.outgoing == outgoing)

    def inbox(self):
        return [json.loads(l) for l in Path("signals.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_channels_signals_commands_and_inbox(self):
        async def scenario(c):
            on_msg = self.handler(c, False)
            await on_msg(channel_event(-1001, SIG, 100))                                   # canal 1: 2 TPs, lote general
            await on_msg(channel_event(-1002, SIG, 100))                                   # canal 2: 1 TP, lote propio
            await on_msg(channel_event(-1001, "Buenos días equipo", 101))                  # charla: ni se registra
            await on_msg(channel_event(-1001, "BUY GOLD 3900 ahora mismo", 102))          # parece señal, no se entiende
            await on_msg(channel_event(-1001, SIG, 103, age=300))                          # llegó tarde
            await on_msg(channel_event(-5555, SIG, 104))                                   # chat que no es un canal elegido
            self.fake.pos = [position(11, "SELL", 2650, volume=0.01, comment="tg1.100:1"),
                             position(12, "SELL", 2650, volume=0.04, comment="tg2.100:1")]
            n = len(self.fake.sent)
            await on_msg(channel_event(-1002, "close all", 105))                           # solo afecta al canal 2
            self.closed = [r.get("position") for r in self.fake.sent[n:] if r.get("position")]
            await asyncio.sleep(0.05)
        client = self.run_bot(scenario)
        opens = [r for r in self.fake.sent if r.get("action") == 1 and "position" not in r
                 and r.get("comment", "").startswith("tg")]                                # aperturas (sin los cierres)
        comments = [r["comment"] for r in opens]
        self.assertEqual(comments, ["tg1.100:1", "tg1.100:2", "tg2.100:1"])
        vols = [r["volume"] for r in opens]
        self.assertEqual(vols, [0.01, 0.01, 0.04])                                          # 0.02 repartido / lote 0.04 del canal 2
        self.assertEqual(self.closed, [12])                                                 # «close all» del canal 2 no toca el canal 1
        rows = self.inbox()
        self.assertEqual([(r["channel"], r["msg_id"], r["status"]) for r in rows],
                         [(1, 100, "operada"), (2, 100, "operada"), (1, 102, "revisar"), (1, 103, "ignorada"),
                          (2, 105, "comando")])
        self.assertIn("2/2 órdenes", rows[0]["detail"])
        self.assertIn("retraso", rows[3]["detail"])
        texts = " ".join(t for _, t in client.sent)
        self.assertIn("[IC GOLD]", texts)
        self.assertIn("parece una señal pero no lo entendí", texts)
        self.assertIn("No se pudo acceder al canal «Inaccesible»", texts)                    # un canal malo no tumba a los demás
        self.assertIn("Canales (2)", texts)                                                  # arrancó con los 2 accesibles

    def test_telegram_commands_pause_status_and_close(self):
        replies = {}

        async def scenario(c):
            on_msg, on_saved = self.handler(c, False), self.handler(c, True)
            await on_saved(NS(raw_text="/pausa 1"))
            replies["pausa"] = c.sent[-1][1]
            await on_msg(channel_event(-1001, SIG, 200))                                   # canal 1 pausado
            await on_msg(channel_event(-1002, SIG, 200))                                   # canal 2 sigue
            await on_saved(NS(raw_text="/estado"))
            replies["estado"] = c.sent[-1][1]
            self.fake.pos = [position(21, "SELL", 2650, volume=0.01, comment="tg2.200:1")]
            await on_saved(NS(raw_text="/cerrar"))
            await on_saved(NS(raw_text="/cerrar confirmar"))
            replies["cerrar"] = c.sent[-1][1]
            await on_saved(NS(raw_text="/reanudar 1"))
            await on_saved(NS(raw_text="nota personal sin comando"))                        # se ignora
            await on_saved(NS(raw_text="/volar"))
            replies["volar"] = c.sent[-1][1]
            await asyncio.sleep(0.05)
        self.run_bot(scenario)
        self.assertIn("Canal #1", replies["pausa"])
        rows = {(r["channel"], r["status"]) for r in self.inbox()}
        self.assertEqual(rows, {(1, "ignorada"), (2, "operada")})                          # pausa respetada solo en el canal 1
        self.assertIn("pausa", [r["detail"] for r in self.inbox() if r["channel"] == 1][0])
        self.assertIn("canales en pausa: [1]", replies["estado"])
        self.assertIn("Cerradas 1 operación", replies["cerrar"])
        self.assertTrue(any(r.get("position") == 21 for r in self.fake.sent))
        self.assertIn("No entiendo", replies["volar"])
        self.assertEqual(json.loads(Path("control.json").read_text())["paused_channels"], [])   # /reanudar 1

    def test_daily_limit_blocks_and_housekeeping_keeps_running(self):
        async def scenario(c):
            on_msg = self.handler(c, False)
            self.fake.pos = [position(31, "SELL", 2650, volume=0.01, comment="tg1.1:1", profit=-40.0)]   # -4% de 1000
            await on_msg(channel_event(-1001, SIG, 300))
            await asyncio.sleep(0.05)
        client = self.run_bot(scenario, daily_loss_limit_pct=3.0)
        self.assertEqual([r for r in self.fake.sent if r.get("comment", "").startswith("tg1.300")], [])
        self.assertIn("Límite diario alcanzado", " ".join(t for _, t in client.sent))
        self.assertEqual(self.inbox()[0]["status"], "ignorada")
        self.assertTrue(json.loads(Path("risk_state.json").read_text())["tripped"])


if __name__ == "__main__":
    unittest.main()
