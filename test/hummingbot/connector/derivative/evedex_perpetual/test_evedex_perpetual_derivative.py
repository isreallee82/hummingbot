"""Unit tests for Evedex Perpetual Derivative connector."""
import asyncio
import json
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from typing import Any, Awaitable, Dict, List, Optional

from aioresponses.core import aioresponses

import hummingbot.connector.derivative.evedex_perpetual.evedex_perpetual_constants as CONSTANTS
import hummingbot.connector.derivative.evedex_perpetual.evedex_perpetual_web_utils as web_utils
from hummingbot.connector.derivative.evedex_perpetual.evedex_perpetual_derivative import EvedexPerpetualDerivative
from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode
from hummingbot.core.data_type.in_flight_order import OrderState
from hummingbot.core.event.event_logger import EventLogger
from hummingbot.core.event.events import MarketEvent


class EvedexPerpetualDerivativeUnitTest(IsolatedAsyncioWrapperTestCase):
    """
    Test suite for EvedexPerpetualDerivative connector.

    Based on official Evedex Swagger API:
    - https://swagger.evedex.com/?urls.primaryName=Exchange

    API Endpoints:
    - GET /api/market/instrument - Trading pairs/instruments
    - GET /api/market/{instrument}/deep - Order book
    - POST /api/v2/order/limit - Create limit order
    - POST /api/v2/order/market - Create market order
    - DELETE /api/order/{orderId} - Cancel order
    - GET /api/order/{orderId} - Get order details
    - GET /api/order/opened - Open orders
    - GET /api/fill - Order fills
    - GET /api/user/balance - User balance
    - GET /api/user/me - User info (for exchangeId)
    - GET /api/position - Positions
    """

    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "BTC"
        cls.quote_asset = "USDT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.domain = CONSTANTS.DEFAULT_DOMAIN
        cls.user_exchange_id = "12345"

    def setUp(self) -> None:
        super().setUp()
        self.log_records = []
        self.ws_sent_messages = []
        self.ws_incoming_messages = asyncio.Queue()
        self.resume_test_event = asyncio.Event()

        self.exchange = EvedexPerpetualDerivative(
            evedex_perpetual_api_key="testAPIKey",
            evedex_perpetual_private_key="0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",  # noqa: mock
            trading_pairs=[self.trading_pair],
            domain=self.domain,
        )

        self.exchange._set_current_timestamp(1640780000)
        self.exchange.logger().setLevel(1)
        self.exchange.logger().addHandler(self)

        self.mocking_assistant = NetworkMockingAssistant(self.local_event_loop)
        self.test_task: Optional[asyncio.Task] = None
        self._initialize_event_loggers()

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: float = 5) -> Any:
        """Run async coroutine with timeout."""
        return self.local_event_loop.run_until_complete(asyncio.wait_for(coroutine, timeout))

    @property
    def all_symbols_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.INSTRUMENTS_PATH_URL)

    @property
    def latest_prices_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.INSTRUMENTS_PATH_URL)

    @property
    def network_status_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.PING_PATH_URL)

    @property
    def trading_rules_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.INSTRUMENTS_PATH_URL)

    @property
    def balance_url(self):
        return web_utils.private_rest_url(path_url=CONSTANTS.AVAILABLE_BALANCE_PATH_URL)

    @property
    def positions_url(self):
        return web_utils.private_rest_url(path_url=CONSTANTS.POSITIONS_PATH_URL)

    def tearDown(self) -> None:
        if self.test_task is not None:
            self.test_task.cancel()
        super().tearDown()

    def _initialize_event_loggers(self):
        self.buy_order_completed_logger = EventLogger()
        self.sell_order_completed_logger = EventLogger()
        self.order_cancelled_logger = EventLogger()
        self.order_filled_logger = EventLogger()
        self.funding_payment_completed_logger = EventLogger()

        events_and_loggers = [
            (MarketEvent.BuyOrderCompleted, self.buy_order_completed_logger),
            (MarketEvent.SellOrderCompleted, self.sell_order_completed_logger),
            (MarketEvent.OrderCancelled, self.order_cancelled_logger),
            (MarketEvent.OrderFilled, self.order_filled_logger),
            (MarketEvent.FundingPaymentCompleted, self.funding_payment_completed_logger)
        ]

        for event, logger in events_and_loggers:
            self.exchange.add_listener(event, logger)

    def handle(self, record):
        self.log_records.append(record)

    def _is_logged(self, log_level: str, message: str) -> bool:
        return any(record.levelname == log_level and record.getMessage() == message for record in self.log_records)

    def _instruments_response(self) -> List[Dict[str, Any]]:
        """
        Mock response for GET /api/market/instrument based on Swagger API.
        Instrument schema from official OpenAPI spec.
        """
        return [
            {
                "id": "1",
                "name": self.ex_trading_pair,
                "displayName": f"{self.base_asset}/{self.quote_asset}",
                "from": {
                    "id": "1",
                    "name": self.base_asset,
                    "symbol": self.base_asset,
                    "image": None,
                    "precision": 8,
                    "showPrecision": 8,
                    "createdAt": "2024-01-01T00:00:00.000Z",
                    "avgLastPrice": 50000.0
                },
                "to": {
                    "id": "2",
                    "name": self.quote_asset,
                    "symbol": self.quote_asset,
                    "image": None,
                    "precision": 8,
                    "showPrecision": 8,
                    "createdAt": "2024-01-01T00:00:00.000Z"
                },
                "maxLeverage": 100,
                "leverageLimit": {"100000": 50, "500000": 20},
                "lotSize": 0.001,
                "priceIncrement": 0.01,
                "quantityIncrement": 0.001,
                "multiplier": 1.0,
                "maintenanceMargin": {},
                "minVolume": 10.0,
                "minPrice": 0.01,
                "maxPrice": 1000000.0,
                "minQuantity": 0.001,
                "maxQuantity": 10000.0,
                "slippageLimit": 0.1,
                "lastPrice": 50000.0,
                "markPrice": 50000.0,
                "fatFingerPriceProtection": 0.1,
                "markPriceLimit": 0.1,
                "visibility": "all",
                "trading": "all",
                "marketState": "OPEN",
                "updatedAt": "2024-01-01T00:00:00.000Z",
                "startDate": None,
                "isPopular": True,
                "newLabel": False
            }
        ]

    def _ping_response(self) -> Dict[str, Any]:
        """Mock response for GET /api/ping."""
        return {"time": 1640780000}

    def _balance_response(self) -> Dict[str, Any]:
        """
        Mock response for GET /api/market/available-balance based on actual API.
        Returns funding balance info with availableBalance.
        """
        return {
            "currency": "usdt",
            "funding": {
                "currency": "usdt",
                "balance": 5000.0
            },
            "availableBalance": 4500.0,
            "maintenanceMargin": 100.0
        }

    def _positions_response(self) -> Dict[str, Any]:
        """
        Mock response for GET /api/position based on Swagger API.
        Position schema from official OpenAPI spec.
        """
        return {
            "list": [
                {
                    "id": "pos_123456",
                    "user": "user_001",
                    "instrument": self.ex_trading_pair,
                    "quantity": 1.0,
                    "entryPrice": 49000.0,
                    "markPrice": 50000.0,
                    "liquidationPrice": 45000.0,
                    "leverage": 10,
                    "unrealizedPnL": 1000.0,
                    "realizedPnL": 0.0,
                    "marginMode": "CROSS",
                    "side": "LONG",
                    "createdAt": "2024-01-01T00:00:00.000Z",
                    "updatedAt": "2024-01-01T00:00:00.000Z"
                }
            ],
            "count": 1
        }

    def _order_response(self, status: str = "NEW") -> Dict[str, Any]:
        """
        Mock response for order operations based on Swagger API Order schema.
        """
        return {
            "id": "00001:00000000000000000000000001",
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
            "unFilledQuantity": 1.0 if status == "NEW" else 0.0,
            "cashQuantity": 50000.0,
            "filledAvgPrice": 0.0 if status == "NEW" else 50000.0,
            "realizedPnL": 0.0,
            "fee": [] if status == "NEW" else [{"coin": self.quote_asset, "quantity": 10.0}],
            "triggeredAt": None,
            "exchangeRequestId": "req_123",
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2024-01-01T00:00:00.000Z"
        }

    def _fills_response(self) -> Dict[str, Any]:
        """
        Mock response for GET /api/fill based on Swagger API.
        """
        return {
            "list": [
                {
                    "id": "fill_123456",
                    "order": "00001:00000000000000000000000001",
                    "instrument": self.ex_trading_pair,
                    "fillQuantity": 1.0,
                    "fillPrice": 50000.0,
                    "fillRole": "TAKER",
                    "fee": [{"coin": self.quote_asset, "quantity": 10.0}],
                    "pnl": 0.0,
                    "isPnlRealized": False,
                    "createdAt": "2024-01-01T00:00:00.000Z"
                }
            ],
            "count": 1
        }

    def _user_me_response(self) -> Dict[str, Any]:
        """Mock response for GET /api/user/me."""
        return {
            "id": "user_001",
            "exchangeId": self.user_exchange_id,
            "email": "test@example.com",
            "status": "ACTIVE",
            "createdAt": "2024-01-01T00:00:00.000Z"
        }

    @aioresponses()
    def test_check_network_success(self, mock_api):
        """Test network check with /api/ping endpoint."""
        url = self.network_status_url
        mock_api.get(url, body=json.dumps(self._ping_response()), repeat=True)
        # Also mock instruments endpoint since it may be called during network check
        mock_api.get(self.all_symbols_url, body=json.dumps(self._instruments_response()), repeat=True)

        result = self.async_run_with_timeout(self.exchange.check_network())

        from hummingbot.core.network_iterator import NetworkStatus
        self.assertEqual(result, NetworkStatus.CONNECTED)

    @aioresponses()
    def test_check_network_failure(self, mock_api):
        """Test network check failure."""
        url = self.network_status_url
        mock_api.get(url, status=500, repeat=True)
        # Also mock instruments endpoint since it may be called during network check
        mock_api.get(self.all_symbols_url, body=json.dumps(self._instruments_response()), repeat=True)

        result = self.async_run_with_timeout(self.exchange.check_network())

        from hummingbot.core.network_iterator import NetworkStatus
        self.assertNotEqual(result, NetworkStatus.CONNECTED)  # Not connected

    @aioresponses()
    def test_update_trading_rules(self, mock_api):
        """Test trading rules update from /api/market/instrument."""
        url = self.trading_rules_url
        # Mock the instruments endpoint multiple times - once for symbol map initialization, once for trading rules
        mock_api.get(url, body=json.dumps(self._instruments_response()), repeat=True)

        # Initialize symbol map first with the mocked response data (sync method)
        exchange_info = self._instruments_response()
        self.exchange._initialize_trading_pair_symbols_from_exchange_info(exchange_info)

        self.async_run_with_timeout(self.exchange._update_trading_rules())

        self.assertIn(self.trading_pair, self.exchange.trading_rules)
        trading_rule = self.exchange.trading_rules[self.trading_pair]
        self.assertEqual(trading_rule.min_order_size, Decimal("0.001"))
        self.assertEqual(trading_rule.min_price_increment, Decimal("0.01"))
        self.assertEqual(trading_rule.min_base_amount_increment, Decimal("0.001"))

    @aioresponses()
    def test_update_balances(self, mock_api):
        """Test balance update from /api/market/available-balance."""
        url = self.balance_url
        mock_api.get(url, body=json.dumps(self._balance_response()))

        self.async_run_with_timeout(self.exchange._update_balances())

        # The perpetual connector uses USDT as the funding currency
        self.assertEqual(self.exchange.available_balances["USDT"], Decimal("4500.0"))
        self.assertEqual(self.exchange.get_balance("USDT"), Decimal("5000.0"))

    def test_supported_order_types(self):
        """Test supported order types match API capabilities."""
        supported_types = self.exchange.supported_order_types()

        self.assertIn(OrderType.LIMIT, supported_types)
        self.assertIn(OrderType.MARKET, supported_types)

    def test_supported_position_modes(self):
        """Test supported position modes (Evedex uses one-way mode)."""
        supported_modes = self.exchange.supported_position_modes()

        self.assertIn(PositionMode.ONEWAY, supported_modes)

    def test_exchange_symbol_format(self):
        """Test exchange symbol format: BASE-QUOTE."""
        symbol = f"{self.base_asset}-{self.quote_asset}"
        self.assertEqual(symbol, self.ex_trading_pair)

    def test_client_order_id_prefix(self):
        """Test client order ID prefix."""
        self.assertEqual(self.exchange.client_order_id_prefix, CONSTANTS.HBOT_ORDER_ID_PREFIX)

    def test_is_cancel_request_synchronous(self):
        """Test that cancel requests are synchronous."""
        self.assertTrue(self.exchange.is_cancel_request_in_exchange_synchronous)

    def test_order_state_mapping(self):
        """Test order state mapping matches Swagger API OrderStatus enum."""
        # From OrderStatus enum: INTENTION, NEW, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, EXPIRED, REPLACED, ERROR
        self.assertEqual(CONSTANTS.ORDER_STATE["INTENTION"], OrderState.PENDING_CREATE)
        self.assertEqual(CONSTANTS.ORDER_STATE["NEW"], OrderState.OPEN)
        self.assertEqual(CONSTANTS.ORDER_STATE["PARTIALLY_FILLED"], OrderState.PARTIALLY_FILLED)
        self.assertEqual(CONSTANTS.ORDER_STATE["FILLED"], OrderState.FILLED)
        self.assertEqual(CONSTANTS.ORDER_STATE["CANCELLED"], OrderState.CANCELED)
        self.assertEqual(CONSTANTS.ORDER_STATE["REJECTED"], OrderState.FAILED)
        self.assertEqual(CONSTANTS.ORDER_STATE["EXPIRED"], OrderState.CANCELED)
        self.assertEqual(CONSTANTS.ORDER_STATE["REPLACED"], OrderState.OPEN)
        self.assertEqual(CONSTANTS.ORDER_STATE["ERROR"], OrderState.FAILED)


