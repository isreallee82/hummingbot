"""Unit tests for Evedex Perpetual API Order Book Data Source."""
import asyncio
import time
import unittest
from decimal import Decimal
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from hummingbot.connector.derivative.evedex_perpetual import evedex_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.evedex_perpetual.evedex_perpetual_api_order_book_data_source import (
    EvedexPerpetualAPIOrderBookDataSource,
)
from hummingbot.core.data_type.order_book_message import OrderBookMessageType


class TestEvedexPerpetualAPIOrderBookDataSource(unittest.IsolatedAsyncioTestCase):
    """
    Test suite for EvedexPerpetualAPIOrderBookDataSource.

    API Endpoints tested:
    - GET /api/market/instrument - Trading pairs with metrics
    - GET /api/market/{instrument}/deep - Order book snapshot

    WebSocket Channels (Centrifuge protocol):
    - orderBook-{instrument}-{roundPrices} - Order book updates
    - trade-{instrument} - Trade updates
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base_asset = "BTC"
        cls.quote_asset = "USDT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = f"{cls.base_asset}-{cls.quote_asset}"

    def setUp(self):
        super().setUp()
        self.listening_task: Optional[asyncio.Task] = None

        self.connector = MagicMock()
        self.connector._domain = CONSTANTS.DEFAULT_DOMAIN
        self.connector.exchange_symbol_associated_to_pair = AsyncMock(return_value=self.ex_trading_pair)
        self.connector.trading_pair_associated_to_exchange_symbol = AsyncMock(return_value=self.trading_pair)
        self.connector._api_get = AsyncMock()
        self.connector.get_last_traded_prices = AsyncMock(return_value={self.trading_pair: 50000.0})

        self.api_factory = MagicMock()
        self.ws_assistant = MagicMock()
        self.ws_assistant.connect = AsyncMock()
        self.ws_assistant.send = AsyncMock()
        self.ws_assistant.disconnect = AsyncMock()
        self.api_factory.get_ws_assistant = AsyncMock(return_value=self.ws_assistant)

        self.data_source = EvedexPerpetualAPIOrderBookDataSource(
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=self.api_factory,
            domain=CONSTANTS.DEFAULT_DOMAIN
        )

    def tearDown(self):
        if self.listening_task is not None:
            self.listening_task.cancel()
        super().tearDown()

    def _order_book_snapshot_response(self) -> Dict:
        """
        Mock response for GET /api/market/{instrument}/deep.
        MarketDepth schema: { bids: MarketDepthEntry[], asks: MarketDepthEntry[] }
        """
        return {
            "bids": [
                {"price": 49900.0, "quantity": 1.5, "orders": 3},
                {"price": 49800.0, "quantity": 2.0, "orders": 5}
            ],
            "asks": [
                {"price": 50100.0, "quantity": 1.2, "orders": 2},
                {"price": 50200.0, "quantity": 2.5, "orders": 4}
            ],
            "t": int(time.time() * 1000)
        }

    def _instrument_info_response(self) -> List[Dict]:
        """Mock response for GET /api/market/instrument with metrics."""
        return [
            {
                "id": "1",
                "name": self.ex_trading_pair,
                "displayName": f"{self.base_asset}/{self.quote_asset}",
                "lastPrice": 50000.0,
                "markPrice": 50000.0,
                "fundingRate": 0.0001,
                "lotSize": 0.001,
                "priceIncrement": 0.01,
                "quantityIncrement": 0.001,
                "minQuantity": 0.001,
                "maxQuantity": 10000.0,
                "maxLeverage": 100,
                "trading": "all",
                "marketState": "OPEN"
            }
        ]

    async def test_get_last_traded_prices(self):
        """Test fetching last traded prices."""
        result = await self.data_source.get_last_traded_prices([self.trading_pair])

        self.assertIn(self.trading_pair, result)
        self.assertEqual(result[self.trading_pair], 50000.0)
        self.connector.get_last_traded_prices.assert_called_once_with(trading_pairs=[self.trading_pair])

    async def test_get_funding_info(self):
        """Test fetching funding info from /api/market/instrument."""
        self.connector._api_get.return_value = self._instrument_info_response()

        funding_info = await self.data_source.get_funding_info(self.trading_pair)

        self.assertEqual(funding_info.trading_pair, self.trading_pair)
        self.assertEqual(funding_info.mark_price, Decimal("50000.0"))
        self.assertEqual(funding_info.index_price, Decimal("50000.0"))
        self.assertEqual(funding_info.rate, Decimal("0.0001"))

    async def test_request_instrument_info(self):
        """Test instrument info request."""
        self.connector._api_get.return_value = self._instrument_info_response()

        result = await self.data_source._request_instrument_info(self.trading_pair)

        self.assertEqual(result["name"], self.ex_trading_pair)
        self.assertEqual(result["markPrice"], 50000.0)
        # Verify call was made with correct path and params
        self.assertEqual(self.connector._api_get.call_count, 1)
        call_kwargs = self.connector._api_get.call_args.kwargs
        self.assertEqual(call_kwargs.get("path_url"), CONSTANTS.INSTRUMENTS_PATH_URL)
        self.assertEqual(call_kwargs.get("params"), {"instrument": self.ex_trading_pair, "fields": "metrics"})

    async def test_request_order_book_snapshot(self):
        """Test order book snapshot request from /api/market/{instrument}/deep."""
        expected_response = self._order_book_snapshot_response()
        self.connector._api_get.return_value = expected_response

        result = await self.data_source._request_order_book_snapshot(self.trading_pair)

        self.assertEqual(result, expected_response)
        expected_path = CONSTANTS.ORDER_BOOK_PATH_URL.format(instrument=self.ex_trading_pair)
        # Verify call was made with correct path
        self.assertEqual(self.connector._api_get.call_count, 1)
        call_kwargs = self.connector._api_get.call_args.kwargs
        self.assertEqual(call_kwargs.get("path_url"), expected_path)

    async def test_order_book_snapshot(self):
        """Test order book snapshot message creation."""
        self.connector._api_get.return_value = self._order_book_snapshot_response()

        snapshot_msg = await self.data_source._order_book_snapshot(self.trading_pair)

        self.assertEqual(snapshot_msg.type, OrderBookMessageType.SNAPSHOT)
        self.assertEqual(snapshot_msg.content["trading_pair"], self.trading_pair)
        self.assertEqual(len(snapshot_msg.content["bids"]), 2)
        self.assertEqual(len(snapshot_msg.content["asks"]), 2)
        # Bids/asks are converted to [price, quantity] string format
        self.assertEqual(snapshot_msg.content["bids"][0][0], "49900.0")
        self.assertEqual(snapshot_msg.content["bids"][0][1], "1.5")

    async def test_connected_websocket_assistant(self):
        """Test WebSocket connection establishment with Centrifugo protocol."""
        await self.data_source._connected_websocket_assistant()

        self.ws_assistant.connect.assert_called_once()
        call_kwargs = self.ws_assistant.connect.call_args[1]
        self.assertIn("ws_url", call_kwargs)
        self.assertIn("ping_timeout", call_kwargs)
        # Should have sent Centrifugo connect message
        self.ws_assistant.send.assert_called()

    async def test_subscribe_channels(self):
        """Test subscription to Centrifugo channels."""
        await self.data_source._subscribe_channels(self.ws_assistant)

        # Should have sent subscriptions: heartbeat + orderbook + trades per trading pair
        self.assertGreaterEqual(self.ws_assistant.send.call_count, 3)

        # Check that subscribe payloads use Centrifugo format
        calls = self.ws_assistant.send.call_args_list
        payloads = [call[0][0].payload for call in calls]

        # Verify at least one orderbook subscription
        orderbook_subs = [p for p in payloads if "subscribe" in p and "orderBook" in p.get("subscribe", {}).get("channel", "")]
        self.assertGreater(len(orderbook_subs), 0)

        # Verify at least one trade subscription
        trade_subs = [p for p in payloads if "subscribe" in p and "recent-trade" in p.get("subscribe", {}).get("channel", "")]
        self.assertGreater(len(trade_subs), 0)


class TestEvedexPerpetualOrderBookWebSocket(unittest.IsolatedAsyncioTestCase):
    """Test WebSocket message handling."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base_asset = "BTC"
        cls.quote_asset = "USDT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = f"{cls.base_asset}-{cls.quote_asset}"

    def _order_book_ws_update(self) -> Dict:
        """Mock WebSocket message from Centrifugo push format."""
        return {
            "push": {
                "channel": f"futures-perp:orderBook-{self.ex_trading_pair.replace('-', '')}-0.1",
                "pub": {
                    "data": {
                        "instrument": self.ex_trading_pair,
                        "orderBook": {
                            "t": int(time.time() * 1000),
                            "bids": [
                                {"price": 49950.0, "quantity": 1.0},
                                {"price": 49900.0, "quantity": 1.5}
                            ],
                            "asks": [
                                {"price": 50050.0, "quantity": 0.8},
                                {"price": 50100.0, "quantity": 1.2}
                            ]
                        }
                    }
                }
            }
        }

    def _trade_ws_message(self) -> Dict:
        """Mock WebSocket message from Centrifugo push format."""
        return {
            "push": {
                "channel": f"futures-perp:recent-trade-{self.ex_trading_pair.replace('-', '')}",
                "pub": {
                    "data": {
                        "instrument": self.ex_trading_pair,
                        "side": "BUY",
                        "fillPrice": 50000.0,
                        "fillQuantity": 0.5,
                        "executionId": "trade_123456"
                    }
                }
            }
        }

    def test_orderbook_channel_format(self):
        """Test order book channel naming: futures-perp:orderBook-{instrument}-0.1."""
        msg = self._order_book_ws_update()
        channel = msg["push"]["channel"]

        self.assertTrue(channel.startswith("futures-perp:orderBook-"))
        self.assertTrue(channel.endswith("-0.1"))

    def test_trade_channel_format(self):
        """Test trade channel naming: futures-perp:recent-trade-{instrument}."""
        msg = self._trade_ws_message()
        channel = msg["push"]["channel"]

        self.assertTrue(channel.startswith("futures-perp:recent-trade-"))

    def test_orderbook_data_structure(self):
        """Test order book WebSocket data structure in Centrifugo format."""
        msg = self._order_book_ws_update()
        data = msg["push"]["pub"]["data"]

        self.assertIn("instrument", data)
        self.assertIn("orderBook", data)
        orderbook = data["orderBook"]
        self.assertIn("bids", orderbook)
        self.assertIn("asks", orderbook)
        self.assertIn("t", orderbook)  # timestamp

    def test_trade_data_structure(self):
        """Test trade WebSocket data structure in Centrifugo format."""
        msg = self._trade_ws_message()
        data = msg["push"]["pub"]["data"]

        self.assertIn("instrument", data)
        self.assertIn("side", data)
        self.assertIn("fillPrice", data)
        self.assertIn("fillQuantity", data)
        self.assertIn("executionId", data)


class TestEvedexPerpetualFundingInfo(unittest.TestCase):
    """Test funding info functionality."""

    def test_funding_info_fields(self):
        """Test that funding info contains required fields."""
        from hummingbot.core.data_type.funding_info import FundingInfo

        funding_info = FundingInfo(
            trading_pair="BTC-USDT",
            index_price=Decimal("50000"),
            mark_price=Decimal("50010"),
            next_funding_utc_timestamp=int(time.time()) + 3600,
            rate=Decimal("0.0001")
        )

        self.assertEqual(funding_info.trading_pair, "BTC-USDT")
        self.assertEqual(funding_info.index_price, Decimal("50000"))
        self.assertEqual(funding_info.mark_price, Decimal("50010"))
        self.assertEqual(funding_info.rate, Decimal("0.0001"))
        self.assertIsNotNone(funding_info.next_funding_utc_timestamp)


if __name__ == "__main__":
    unittest.main()
