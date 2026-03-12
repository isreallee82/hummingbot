import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from hummingbot.connector.derivative.grvt_perpetual import grvt_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_api_order_book_data_source import (
    GrvtPerpetualAPIOrderBookDataSource,
)


class _MockConnector:
    def __init__(self):
        self._api_get = AsyncMock(return_value={"result": {}})

    async def exchange_symbol_associated_to_pair(self, trading_pair: str) -> str:
        return "BTC_USDT_Perp"

    async def trading_pair_associated_to_exchange_symbol(self, symbol: str) -> str:
        return "BTC-USDT"

    async def get_last_traded_prices(self, trading_pairs):
        return {tp: 1.0 for tp in trading_pairs}


class GrvtPerpetualAPIOrderBookDataSourceTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.fixtures_dir = Path(__file__).resolve().parents[5] / "grvt" / "fixtures"
        self.connector = _MockConnector()
        self.api_factory = MagicMock()
        self.data_source = GrvtPerpetualAPIOrderBookDataSource(
            trading_pairs=["BTC-USDT"],
            connector=self.connector,
            api_factory=self.api_factory,
        )

    def _load_fixture(self, filename: str):
        with open(self.fixtures_dir / filename, "r") as f:
            return json.load(f)

    def test_channel_originating_message(self):
        diff_message = self._load_fixture("orderbook_delta_ws.json")
        self.assertEqual(
            CONSTANTS.WS_PUBLIC_ORDERBOOK_DIFF_STREAM,
            self.data_source._channel_originating_message(diff_message),
        )

        ticker_message = self._load_fixture("ticker_ws.json")
        self.assertEqual(
            CONSTANTS.WS_PUBLIC_TICKERS_STREAM,
            self.data_source._channel_originating_message(ticker_message),
        )

    async def test_parse_diff_message(self):
        message_queue = asyncio.Queue()
        diff_message = self._load_fixture("orderbook_delta_ws.json")

        await self.data_source._parse_order_book_diff_message(diff_message, message_queue)
        parsed = await message_queue.get()

        self.assertEqual("BTC-USDT", parsed.content["trading_pair"])
        self.assertEqual(123457, parsed.content["update_id"])

    async def test_parse_trade_message(self):
        message_queue = asyncio.Queue()
        trade_message = self._load_fixture("trades_ws.json")

        await self.data_source._parse_trade_message(trade_message, message_queue)
        parsed = await message_queue.get()

        self.assertEqual("BTC-USDT", parsed.content["trading_pair"])
        self.assertEqual("60001", parsed.content["price"])
        self.assertEqual("0.004", parsed.content["amount"])

    async def test_parse_funding_info_message(self):
        message_queue = asyncio.Queue()
        ticker_message = self._load_fixture("ticker_ws.json")

        await self.data_source._parse_funding_info_message(ticker_message, message_queue)
        parsed = await message_queue.get()

        self.assertEqual("BTC-USDT", parsed.trading_pair)
        self.assertEqual(0, parsed.next_funding_utc_timestamp)

    async def test_subscribe_to_trading_pair_sends_messages(self):
        ws_assistant = AsyncMock()
        self.data_source._ws_assistant = ws_assistant

        success = await self.data_source.subscribe_to_trading_pair("BTC-USDT")

        self.assertTrue(success)
        self.assertEqual(4, ws_assistant.send.call_count)

    async def test_request_order_book_snapshot_retries_on_invalid_depth(self):
        expected_payload = self._load_fixture("orderbook_snapshot.json")
        self.connector._api_get.side_effect = [
            IOError('Error executing request GET /v1/book. HTTP status is 400. Error: {"code":3031,"message":"Depth is invalid","status":400}'),
            expected_payload,
        ]

        snapshot = await self.data_source._request_order_book_snapshot("BTC-USDT")

        self.assertEqual(expected_payload, snapshot)
        self.assertEqual(2, self.connector._api_get.call_count)
        first_call = self.connector._api_get.call_args_list[0]
        second_call = self.connector._api_get.call_args_list[1]
        self.assertEqual(50, first_call.kwargs["params"]["depth"])
        self.assertEqual(10, second_call.kwargs["params"]["depth"])