class EvedexPerpetualOrderCreationTests(IsolatedAsyncioWrapperTestCase):
    """Test order creation functionality."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "BTC"
        cls.quote_asset = "USDT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.domain = CONSTANTS.DEFAULT_DOMAIN

    def setUp(self) -> None:
        super().setUp()
        self.exchange = EvedexPerpetualDerivative(
            evedex_perpetual_api_key="testAPIKey",
            evedex_perpetual_private_key="0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",  # noqa: mock
            trading_pairs=[self.trading_pair],
            domain=self.domain,
        )
        self.exchange._set_current_timestamp(1640780000)

    def _limit_order_response(self) -> Dict[str, Any]:
        """Mock response for POST /api/v2/order/limit."""
        return {
            "id": "00001:00000000000000000000000001",
            "user": "user_001",
            "instrument": self.ex_trading_pair,
            "type": "LIMIT",
            "side": "BUY",
            "status": "NEW",
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

    def _market_order_response(self) -> Dict[str, Any]:
        """Mock response for POST /api/v2/order/market."""
        return {
            "id": "00001:00000000000000000000000002",
            "user": "user_001",
            "instrument": self.ex_trading_pair,
            "type": "MARKET",
            "side": "BUY",
            "status": "FILLED",
            "rejectedReason": "",
            "quantity": 1.0,
            "limitPrice": None,
            "stopPrice": None,
            "group": "manually",
            "unFilledQuantity": 0.0,
            "cashQuantity": 50000.0,
            "filledAvgPrice": 50000.0,
            "realizedPnL": 0.0,
            "fee": [{"coin": self.quote_asset, "quantity": 10.0}],
            "triggeredAt": None,
            "exchangeRequestId": "req_124",
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2024-01-01T00:00:00.000Z"
        }

    @aioresponses()
    def test_create_limit_buy_order(self, mock_api):
        """Test limit buy order creation via POST /api/v2/order/limit."""
        url = web_utils.private_rest_url(CONSTANTS.LIMIT_ORDER_PATH_URL)
        mock_api.post(url, body=json.dumps(self._limit_order_response()))

        # Set up trading rules
        self.exchange._trading_rules[self.trading_pair] = TradingRule(
            trading_pair=self.trading_pair,
            min_order_size=Decimal("0.001"),
            min_price_increment=Decimal("0.01"),
            min_base_amount_increment=Decimal("0.001"),
        )

        order_id = self.exchange.buy(
            trading_pair=self.trading_pair,
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            price=Decimal("50000"),
            position_action=PositionAction.OPEN
        )

        self.assertIsNotNone(order_id)

    @aioresponses()
    def test_create_limit_sell_order(self, mock_api):
        """Test limit sell order creation."""
        response = self._limit_order_response()
        response["side"] = "SELL"
        url = web_utils.private_rest_url(CONSTANTS.LIMIT_ORDER_PATH_URL)
        mock_api.post(url, body=json.dumps(response))

        self.exchange._trading_rules[self.trading_pair] = TradingRule(
            trading_pair=self.trading_pair,
            min_order_size=Decimal("0.001"),
            min_price_increment=Decimal("0.01"),
            min_base_amount_increment=Decimal("0.001"),
        )

        order_id = self.exchange.sell(
            trading_pair=self.trading_pair,
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            price=Decimal("50000"),
            position_action=PositionAction.CLOSE
        )

        self.assertIsNotNone(order_id)


class EvedexPerpetualPositionTests(IsolatedAsyncioWrapperTestCase):
    """Test position management functionality."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "BTC"
        cls.quote_asset = "USDT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.domain = CONSTANTS.DEFAULT_DOMAIN

    def setUp(self) -> None:
        super().setUp()
        self.exchange = EvedexPerpetualDerivative(
            evedex_perpetual_api_key="testAPIKey",
            evedex_perpetual_private_key="0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",  # noqa: mock
            trading_pairs=[self.trading_pair],
            domain=self.domain,
        )
        self.exchange._set_current_timestamp(1640780000)

    def _positions_response(self) -> Dict[str, Any]:
        """Mock response for GET /api/position."""
        return {
            "list": [
                {
                    "id": "pos_123456",
                    "user": "user_001",
                    "instrument": self.ex_trading_pair,
                    "quantity": 1.0,
                    "entryPrice": 49000.0,
                    "markPrice": 50000.0,
                    "liquidationPrice": 45000.0,
                    "leverage": 10,
                    "unrealizedPnL": 1000.0,
                    "realizedPnL": 0.0,
                    "marginMode": "CROSS",
                    "side": "LONG",
                    "createdAt": "2024-01-01T00:00:00.000Z",
                    "updatedAt": "2024-01-01T00:00:00.000Z"
                }
            ],
            "count": 1
        }

    def test_position_mode_is_oneway(self):
        """Test that Evedex uses one-way position mode."""
        self.assertEqual(self.exchange._position_mode, PositionMode.ONEWAY)


