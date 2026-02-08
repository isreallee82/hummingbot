"""Unit tests for Evedex API Order Book Data Source."""
import asyncio
import time
import unittest
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.exchange.evedex import evedex_constants as CONSTANTS, evedex_web_utils as web_utils
from hummingbot.connector.exchange.evedex.evedex_api_order_book_data_source import EvedexAPIOrderBookDataSource
from hummingbot.core.data_type.order_book_message import OrderBookMessageType


class TestEvedexAPIOrderBookDataSource(unittest.IsolatedAsyncioTestCase):
    """
    Test suite for EvedexAPIOrderBookDataSource based on official Evedex Swagger API.

    API Endpoints tested:
    - GET /api/market/{instrument}/deep - Order book snapshot
    - GET /api/market/instrument - Trading pair info

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
        cls.ex_trading_pair = f"{cls.base_asset}-{cls.quote_asset}"  # Evedex format

    def setUp(self):
        super().setUp()
        self.log_records = []
        self.listening_task: Optional[asyncio.Task] = None
        self.mocking_assistant = None

        self.connector = MagicMock()
        self.connector._domain = CONSTANTS.DEFAULT_DOMAIN
        self.connector.exchange_symbol_associated_to_pair = AsyncMock(return_value=self.ex_trading_pair)
        self.connector.trading_pair_associated_to_exchange_symbol = AsyncMock(return_value=self.trading_pair)
        self.connector._api_get = AsyncMock()

        self.api_factory = MagicMock()
        self.rest_assistant = AsyncMock()
        self.api_factory.get_rest_assistant = AsyncMock(return_value=self.rest_assistant)

        self.ws_assistant = MagicMock()
        self.ws_assistant.connect = AsyncMock()
        self.ws_assistant.send = AsyncMock()
        self.ws_assistant.disconnect = AsyncMock()
        self.api_factory.get_ws_assistant = AsyncMock(return_value=self.ws_assistant)

        self.data_source = EvedexAPIOrderBookDataSource(
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=self.api_factory,
            domain=CONSTANTS.DEFAULT_DOMAIN
        )

    def tearDown(self):
        if self.listening_task is not None:
            self.listening_task.cancel()
        super().tearDown()

    def handle_msg(self, msg):
        self.log_records.append(msg)

    def _order_book_snapshot_response(self) -> Dict:
        """
        Mock response for GET /api/market/{instrument}/deep based on Swagger API.
        Returns MarketDepth schema: { bids: MarketDepthEntry[], asks: MarketDepthEntry[] }
        MarketDepthEntry: { price: number, quantity: number, orders: number }
        """
        return {
            "bids": [
                {"price": 49900.0, "quantity": 1.5, "orders": 3},
                {"price": 49800.0, "quantity": 2.0, "orders": 5},
                {"price": 49700.0, "quantity": 3.5, "orders": 8}
            ],
            "asks": [
                {"price": 50100.0, "quantity": 1.2, "orders": 2},
                {"price": 50200.0, "quantity": 2.5, "orders": 4},
                {"price": 50300.0, "quantity": 3.0, "orders": 6}
            ]
        }

    def _order_book_ws_update(self) -> Dict:
        """
        Mock WebSocket message from spot:orderBook-{instrument}-0.1 channel.
        Centrifugo push format with namespace prefix.
        """
        return {
            "push": {
                "channel": f"spot:orderBook-{self.ex_trading_pair}-0.1",
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
        """
        Mock WebSocket message from spot:recent-trade-{instrument} channel.
        Centrifugo push format with namespace prefix.
        """
        return {
            "push": {
                "channel": f"spot:recent-trade-{self.ex_trading_pair}",
                "pub": {
                    "data": {
                        "instrument": self.ex_trading_pair,
                        "side": "BUY",
                        "fillPrice": 50000.0,
                        "fillQuantity": 0.5,
                        "executionId": "trade_123456",
                        "orderId": "order_789"
                    }
                }
            }
        }

    def _instruments_response(self) -> List:
        """Mock response for GET /api/market/instrument based on Swagger API."""
        return [
            {
                "id": "1",
                "name": self.ex_trading_pair,
                "displayName": f"{self.base_asset}/{self.quote_asset}",
                "from": {
                    "id": "1",
                    "name": self.base_asset,
                    "symbol": self.base_asset,
                    "precision": 8
                },
                "to": {
                    "id": "2",
                    "name": self.quote_asset,
                    "symbol": self.quote_asset,
                    "precision": 8
                },
                "lastPrice": 50000.0,
                "lotSize": 0.001,
                "priceIncrement": 0.01,
                "quantityIncrement": 0.001,
                "minQuantity": 0.001,
                "maxQuantity": 10000.0,
                "trading": "all",
                "marketState": "OPEN"
            }
        ]

    async def test_get_last_traded_prices(self):
        """Test fetching last traded prices from /api/market/instrument."""
        self.connector._api_get.return_value = self._instruments_response()

        result = await self.data_source.get_last_traded_prices([self.trading_pair])

        self.assertIn(self.trading_pair, result)
        self.assertEqual(result[self.trading_pair], 50000.0)

    async def test_get_last_traded_prices_with_single_response(self):
        """Test fetching last traded price when API returns single object instead of list."""
        single_response = self._instruments_response()[0]
        self.connector._api_get.return_value = single_response

        result = await self.data_source.get_last_traded_prices([self.trading_pair])

        self.assertIn(self.trading_pair, result)
        self.assertEqual(result[self.trading_pair], 50000.0)

    async def test_order_book_snapshot(self):
        """Test order book snapshot fetching from /api/market/{instrument}/deep."""
        self.rest_assistant.execute_request = AsyncMock(return_value=self._order_book_snapshot_response())

        snapshot_msg = await self.data_source._order_book_snapshot(self.trading_pair)

        self.assertEqual(snapshot_msg.type, OrderBookMessageType.SNAPSHOT)
        self.assertEqual(snapshot_msg.content["trading_pair"], self.trading_pair)
        self.assertEqual(len(snapshot_msg.content["bids"]), 3)
        self.assertEqual(len(snapshot_msg.content["asks"]), 3)
        self.assertEqual(snapshot_msg.content["bids"][0][0], 49900.0)  # Price
        self.assertEqual(snapshot_msg.content["bids"][0][1], 1.5)     # Quantity
        self.assertEqual(snapshot_msg.content["asks"][0][0], 50100.0)
        self.assertEqual(snapshot_msg.content["asks"][0][1], 1.2)

    async def test_request_order_book_snapshot(self):
        """Test raw order book snapshot request."""
        expected_response = self._order_book_snapshot_response()
        self.rest_assistant.execute_request = AsyncMock(return_value=expected_response)

        response = await self.data_source._request_order_book_snapshot(self.trading_pair)

        self.assertEqual(response, expected_response)
        self.connector.exchange_symbol_associated_to_pair.assert_called_once_with(trading_pair=self.trading_pair)

    async def test_connected_websocket_assistant(self):
        """Test WebSocket connection establishment."""
        await self.data_source._connected_websocket_assistant()

        self.ws_assistant.connect.assert_called_once()
        # self.assertIn("ws_url", call_kwargs)
        # self.assertIn("ping_timeout", call_kwargs)

    async def test_connected_websocket_assistant_cancels_ping_task(self):
        ping_task = asyncio.create_task(asyncio.sleep(10))
        self.data_source._ping_task = ping_task
        await self.data_source._connected_websocket_assistant()
        self.assertTrue(ping_task.cancelled() or ping_task.done())

    async def test_ping_loop_sends_and_cancels(self):
        sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError])
        with patch("asyncio.sleep", sleep_mock):
            with self.assertRaises(asyncio.CancelledError):
                await self.data_source._ping_loop(self.ws_assistant)
        self.ws_assistant.send.assert_called()

    async def test_ping_loop_handles_exception(self):
        sleep_mock = AsyncMock(side_effect=Exception("boom"))
        with patch("asyncio.sleep", sleep_mock):
            await self.data_source._ping_loop(self.ws_assistant)

    async def test_subscribe_channels(self):
        """Test subscription to Centrifugo channels: spot:orderBook-{instrument}-0.1 and spot:recent-trade-{instrument}."""
        await self.data_source._subscribe_channels(self.ws_assistant)

        # Should have sent connect message + 2 subscription messages per trading pair
        # Connect message is sent first, then subscriptions
        self.assertGreaterEqual(self.ws_assistant.send.call_count, 2)

    async def test_subscribe_channels_exception(self):
        self.ws_assistant.send = AsyncMock(side_effect=Exception("boom"))
        with self.assertRaises(Exception):
            await self.data_source._subscribe_channels(self.ws_assistant)

    async def test_on_order_stream_interruption_cancels_ping_task(self):
        ping_task = asyncio.create_task(asyncio.sleep(10))
        self.data_source._ping_task = ping_task
        await self.data_source._on_order_stream_interruption(self.ws_assistant)
        self.assertTrue(ping_task.cancelled() or ping_task.done())
        self.assertIsNone(self.data_source._ws_assistant)

    async def test_get_last_traded_prices_handles_error(self):
        self.connector._api_get = AsyncMock(side_effect=Exception("boom"))
        result = await self.data_source.get_last_traded_prices([self.trading_pair])
        self.assertEqual(result, {})

    async def test_subscribe_to_trading_pair_without_ws(self):
        self.data_source._ws_assistant = None
        result = await self.data_source.subscribe_to_trading_pair(self.trading_pair)
        self.assertFalse(result)

    async def test_subscribe_to_trading_pair_success(self):
        self.data_source._ws_assistant = self.ws_assistant
        result = await self.data_source.subscribe_to_trading_pair(self.trading_pair)

        self.assertTrue(result)
        self.assertIn(self.trading_pair, self.data_source._trading_pairs)
        self.assertGreaterEqual(self.ws_assistant.send.call_count, 2)

    async def test_subscribe_to_trading_pair_exception(self):
        self.data_source._ws_assistant = self.ws_assistant
        self.ws_assistant.send = AsyncMock(side_effect=Exception("boom"))
        result = await self.data_source.subscribe_to_trading_pair(self.trading_pair)
        self.assertFalse(result)

    async def test_unsubscribe_from_trading_pair_without_ws(self):
        self.data_source._ws_assistant = None
        result = await self.data_source.unsubscribe_from_trading_pair(self.trading_pair)
        self.assertFalse(result)

    async def test_unsubscribe_from_trading_pair_success(self):
        self.data_source._ws_assistant = self.ws_assistant
        self.data_source.add_trading_pair(self.trading_pair)
        result = await self.data_source.unsubscribe_from_trading_pair(self.trading_pair)

        self.assertTrue(result)
        self.assertNotIn(self.trading_pair, self.data_source._trading_pairs)
        self.assertGreaterEqual(self.ws_assistant.send.call_count, 2)

    async def test_unsubscribe_from_trading_pair_exception(self):
        self.data_source._ws_assistant = self.ws_assistant
        self.ws_assistant.send = AsyncMock(side_effect=Exception("boom"))
        result = await self.data_source.unsubscribe_from_trading_pair(self.trading_pair)
        self.assertFalse(result)

    async def test_process_message_for_unknown_channel_pong(self):
        ws = MagicMock()
        ws.send = AsyncMock()
        await self.data_source._process_message_for_unknown_channel({"ping": {}}, ws)
        ws.send.assert_called()

    async def test_process_message_for_unknown_channel_empty(self):
        ws = MagicMock()
        ws.send = AsyncMock()
        await self.data_source._process_message_for_unknown_channel({}, ws)
        ws.send.assert_called()

    def test_channel_originating_message_orderbook(self):
        event_message = {"push": {"channel": "spot:orderBook-BTC-USDT-0.1", "pub": {"data": {}}}}
        channel = self.data_source._channel_originating_message(event_message)
        self.assertEqual(channel, self.data_source._diff_messages_queue_key)

    def test_channel_originating_message_trade(self):
        event_message = {"push": {"channel": "spot:recent-trade-BTC-USDT", "pub": {"data": {}}}}
        channel = self.data_source._channel_originating_message(event_message)
        self.assertEqual(channel, self.data_source._trade_messages_queue_key)

    async def test_parse_order_book_diff_message(self):
        """Test parsing order book diff message from spot:orderBook-{instrument}-0.1 channel (Centrifugo push format)."""
        raw_message = self._order_book_ws_update()
        message_queue = asyncio.Queue()

        await self.data_source._parse_order_book_diff_message(raw_message, message_queue)

        self.assertEqual(message_queue.qsize(), 1)
        diff_msg = message_queue.get_nowait()
        self.assertEqual(diff_msg.type, OrderBookMessageType.DIFF)
        self.assertEqual(diff_msg.content["trading_pair"], self.trading_pair)
        self.assertEqual(len(diff_msg.content["bids"]), 2)
        self.assertEqual(len(diff_msg.content["asks"]), 2)
        self.assertEqual(diff_msg.content["bids"][0][0], 49950.0)
        self.assertEqual(diff_msg.content["bids"][0][1], 1.0)

    async def test_parse_order_book_diff_message_with_list_entries(self):
        raw_message = self._order_book_ws_update()
        raw_message["push"]["pub"]["data"]["orderBook"]["bids"] = [[49950.0, 1.0]]
        raw_message["push"]["pub"]["data"]["orderBook"]["asks"] = [[50050.0, 0.8]]
        message_queue = asyncio.Queue()

        await self.data_source._parse_order_book_diff_message(raw_message, message_queue)

        self.assertEqual(message_queue.qsize(), 1)

    async def test_parse_order_book_diff_message_unknown_symbol(self):
        """Test parsing order book message with unknown trading pair symbol."""
        raw_message = self._order_book_ws_update()
        self.connector.trading_pair_associated_to_exchange_symbol.side_effect = KeyError("Unknown symbol")
        message_queue = asyncio.Queue()

        await self.data_source._parse_order_book_diff_message(raw_message, message_queue)

        # Should not add message to queue for unknown symbol
        self.assertEqual(message_queue.qsize(), 0)

    async def test_parse_trade_message(self):
        """Test parsing trade message from spot:recent-trade-{instrument} channel (Centrifugo push format)."""
        raw_message = self._trade_ws_message()
        message_queue = asyncio.Queue()

        await self.data_source._parse_trade_message(raw_message, message_queue)

        self.assertEqual(message_queue.qsize(), 1)
        trade_msg = message_queue.get_nowait()
        self.assertEqual(trade_msg.type, OrderBookMessageType.TRADE)
        self.assertEqual(trade_msg.content["trading_pair"], self.trading_pair)
        self.assertEqual(trade_msg.content["price"], 50000.0)
        self.assertEqual(trade_msg.content["amount"], 0.5)
        self.assertEqual(trade_msg.content["trade_id"], "trade_123456")

    async def test_parse_trade_message_sell_side(self):
        """Test parsing trade message with SELL side (Centrifugo push format)."""
        raw_message = self._trade_ws_message()
        raw_message["push"]["pub"]["data"]["side"] = "SELL"
        message_queue = asyncio.Queue()

        await self.data_source._parse_trade_message(raw_message, message_queue)

        self.assertEqual(message_queue.qsize(), 1)
        trade_msg = message_queue.get_nowait()
        # SELL side should result in TradeType.SELL value
        from hummingbot.core.data_type.common import TradeType
        self.assertEqual(trade_msg.content["trade_type"], float(TradeType.SELL.value))

    async def test_parse_trade_message_unknown_symbol(self):
        """Test parsing trade message with unknown trading pair symbol."""
        raw_message = self._trade_ws_message()
        self.connector.trading_pair_associated_to_exchange_symbol.side_effect = KeyError("Unknown symbol")
        message_queue = asyncio.Queue()

        await self.data_source._parse_trade_message(raw_message, message_queue)

        # Should not add message to queue for unknown symbol
        self.assertEqual(message_queue.qsize(), 0)

    async def test_parse_trade_message_with_list_data(self):
        """Test parsing trade message when data is a list of trades (Centrifugo push format)."""
        raw_message = {
            "push": {
                "channel": f"spot:recent-trade-{self.ex_trading_pair}",
                "pub": {
                    "data": [
                        {
                            "instrument": self.ex_trading_pair,
                            "side": "BUY",
                            "fillPrice": 50000.0,
                            "fillQuantity": 0.5,
                            "executionId": "trade_1"
                        },
                        {
                            "instrument": self.ex_trading_pair,
                            "side": "SELL",
                            "fillPrice": 50010.0,
                            "fillQuantity": 0.3,
                            "executionId": "trade_2"
                        }
                    ]
                }
            }
        }
        message_queue = asyncio.Queue()

        await self.data_source._parse_trade_message(raw_message, message_queue)

        self.assertEqual(message_queue.qsize(), 2)

    def test_channel_originating_message_unknown(self):
        """Test channel detection for unknown channels (Centrifugo push format)."""
        message = {"push": {"channel": "unknown-channel", "pub": {"data": {}}}}
        result = self.data_source._channel_originating_message(message)
        self.assertEqual(result, "")

    async def test_order_book_snapshot_with_array_format(self):
        """Test order book snapshot with array format [price, quantity] instead of objects."""
        array_response = {
            "bids": [[49900.0, 1.5], [49800.0, 2.0]],
            "asks": [[50100.0, 1.2], [50200.0, 2.5]]
        }
        self.rest_assistant.execute_request = AsyncMock(return_value=array_response)

        snapshot_msg = await self.data_source._order_book_snapshot(self.trading_pair)

        self.assertEqual(snapshot_msg.type, OrderBookMessageType.SNAPSHOT)
        self.assertEqual(len(snapshot_msg.content["bids"]), 2)
        self.assertEqual(snapshot_msg.content["bids"][0][0], 49900.0)
        self.assertEqual(snapshot_msg.content["bids"][0][1], 1.5)


class TestEvedexAPIOrderBookDataSourceURLs(unittest.TestCase):
    """Test URL generation for order book data source."""

    def test_order_book_path_url_format(self):
        """Test that order book path URL uses correct format."""
        instrument = "BTC-USDT"
        path_url = CONSTANTS.ORDER_BOOK_PATH_URL.format(instrument=instrument)
        self.assertEqual(path_url, f"/api/market/{instrument}/deep")

    def test_public_rest_url(self):
        """Test public REST URL generation."""
        url = web_utils.public_rest_url(CONSTANTS.INSTRUMENTS_PATH_URL)
        self.assertIn(CONSTANTS.REST_URL, url)
        self.assertIn(CONSTANTS.INSTRUMENTS_PATH_URL, url)

    def test_wss_url(self):
        """Test WebSocket URL generation."""
        ws_url = web_utils.wss_url()
        self.assertEqual(ws_url, CONSTANTS.WSS_URL)


if __name__ == "__main__":
    unittest.main()
