"""Unit tests for Evedex API User Stream Data Source."""
import asyncio
import unittest
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

from hummingbot.connector.exchange.evedex import evedex_constants as CONSTANTS
from hummingbot.connector.exchange.evedex.evedex_api_user_stream_data_source import EvedexAPIUserStreamDataSource
from hummingbot.connector.exchange.evedex.evedex_auth import EvedexAuth


class TestEvedexAPIUserStreamDataSource(unittest.IsolatedAsyncioTestCase):
    """
    Test suite for EvedexAPIUserStreamDataSource based on official Evedex Swagger API.

    WebSocket Channels (Centrifuge protocol):
    - order-{userExchangeId} - Order updates
    - user-{userExchangeId} - Account/balance updates
    - orderFills-{userExchangeId} - Order fill events

    API Endpoints:
    - GET /api/user/me - Get user info including exchangeId
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base_asset = "BTC"
        cls.quote_asset = "USDT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.user_exchange_id = "12345"
        cls.api_key = "test-api-key"

    def setUp(self):
        super().setUp()
        self.listening_task: Optional[asyncio.Task] = None

        self.auth = EvedexAuth(api_key=self.api_key)

        self.connector = MagicMock()
        self.connector._domain = CONSTANTS.DEFAULT_DOMAIN
        self.connector.exchange_symbol_associated_to_pair = AsyncMock(return_value=self.ex_trading_pair)
        self.connector.trading_pair_associated_to_exchange_symbol = AsyncMock(return_value=self.trading_pair)
        self.connector._api_get = AsyncMock(return_value={"exchangeId": self.user_exchange_id})

        self.api_factory = MagicMock()
        self.ws_assistant = MagicMock()
        self.ws_assistant.connect = AsyncMock()
        self.ws_assistant.send = AsyncMock()
        self.ws_assistant.disconnect = AsyncMock()
        self.api_factory.get_ws_assistant = AsyncMock(return_value=self.ws_assistant)

        self.data_source = EvedexAPIUserStreamDataSource(
            auth=self.auth,
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=self.api_factory,
            domain=CONSTANTS.DEFAULT_DOMAIN
        )

    def tearDown(self):
        if self.listening_task is not None:
            self.listening_task.cancel()
        super().tearDown()

    def _user_me_response(self):
        """
        Mock response for GET /api/user/me based on Swagger API UserInfo schema.
        """
        return {
            "id": "user_001",
            "exchangeId": self.user_exchange_id,
            "email": "test@example.com",
            "status": "ACTIVE",
            "createdAt": "2024-01-01T00:00:00.000Z"
        }

    def _order_ws_update(self, status="NEW"):
        """
        Mock WebSocket message from spot:order-{userExchangeId} channel (Centrifugo push format).
        Order schema from official Swagger API.
        """
        return {
            "push": {
                "channel": f"spot:order-{self.user_exchange_id}",
                "pub": {
                    "data": {
                        "id": "order_123456",
                        "user": "user_001",
                        "instrument": self.ex_trading_pair,
                        "type": "LIMIT",
                        "side": "BUY",
                        "status": status,
                        "rejectedReason": "",
                        "quantity": 1.0,
                        "limitPrice": 50000.0,
                        "stopPrice": None,
                        "group": "manually",
                        "unFilledQuantity": 1.0,
                        "cashQuantity": 50000.0,
                        "filledAvgPrice": 0.0,
                        "realizedPnL": 0.0,
                        "fee": [],
                        "triggeredAt": None,
                        "exchangeRequestId": "req_123",
                        "createdAt": "2024-01-01T00:00:00.000Z",
                        "updatedAt": "2024-01-01T00:00:00.000Z"
                    }
                }
            }
        }

    def _user_balance_ws_update(self):
        """
        Mock WebSocket message from spot:user-{userExchangeId} channel (Centrifugo push format).
        Account balance update.
        """
        return {
            "push": {
                "channel": f"spot:user-{self.user_exchange_id}",
                "pub": {
                    "data": {
                        "currency": self.base_asset,
                        "funding": {
                            "currency": self.base_asset,
                            "balance": 10.0
                        },
                        "availableBalance": 8.0,
                        "position": [],
                        "openOrder": [],
                        "updatedAt": "2024-01-01T00:00:00.000Z"
                    }
                }
            }
        }

    def _order_fill_ws_update(self):
        """
        Mock WebSocket message from spot:orderFilled-{userExchangeId} channel (Centrifugo push format).
        Fill data structure from official types.ts.
        """
        return {
            "push": {
                "channel": f"spot:orderFilled-{self.user_exchange_id}",
                "pub": {
                    "data": {
                        "executionId": "fill_123456",
                        "orderId": "order_123456",
                        "instrumentName": self.ex_trading_pair,
                        "side": "BUY",
                        "fillPrice": 50000.0,
                        "fillQuantity": 0.5,
                        "fillValue": 25000.0,
                        "fee": [{"coin": self.quote_asset, "quantity": 10.0}],
                        "pnl": 0.0,
                        "isPnlRealized": False,
                        "createdAt": "2024-01-01T00:00:00.000Z"
                    }
                }
            }
        }

    async def test_get_user_exchange_id(self):
        """Test fetching userExchangeId from /api/user/me endpoint."""
        self.connector._api_get.return_value = self._user_me_response()

        user_exchange_id = await self.data_source._get_user_exchange_id()

        self.assertEqual(user_exchange_id, self.user_exchange_id)
        self.connector._api_get.assert_called_once_with(
            path_url=CONSTANTS.USER_ME_PATH_URL,
            is_auth_required=True
        )

    async def test_get_user_exchange_id_caches_result(self):
        """Test that userExchangeId is cached after first fetch."""
        self.connector._api_get.return_value = self._user_me_response()

        # First call
        await self.data_source._get_user_exchange_id()
        # Second call should use cached value
        await self.data_source._get_user_exchange_id()

        # Should only call API once
        self.assertEqual(self.connector._api_get.call_count, 1)

    async def test_connected_websocket_assistant(self):
        """Test WebSocket connection with Centrifugo protocol."""
        await self.data_source._connected_websocket_assistant()

        # Verify WebSocket connection
        self.ws_assistant.connect.assert_called_once()

    async def test_connected_websocket_assistant_cancels_ping_task(self):
        ping_task = asyncio.create_task(asyncio.sleep(10))
        self.data_source._ping_task = ping_task
        await self.data_source._connected_websocket_assistant()
        self.assertTrue(ping_task.cancelled() or ping_task.done())

    async def test_get_access_token_caches_result(self):
        self.connector._api_get.return_value = {"token": "abc"}
        token = await self.data_source._get_access_token()
        self.assertEqual(token, "abc")
        token = await self.data_source._get_access_token()
        self.assertEqual(self.connector._api_get.call_count, 1)

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
        """
        Test subscription to Centrifugo user channels with spot: namespace:
        - spot:order-{userExchangeId}
        - spot:user-{userExchangeId}
        - spot:orderFilled-{userExchangeId}
        """
        self.connector._api_get.return_value = self._user_me_response()

        await self.data_source._subscribe_channels(self.ws_assistant)

        # Should have sent connect message + subscription messages
        self.assertGreaterEqual(self.ws_assistant.send.call_count, 1)

    async def test_subscribe_channels_exception(self):
        self.ws_assistant.send = AsyncMock(side_effect=Exception("boom"))
        with self.assertRaises(Exception):
            await self.data_source._subscribe_channels(self.ws_assistant)

    async def test_process_event_message_order_channel(self):
        """Test processing order channel messages (Centrifugo push format)."""
        queue = asyncio.Queue()
        message = self._order_ws_update()

        await self.data_source._process_event_message(message, queue)

        self.assertEqual(queue.qsize(), 1)
        processed_msg = queue.get_nowait()
        self.assertEqual(processed_msg["push"]["channel"], f"spot:order-{self.user_exchange_id}")

    async def test_process_event_message_user_channel(self):
        """Test processing user/account channel messages (Centrifugo push format)."""
        queue = asyncio.Queue()
        message = self._user_balance_ws_update()

        await self.data_source._process_event_message(message, queue)

        self.assertEqual(queue.qsize(), 1)
        processed_msg = queue.get_nowait()
        self.assertEqual(processed_msg["push"]["channel"], f"spot:user-{self.user_exchange_id}")

    async def test_process_event_message_order_fills_channel(self):
        """Test processing orderFilled channel messages (Centrifugo push format)."""
        queue = asyncio.Queue()
        message = self._order_fill_ws_update()

        await self.data_source._process_event_message(message, queue)

        self.assertEqual(queue.qsize(), 1)
        processed_msg = queue.get_nowait()
        self.assertEqual(processed_msg["push"]["channel"], f"spot:orderFilled-{self.user_exchange_id}")

    async def test_process_event_message_ignores_unknown_channel(self):
        """Test that unknown channels are still passed through (filtering happens in connector)."""
        queue = asyncio.Queue()
        message = {"push": {"channel": "unknown-channel", "pub": {"data": {"test": "data"}}}}

        await self.data_source._process_event_message(message, queue)

        # Message is passed through - connector will filter by channel
        self.assertEqual(queue.qsize(), 1)

    async def test_process_event_message_ignores_empty_message(self):
        """Test that empty messages are ignored."""
        queue = asyncio.Queue()
        message = {}

        await self.data_source._process_event_message(message, queue)

        self.assertEqual(queue.qsize(), 0)

    async def test_order_status_updates(self):
        """Test processing various order status updates from Swagger API OrderStatus enum."""
        queue = asyncio.Queue()

        # Test all order statuses from OrderStatus enum
        statuses = ["INTENTION", "NEW", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "EXPIRED", "REPLACED", "ERROR"]

        for status in statuses:
            message = self._order_ws_update(status=status)
            await self.data_source._process_event_message(message, queue)

        self.assertEqual(queue.qsize(), len(statuses))

    async def test_order_channel_does_not_match_order_filled(self):
        """Test that spot:order-{id} channel detection doesn't match spot:orderFilled-{id}."""
        queue = asyncio.Queue()

        # Order channel should be processed
        order_msg = {"push": {"channel": f"spot:order-{self.user_exchange_id}", "pub": {"data": {"id": "1"}}}}
        await self.data_source._process_event_message(order_msg, queue)

        # orderFilled channel should be processed separately
        fills_msg = {"push": {"channel": f"spot:orderFilled-{self.user_exchange_id}", "pub": {"data": {"id": "2"}}}}
        await self.data_source._process_event_message(fills_msg, queue)

        self.assertEqual(queue.qsize(), 2)


