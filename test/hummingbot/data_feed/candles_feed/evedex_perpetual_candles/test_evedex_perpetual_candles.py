import asyncio
import json
import re
from test.hummingbot.data_feed.candles_feed.test_candles_base import TestCandlesBase
from unittest.mock import AsyncMock, patch

from aioresponses import aioresponses

from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.data_feed.candles_feed.evedex_perpetual_candles import EvedexPerpetualCandles


class TestEvedexPerpetualCandles(TestCandlesBase):
    __test__ = True
    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "XRP"
        cls.quote_asset = "USDT"
        cls.interval = "15m"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = "XRPUSD"
        cls.max_records = 150

    def setUp(self) -> None:
        super().setUp()
        self.data_feed = EvedexPerpetualCandles(trading_pair=self.trading_pair, interval=self.interval)
        self.data_feed._instrument_resolved = True
        self.data_feed._ex_trading_pair = self.ex_trading_pair

        self.log_records = []
        self.data_feed.logger().setLevel(1)
        self.data_feed.logger().addHandler(self)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.mocking_assistant = NetworkMockingAssistant()
        self.resume_test_event = asyncio.Event()

    def get_fetch_candles_data_mock(self):
        base_ts = 1710000000
        return [
            [base_ts + 0, "1.0", "1.2", "0.9", "1.1", "10", "100", "0", "0", "0"],
            [base_ts + 900, "1.1", "1.3", "1.0", "1.2", "11", "110", "0", "0", "0"],
            [base_ts + 1800, "1.2", "1.4", "1.1", "1.3", "12", "120", "0", "0", "0"],
            [base_ts + 2700, "1.3", "1.5", "1.2", "1.4", "13", "130", "0", "0", "0"],
            [base_ts + 3600, "1.4", "1.6", "1.3", "1.5", "14", "140", "0", "0", "0"],
        ]

    def get_candles_rest_data_mock(self):
        base_ts = 1710000000
        return [
            [int((base_ts + 0) * 1000), "1.0", "1.1", "1.2", "0.9", "100", "10"],
            [int((base_ts + 900) * 1000), "1.1", "1.2", "1.3", "1.0", "110", "11"],
            [int((base_ts + 1800) * 1000), "1.2", "1.3", "1.4", "1.1", "120", "12"],
            [int((base_ts + 2700) * 1000), "1.3", "1.4", "1.5", "1.2", "130", "13"],
            [int((base_ts + 3600) * 1000), "1.4", "1.5", "1.6", "1.3", "140", "14"],
        ]

    def get_candles_ws_data_mock_1(self):
        return {
            "push": {
                "channel": "market-data:last-candlestick-XRPUSD-15m",
                "pub": {"data": [1710004500000, "1.5", "1.6", "1.7", "1.4", "150", "15"], "offset": 1},
            }
        }

    def get_candles_ws_data_mock_2(self):
        return {
            "push": {
                "channel": "market-data:last-candlestick-XRPUSD-15m",
                "pub": {"data": [1710005400000, "1.6", "1.7", "1.8", "1.5", "160", "16"], "offset": 2},
            }
        }

    @staticmethod
    def _success_subscription_mock():
        return {"result": "ok"}

    @aioresponses()
    async def test_fetch_candles(self, mock_api):
        regex_url = re.compile(f"^{self.data_feed.candles_url}".replace(".", r"\.").replace("?", r"\?"))
        data_mock = self.get_candles_rest_data_mock()
        mock_api.get(url=regex_url, body=json.dumps(data_mock))

        resp = await self.data_feed.fetch_candles(start_time=int(self.start_time), end_time=int(self.end_time))

        self.assertEqual(resp.shape[0], len(self.get_fetch_candles_data_mock()))
        self.assertEqual(resp.shape[1], 10)

    @patch("hummingbot.data_feed.candles_feed.candles_base.CandlesBase._time")
    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    async def test_listen_for_subscriptions_subscribes_to_klines(self, ws_connect_mock, mock_time: AsyncMock):
        mock_time.return_value = 1710000000
        ws_connect_mock.return_value = self.mocking_assistant.create_websocket_mock()

        result_subscribe_klines = self._success_subscription_mock()
        self.mocking_assistant.add_websocket_aiohttp_message(
            websocket_mock=ws_connect_mock.return_value,
            message=json.dumps(result_subscribe_klines),
        )

        self.listening_task = asyncio.create_task(self.data_feed.listen_for_subscriptions())

        await self.mocking_assistant.run_until_all_aiohttp_messages_delivered(ws_connect_mock.return_value)
        await asyncio.sleep(0.1)

        sent_messages = self.mocking_assistant.json_messages_sent_through_websocket(
            websocket_mock=ws_connect_mock.return_value
        )
        # First message is Centrifugo connect, second is subscribe
        self.assertGreaterEqual(len(sent_messages), 2)
        self.assertIn("connect", sent_messages[0])

        expected_subscribe = self.data_feed.ws_subscription_payload()
        if "id" in expected_subscribe:
            del expected_subscribe["id"]
        if "id" in sent_messages[1]:
            del sent_messages[1]["id"]
        self.assertEqual(expected_subscribe, sent_messages[1])

        self.assertTrue(self.is_logged("INFO", "Subscribed to public klines..."))

    def test_normalize_timestamp_rounds_to_interval(self):
        ts_ms = 1710004500000 + 1234
        normalized = self.data_feed._normalize_timestamp(ts_ms)
        self.assertEqual(1710004500, normalized)