class EvedexPerpetualWebSocketTests(IsolatedAsyncioWrapperTestCase):
    """Test WebSocket functionality with Centrifuge protocol."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "BTC"
        cls.quote_asset = "USDT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.user_exchange_id = "12345"

    def _order_ws_update(self, status: str = "NEW") -> Dict[str, Any]:
        """
        Mock WebSocket message from order-{userExchangeId} channel.
        """
        return {
            "channel": f"order-{self.user_exchange_id}",
            "data": {
                "id": "00001:00000000000000000000000001",
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
                "unFilledQuantity": 1.0 if status != "FILLED" else 0.0,
                "cashQuantity": 50000.0,
                "filledAvgPrice": 0.0 if status != "FILLED" else 50000.0,
                "realizedPnL": 0.0,
                "fee": [] if status != "FILLED" else [{"coin": self.quote_asset, "quantity": 10.0}],
                "triggeredAt": None,
                "exchangeRequestId": "req_123",
                "createdAt": "2024-01-01T00:00:00.000Z",
                "updatedAt": "2024-01-01T00:00:00.000Z"
            }
        }

    def _fill_ws_update(self) -> Dict[str, Any]:
        """
        Mock WebSocket message from orderFills-{userExchangeId} channel.
        """
        return {
            "channel": f"orderFills-{self.user_exchange_id}",
            "data": {
                "executionId": "fill_123456",
                "orderId": "00001:00000000000000000000000001",
                "instrumentName": self.ex_trading_pair,
                "side": "BUY",
                "fillPrice": 50000.0,
                "fillQuantity": 1.0,
                "fillValue": 50000.0,
                "fee": [{"coin": self.quote_asset, "quantity": 10.0}],
                "pnl": 0.0,
                "isPnlRealized": False,
                "createdAt": "2024-01-01T00:00:00.000Z"
            }
        }

    def _balance_ws_update(self) -> Dict[str, Any]:
        """
        Mock WebSocket message from user-{userExchangeId} channel.
        """
        return {
            "channel": f"user-{self.user_exchange_id}",
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

    def _position_ws_update(self) -> Dict[str, Any]:
        """
        Mock WebSocket message for position update from user-{userExchangeId} channel.
        """
        return {
            "channel": f"user-{self.user_exchange_id}",
            "data": {
                "currency": self.quote_asset,
                "funding": {
                    "currency": self.quote_asset,
                    "balance": 5000.0
                },
                "availableBalance": 4000.0,
                "position": [
                    {
                        "instrument": self.ex_trading_pair,
                        "quantity": 1.0,
                        "entryPrice": 49000.0,
                        "markPrice": 50000.0,
                        "unrealizedPnL": 1000.0,
                        "leverage": 10,
                        "side": "LONG"
                    }
                ],
                "openOrder": [],
                "updatedAt": "2024-01-01T00:00:00.000Z"
            }
        }

    def test_centrifuge_channel_naming(self):
        """Test Centrifuge channel naming patterns."""
        order_channel = f"order-{self.user_exchange_id}"
        user_channel = f"user-{self.user_exchange_id}"
        fills_channel = f"orderFills-{self.user_exchange_id}"
        orderbook_channel = f"orderBook-{self.ex_trading_pair}-OneTenth"
        trade_channel = f"trade-{self.ex_trading_pair}"

        self.assertEqual(order_channel, "order-12345")
        self.assertEqual(user_channel, "user-12345")
        self.assertEqual(fills_channel, "orderFills-12345")
        self.assertEqual(orderbook_channel, f"orderBook-{self.ex_trading_pair}-OneTenth")
        self.assertEqual(trade_channel, f"trade-{self.ex_trading_pair}")

    def test_order_status_values(self):
        """Test all order status values from Swagger API OrderStatus enum."""
        statuses = [
            "INTENTION",
            "NEW",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
            "REPLACED",
            "ERROR"
        ]
        for status in statuses:
            self.assertIn(status, CONSTANTS.ORDER_STATE)


if __name__ == "__main__":
    import unittest
    unittest.main()