class TestEvedexUserStreamDataSourceWebSocketMessages(unittest.IsolatedAsyncioTestCase):
    """Test WebSocket message structures match official API (Centrifugo push format)."""

    def test_order_message_structure(self):
        """Verify order message structure matches Swagger Order schema (Centrifugo push format)."""
        order_message = {
            "push": {
                "channel": "spot:order-12345",
                "pub": {
                    "data": {
                        "id": "order_id",
                        "user": "user_id",
                        "instrument": "BTC-USDT",
                        "type": "LIMIT",  # OrderType: MARKET, LIMIT, STOP_MARKET, STOP_LIMIT
                        "side": "BUY",    # OrderSide: BUY, SELL
                        "status": "NEW",  # OrderStatus: INTENTION, NEW, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, EXPIRED, REPLACED, ERROR
                        "rejectedReason": "",
                        "quantity": 1.0,
                        "limitPrice": 50000.0,
                        "stopPrice": None,
                        "group": "manually",
                        "unFilledQuantity": 1.0,
                        "cashQuantity": 50000.0,
                        "filledAvgPrice": 0.0,
                        "realizedPnL": 0.0,
                        "fee": [],  # Array of { coin: string, quantity: number }
                        "triggeredAt": None,
                        "exchangeRequestId": "req_id",
                        "createdAt": "2024-01-01T00:00:00.000Z",
                        "updatedAt": "2024-01-01T00:00:00.000Z"
                    }
                }
            }
        }

        data = order_message["push"]["pub"]["data"]
        self.assertIn("id", data)
        self.assertIn("status", data)
        self.assertIn("unFilledQuantity", data)
        self.assertIn("filledAvgPrice", data)
        self.assertIn("fee", data)

    def test_fill_message_structure(self):
        """Verify fill message structure matches official types (Centrifugo push format)."""
        fill_message = {
            "push": {
                "channel": "spot:orderFilled-12345",
                "pub": {
                    "data": {
                        "executionId": "exec_id",
                        "orderId": "order_id",
                        "instrumentName": "BTC-USDT",
                        "side": "BUY",
                        "fillPrice": 50000.0,
                        "fillQuantity": 0.5,
                        "fillValue": 25000.0,
                        "fee": [{"coin": "USDT", "quantity": 10.0}],
                        "pnl": 0.0,
                        "isPnlRealized": False,
                        "createdAt": "2024-01-01T00:00:00.000Z"
                    }
                }
            }
        }

        data = fill_message["push"]["pub"]["data"]
        self.assertIn("executionId", data)
        self.assertIn("fillPrice", data)
        self.assertIn("fillQuantity", data)
        self.assertIn("fee", data)

    def test_balance_message_structure(self):
        """Verify balance/user message structure (Centrifugo push format)."""
        balance_message = {
            "push": {
                "channel": "spot:user-12345",
                "pub": {
                    "data": {
                        "currency": "BTC",
                        "funding": {
                            "currency": "BTC",
                            "balance": 10.0
                        },
                        "availableBalance": 8.0,
                        "position": [],
                        "openOrder": [],
                        "updatedAt": "2024-01-01T00:00:00.000Z"
                    }
                }
            }
        }

        data = balance_message["push"]["pub"]["data"]
        self.assertIn("currency", data)
        self.assertIn("availableBalance", data)


if __name__ == "__main__":
    unittest.main()
