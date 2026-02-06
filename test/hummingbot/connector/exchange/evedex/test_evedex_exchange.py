"""Unit tests for Evedex Exchange connector."""

import json
import re
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

from aioresponses import aioresponses
from aioresponses.core import RequestCall

from hummingbot.connector.exchange.evedex import evedex_constants as CONSTANTS, evedex_web_utils as web_utils
from hummingbot.connector.exchange.evedex.evedex_exchange import EvedexExchange
from hummingbot.connector.test_support.exchange_connector_test import AbstractExchangeConnectorTests
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import get_new_client_order_id
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TokenAmount, TradeFeeBase
from hummingbot.core.event.events import OrderFilledEvent


class EvedexExchangeTests(AbstractExchangeConnectorTests.ExchangeConnectorTests):
    """Tests for EvedexExchange connector using the abstract test base."""

    @property
    def all_symbols_url(self):
        return web_utils.public_rest_url(path_url=CONSTANTS.INSTRUMENTS_PATH_URL, domain=self.exchange._domain)

    @property
    def latest_prices_url(self):
        url = web_utils.public_rest_url(path_url=CONSTANTS.INSTRUMENTS_PATH_URL, domain=self.exchange._domain)
        url = f"{url}?fields=metrics&instrument={self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset)}"
        return url

    @property
    def network_status_url(self):
        url = web_utils.public_rest_url(CONSTANTS.PING_PATH_URL, domain=self.exchange._domain)
        return url

    @property
    def trading_rules_url(self):
        url = web_utils.public_rest_url(CONSTANTS.INSTRUMENTS_PATH_URL, domain=self.exchange._domain)
        return url

    @property
    def order_creation_url(self):
        url = web_utils.private_rest_url(CONSTANTS.LIMIT_ORDER_PATH_URL, domain=self.exchange._domain)
        return url

    @property
    def balance_url(self):
        url = web_utils.private_rest_url(CONSTANTS.USER_BALANCE_PATH_URL, domain=self.exchange._domain)
        return url

    @property
    def all_symbols_request_mock_response(self):
        """Mock response for GET /api/market/instrument based on official Swagger API."""
        return [
            {
                "id": "1",
                "name": f"{self.base_asset}-{self.quote_asset}",
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
                "minVolume": 10,
                "minPrice": 0.01,
                "maxPrice": 1000000.0,
                "minQuantity": 0.001,
                "maxQuantity": 10000.0,
                "slippageLimit": 0.1,
                "lastPrice": self.expected_latest_price,
                "markPrice": self.expected_latest_price,
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

    @property
    def latest_prices_request_mock_response(self):
        """Mock response for GET /api/market/instrument with metrics."""
        return self.all_symbols_request_mock_response

    @property
    def all_symbols_including_invalid_pair_mock_response(self) -> Tuple[str, Any]:
        response = [
            {
                "id": "1",
                "name": f"{self.base_asset}-{self.quote_asset}",
                "displayName": f"{self.base_asset}/{self.quote_asset}",
                "from": {"id": "1", "name": self.base_asset, "symbol": self.base_asset, "image": None, "precision": 8,
                         "showPrecision": 8, "createdAt": "2024-01-01T00:00:00.000Z", "avgLastPrice": 50000.0},
                "to": {"id": "2", "name": self.quote_asset, "symbol": self.quote_asset, "image": None, "precision": 8,
                       "showPrecision": 8, "createdAt": "2024-01-01T00:00:00.000Z"},
                "maxLeverage": 100,
                "lotSize": 0.001,
                "priceIncrement": 0.01,
                "quantityIncrement": 0.001,
                "minQuantity": 0.001,
                "maxQuantity": 10000.0,
                "minVolume": 10,
                "lastPrice": self.expected_latest_price,
                "trading": "all",
                "marketState": "OPEN"
            },
            {
                "id": "2",
                "name": "INVALID-PAIR",
                "displayName": "INVALID/PAIR",
                "from": {"id": "3", "name": "INVALID", "symbol": "INVALID", "image": None, "precision": 8,
                         "showPrecision": 8, "createdAt": "2024-01-01T00:00:00.000Z", "avgLastPrice": 1.0},
                "to": {"id": "4", "name": "PAIR", "symbol": "PAIR", "image": None, "precision": 8,
                       "showPrecision": 8, "createdAt": "2024-01-01T00:00:00.000Z"},
                "maxLeverage": 100,
                "lotSize": 0.001,
                "priceIncrement": 0.01,
                "quantityIncrement": 0.001,
                "minQuantity": 0.001,
                "maxQuantity": 10000.0,
                "minVolume": 10,
                "lastPrice": 1.0,
                "trading": "restricted",
                "marketState": "OPEN"
            }
        ]
        return "INVALID-PAIR", response

    @property
    def network_status_request_successful_mock_response(self):
        """Mock response for GET /api/ping based on Swagger API."""
        return {"time": 1640000000}

    @property
    def trading_rules_request_mock_response(self):
        """Mock response for trading rules from /api/market/instrument."""
        return [
            {
                "id": "1",
                "name": f"{self.base_asset}-{self.quote_asset}",
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
                "lotSize": 0.001,
                "priceIncrement": 0.01,
                "quantityIncrement": 0.001,
                "minVolume": 10.0,
                "minPrice": 0.01,
                "maxPrice": 1000000.0,
                "minQuantity": 0.001,
                "maxQuantity": 10000.0,
                "lastPrice": 50000.0,
                "trading": "all",
                "marketState": "OPEN"
            }
        ]

    @property
    def trading_rules_request_erroneous_mock_response(self):
        """Mock response with missing required fields."""
        return [
            {
                "id": "1",
                "name": f"{self.base_asset}-{self.quote_asset}",
                "displayName": f"{self.base_asset}/{self.quote_asset}",
                # Missing required fields like lotSize, priceIncrement, etc.
            }
        ]

    @property
    def order_creation_request_successful_mock_response(self):
        """Mock response for POST /api/v2/order/limit based on Swagger API Order schema."""
        return {
            "id": str(self.expected_exchange_order_id),
            "user": "user123",
            "instrument": f"{self.base_asset}-{self.quote_asset}",
            "type": "LIMIT",
            "side": "BUY",
            "status": "NEW",
            "rejectedReason": "",
            "quantity": 100.0,
            "limitPrice": 10000.0,
            "stopPrice": None,
            "group": "manually",
            "unFilledQuantity": 100.0,
            "cashQuantity": 1000000.0,
            "filledAvgPrice": 0.0,
            "realizedPnL": 0.0,
            "fee": [],
            "triggeredAt": None,
            "exchangeRequestId": "req123",
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2024-01-01T00:00:00.000Z"
        }

    @property
    def balance_request_mock_response_for_base_and_quote(self):
        """Mock response for GET /api/user/balance based on Swagger API BalanceItem schema."""
        return [
            {
                "currency": self.base_asset,
                "availableBalance": 10.0,
                "balance": 15.0,
                "balanceUSD": 750000.0
            },
            {
                "currency": self.quote_asset,
                "availableBalance": 2000.0,
                "balance": 2000.0,
                "balanceUSD": 2000.0
            }
        ]

    @property
    def balance_request_mock_response_only_base(self):
        """Mock response for balance with only base asset."""
        return [
            {
                "currency": self.base_asset,
                "availableBalance": 10.0,
                "balance": 15.0,
                "balanceUSD": 750000.0
            }
        ]

    @property
    def balance_event_websocket_update(self):
        """WebSocket balance update from spot:user-{userExchangeId} channel (Centrifugo push format)."""
        return {
            "push": {
                "channel": "spot:user-12345",
                "pub": {
                    "data": {
                        "currency": self.base_asset,
                        "funding": {
                            "currency": self.base_asset,
                            "balance": 15.0
                        },
                        "availableBalance": 10.0,
                        "position": [],
                        "openOrder": [],
                        "updatedAt": "2024-01-01T00:00:00.000Z"
                    }
                }
            }
        }

    @property
    def expected_latest_price(self):
        return 50000.0

    @property
    def expected_supported_order_types(self):
        return [OrderType.LIMIT, OrderType.MARKET]

    @property
    def expected_trading_rule(self):
        instrument = self.trading_rules_request_mock_response[0]
        return TradingRule(
            trading_pair=self.trading_pair,
            min_order_size=Decimal(str(instrument["minQuantity"])),
            min_price_increment=Decimal(str(instrument["priceIncrement"])),
            min_base_amount_increment=Decimal(str(instrument["quantityIncrement"])),
            min_notional_size=Decimal(str(instrument["minVolume"])),
        )

    @property
    def expected_logged_error_for_erroneous_trading_rule(self):
        erroneous_rule = self.trading_rules_request_erroneous_mock_response[0]
        return f"Error parsing the trading pair rule {erroneous_rule}. Skipping."

    @property
    def expected_exchange_order_id(self):
        return "00001:00000000000000000000000001"

    @property
    def is_order_fill_http_update_included_in_status_update(self) -> bool:
        return True

    @property
    def is_order_fill_http_update_executed_during_websocket_order_event_processing(self) -> bool:
        return False

    @property
    def expected_partial_fill_price(self) -> Decimal:
        return Decimal("49500")

    @property
    def expected_partial_fill_amount(self) -> Decimal:
        return Decimal("0.5")

    @property
    def expected_fill_fee(self) -> TradeFeeBase:
        return DeductedFromReturnsTradeFee(
            percent_token=self.quote_asset,
            flat_fees=[TokenAmount(token=self.quote_asset, amount=Decimal("10"))])

    @property
    def expected_fill_trade_id(self) -> str:
        return "fill_123456"

    def exchange_symbol_for_tokens(self, base_token: str, quote_token: str) -> str:
        """Evedex uses format: BASE-QUOTE (e.g., BTC-USDT)."""
        return f"{base_token}-{quote_token}"

    def create_exchange_instance(self):
        return EvedexExchange(
            evedex_api_key="testAPIKey",
            evedex_private_key="0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",  # noqa: mock
            trading_pairs=[self.trading_pair],
        )

    def validate_auth_credentials_present(self, request_call: RequestCall):
        """Validate that API key is present in request headers."""
        headers = request_call.kwargs.get("headers", {})
        self.assertIn("X-API-Key", headers)

    def validate_order_creation_request(self, order: InFlightOrder, request_call: RequestCall):
        """Validate order creation request matches expected format."""
        request_data = request_call.kwargs.get("data") or request_call.kwargs.get("json")
        if isinstance(request_data, str):
            request_data = json.loads(request_data)

        self.assertEqual(f"{self.base_asset}-{self.quote_asset}", request_data["instrument"])
        self.assertEqual(order.trade_type.name.upper(), request_data["side"])
        self.assertEqual(Decimal("100"), Decimal(str(request_data["quantity"])))
        self.assertEqual(Decimal("10000"), Decimal(str(request_data["limitPrice"])))
        self.assertEqual(CONSTANTS.CHAIN_ID, str(request_data["chainId"]))
        self.assertEqual(1, int(request_data["leverage"]))
        self.assertTrue(str(request_data["signature"]).startswith("0x"))

    def validate_order_cancelation_request(self, order: InFlightOrder, request_call: RequestCall):
        """Validate order cancellation request."""
        # Evedex uses DELETE /api/order/{orderId}
        # The order ID is in the URL path, which is validated by the mock URL matching
        # Just verify the request was made (mock would not have been called otherwise)
        self.assertIsNotNone(request_call)

    def validate_order_status_request(self, order: InFlightOrder, request_call: RequestCall):
        """Validate order status request."""
        # Evedex uses GET /api/order/{orderId}
        # The order ID is in the URL path, which is validated by the mock URL matching
        self.assertIsNotNone(request_call)

    def validate_trades_request(self, order: InFlightOrder, request_call: RequestCall):
        """Validate trades request."""
        request_params = request_call.kwargs.get("params", {})
        # Evedex uses /api/fill with instrument filter
        self.assertIn("instrument", request_params)

    def configure_successful_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL.format(orderId=order.exchange_order_id))
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {}  # Empty response on success
        mock_api.delete(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_erroneous_cancelation_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL.format(orderId=order.exchange_order_id))
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.delete(regex_url, status=400, callback=callback)
        return url

    def configure_order_not_found_error_cancelation_response(
        self, order: InFlightOrder, mock_api: aioresponses, callback: Optional[Callable] = lambda *args, **kwargs: None
    ) -> str:
        url = web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH_URL.format(orderId=order.exchange_order_id))
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"message": CONSTANTS.ORDER_NOT_EXIST_MESSAGE}
        mock_api.delete(regex_url, status=404, body=json.dumps(response), callback=callback)
        return url

    def configure_one_successful_one_erroneous_cancel_all_response(
            self,
            successful_order: InFlightOrder,
            erroneous_order: InFlightOrder,
            mock_api: aioresponses) -> List[str]:
        all_urls = []
        url = self.configure_successful_cancelation_response(order=successful_order, mock_api=mock_api)
        all_urls.append(url)
        url = self.configure_erroneous_cancelation_response(order=erroneous_order, mock_api=mock_api)
        all_urls.append(url)
        return all_urls

    def configure_completely_filled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.GET_ORDER_PATH_URL.format(orderId=order.exchange_order_id))
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_completely_filled_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_canceled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.GET_ORDER_PATH_URL.format(orderId=order.exchange_order_id))
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_canceled_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_open_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.GET_ORDER_PATH_URL.format(orderId=order.exchange_order_id))
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_open_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_http_error_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.GET_ORDER_PATH_URL.format(orderId=order.exchange_order_id))
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, status=401, callback=callback)
        return url

    def configure_partially_filled_order_status_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.GET_ORDER_PATH_URL.format(orderId=order.exchange_order_id))
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_status_request_partially_filled_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_order_not_found_error_order_status_response(
        self, order: InFlightOrder, mock_api: aioresponses, callback: Optional[Callable] = lambda *args, **kwargs: None
    ) -> List[str]:
        url = web_utils.private_rest_url(CONSTANTS.GET_ORDER_PATH_URL.format(orderId=order.exchange_order_id))
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = {"message": CONSTANTS.ORDER_NOT_EXIST_MESSAGE}
        mock_api.get(regex_url, body=json.dumps(response), status=404, callback=callback)
        return [url]

    def configure_partial_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_FILLS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_fills_request_partial_fill_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_full_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_FILLS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        response = self._order_fills_request_full_fill_mock_response(order=order)
        mock_api.get(regex_url, body=json.dumps(response), callback=callback)
        return url

    def configure_erroneous_http_fill_trade_response(
            self,
            order: InFlightOrder,
            mock_api: aioresponses,
            callback: Optional[Callable] = lambda *args, **kwargs: None) -> str:
        url = web_utils.private_rest_url(CONSTANTS.ORDER_FILLS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))
        mock_api.get(regex_url, status=400, callback=callback)
        return url

    def order_event_for_new_order_websocket_update(self, order: InFlightOrder):
        """WebSocket order update from spot:order-{userExchangeId} channel for new order (Centrifugo push format)."""
        return {
            "push": {
                "channel": "spot:order-12345",
                "pub": {
                    "data": {
                        "id": order.exchange_order_id,
                        "user": "user123",
                        "instrument": f"{self.base_asset}-{self.quote_asset}",
                        "type": "LIMIT",
                        "side": order.trade_type.name.upper(),
                        "status": "NEW",
                        "rejectedReason": "",
                        "quantity": float(order.amount),
                        "limitPrice": float(order.price),
                        "stopPrice": None,
                        "group": "manually",
                        "unFilledQuantity": float(order.amount),
                        "cashQuantity": float(order.amount * order.price),
                        "filledAvgPrice": 0.0,
                        "realizedPnL": 0.0,
                        "fee": [],
                        "triggeredAt": None,
                        "exchangeRequestId": "req123",
                        "createdAt": "2024-01-01T00:00:00.000Z",
                        "updatedAt": "2024-01-01T00:00:00.000Z"
                    }
                }
            }
        }

    def order_event_for_canceled_order_websocket_update(self, order: InFlightOrder):
        """WebSocket order update from spot:order-{userExchangeId} channel for canceled order (Centrifugo push format)."""
        return {
            "push": {
                "channel": "spot:order-12345",
                "pub": {
                    "data": {
                        "id": order.exchange_order_id,
                        "user": "user123",
                        "instrument": f"{self.base_asset}-{self.quote_asset}",
                        "type": "LIMIT",
                        "side": order.trade_type.name.upper(),
                        "status": "CANCELLED",
                        "rejectedReason": "",
                        "quantity": float(order.amount),
                        "limitPrice": float(order.price),
                        "stopPrice": None,
                        "group": "manually",
                        "unFilledQuantity": float(order.amount),
                        "cashQuantity": float(order.amount * order.price),
                        "filledAvgPrice": 0.0,
                        "realizedPnL": 0.0,
                        "fee": [],
                        "triggeredAt": None,
                        "exchangeRequestId": "req123",
                        "createdAt": "2024-01-01T00:00:00.000Z",
                        "updatedAt": "2024-01-01T00:00:00.000Z"
                    }
                }
            }
        }

    def order_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        """WebSocket order update from spot:order-{userExchangeId} channel for filled order (Centrifugo push format)."""
        return {
            "push": {
                "channel": "spot:order-12345",
                "pub": {
                    "data": {
                        "id": order.exchange_order_id,
                        "user": "user123",
                        "instrument": f"{self.base_asset}-{self.quote_asset}",
                        "type": "LIMIT",
                        "side": order.trade_type.name.upper(),
                        "status": "FILLED",
                        "rejectedReason": "",
                        "quantity": float(order.amount),
                        "limitPrice": float(order.price),
                        "stopPrice": None,
                        "group": "manually",
                        "unFilledQuantity": 0.0,
                        "cashQuantity": float(order.amount * order.price),
                        "filledAvgPrice": float(order.price),
                        "realizedPnL": 0.0,
                        "fee": [{"coin": self.quote_asset, "quantity": float(self.expected_fill_fee.flat_fees[0].amount)}],
                        "triggeredAt": None,
                        "exchangeRequestId": "req123",
                        "createdAt": "2024-01-01T00:00:00.000Z",
                        "updatedAt": "2024-01-01T00:00:00.000Z"
                    }
                }
            }
        }

    def trade_event_for_full_fill_websocket_update(self, order: InFlightOrder):
        """WebSocket fill event from spot:orderFilled-{userExchangeId} channel (Centrifugo push format)."""
        return {
            "push": {
                "channel": "spot:orderFilled-12345",
                "pub": {
                    "data": {
                        "executionId": self.expected_fill_trade_id,
                        "orderId": order.exchange_order_id,
                        "instrumentName": f"{self.base_asset}-{self.quote_asset}",
                        "side": order.trade_type.name.upper(),
                        "fillPrice": float(order.price),
                        "fillQuantity": float(order.amount),
                        "fillValue": float(order.amount * order.price),
                        "fee": [{"coin": self.quote_asset, "quantity": float(self.expected_fill_fee.flat_fees[0].amount)}],
                        "pnl": 0.0,
                        "isPnlRealized": False,
                        "createdAt": "2024-01-01T00:00:00.000Z"
                    }
                }
            }
        }

    def _order_status_request_completely_filled_mock_response(self, order: InFlightOrder) -> Dict[str, Any]:
        """Mock response for completely filled order based on Order schema."""
        return {
            "id": order.exchange_order_id,
            "user": "user123",
            "instrument": f"{self.base_asset}-{self.quote_asset}",
            "type": "LIMIT",
            "side": order.trade_type.name.upper(),
            "status": "FILLED",
            "rejectedReason": "",
            "quantity": float(order.amount),
            "limitPrice": float(order.price),
            "stopPrice": None,
            "group": "manually",
            "unFilledQuantity": 0.0,
            "cashQuantity": float(order.amount * order.price),
            "filledAvgPrice": float(order.price),
            "realizedPnL": 0.0,
            "fee": [{"coin": self.quote_asset, "quantity": float(self.expected_fill_fee.flat_fees[0].amount)}],
            "triggeredAt": None,
            "exchangeRequestId": "req123",
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2024-01-01T00:00:00.000Z"
        }

    def _order_status_request_canceled_mock_response(self, order: InFlightOrder) -> Dict[str, Any]:
        """Mock response for canceled order."""
        return {
            "id": order.exchange_order_id,
            "user": "user123",
            "instrument": f"{self.base_asset}-{self.quote_asset}",
            "type": "LIMIT",
            "side": order.trade_type.name.upper(),
            "status": "CANCELLED",
            "rejectedReason": "",
            "quantity": float(order.amount),
            "limitPrice": float(order.price),
            "stopPrice": None,
            "group": "manually",
            "unFilledQuantity": float(order.amount),
            "cashQuantity": float(order.amount * order.price),
            "filledAvgPrice": 0.0,
            "realizedPnL": 0.0,
            "fee": [],
            "triggeredAt": None,
            "exchangeRequestId": "req123",
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2024-01-01T00:00:00.000Z"
        }

    def _order_status_request_open_mock_response(self, order: InFlightOrder) -> Dict[str, Any]:
        """Mock response for open order."""
        return {
            "id": order.exchange_order_id,
            "user": "user123",
            "instrument": f"{self.base_asset}-{self.quote_asset}",
            "type": "LIMIT",
            "side": order.trade_type.name.upper(),
            "status": "NEW",
            "rejectedReason": "",
            "quantity": float(order.amount),
            "limitPrice": float(order.price),
            "stopPrice": None,
            "group": "manually",
            "unFilledQuantity": float(order.amount),
            "cashQuantity": float(order.amount * order.price),
            "filledAvgPrice": 0.0,
            "realizedPnL": 0.0,
            "fee": [],
            "triggeredAt": None,
            "exchangeRequestId": "req123",
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2024-01-01T00:00:00.000Z"
        }

    def _order_status_request_partially_filled_mock_response(self, order: InFlightOrder) -> Dict[str, Any]:
        """Mock response for partially filled order."""
        return {
            "id": order.exchange_order_id,
            "user": "user123",
            "instrument": f"{self.base_asset}-{self.quote_asset}",
            "type": "LIMIT",
            "side": order.trade_type.name.upper(),
            "status": "PARTIALLY_FILLED",
            "rejectedReason": "",
            "quantity": float(order.amount),
            "limitPrice": float(order.price),
            "stopPrice": None,
            "group": "manually",
            "unFilledQuantity": float(order.amount - self.expected_partial_fill_amount),
            "cashQuantity": float(order.amount * order.price),
            "filledAvgPrice": float(self.expected_partial_fill_price),
            "realizedPnL": 0.0,
            "fee": [{"coin": self.quote_asset, "quantity": 5.0}],
            "triggeredAt": None,
            "exchangeRequestId": "req123",
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2024-01-01T00:00:00.000Z"
        }

    def _order_fills_request_partial_fill_mock_response(self, order: InFlightOrder) -> Dict[str, Any]:
        """Mock response for partial fill from /api/fill endpoint."""
        return {
            "list": [
                {
                    "id": self.expected_fill_trade_id,
                    "order": order.exchange_order_id,
                    "instrument": f"{self.base_asset}-{self.quote_asset}",
                    "fillQuantity": float(self.expected_partial_fill_amount),
                    "fillPrice": float(self.expected_partial_fill_price),
                    "fillRole": "TAKER",
                    "fee": [{"coin": self.quote_asset, "quantity": float(self.expected_fill_fee.flat_fees[0].amount)}],
                    "createdAt": "2024-01-01T00:00:00.000Z"
                }
            ],
            "count": 1
        }

    def _order_fills_request_full_fill_mock_response(self, order: InFlightOrder) -> Dict[str, Any]:
        """Mock response for full fill from /api/fill endpoint."""
        return {
            "list": [
                {
                    "id": self.expected_fill_trade_id,
                    "order": order.exchange_order_id,
                    "instrument": f"{self.base_asset}-{self.quote_asset}",
                    "fillQuantity": float(order.amount),
                    "fillPrice": float(order.price),
                    "fillRole": "TAKER",
                    "fee": [{"coin": self.quote_asset, "quantity": float(self.expected_fill_fee.flat_fees[0].amount)}],
                    "createdAt": "2024-01-01T00:00:00.000Z"
                }
            ],
            "count": 1
        }

    def _order_cancelation_request_successful_mock_response(self, order: InFlightOrder) -> Dict[str, Any]:
        """Mock response for successful order cancellation."""
        return {}

    # Evedex-specific tests below

    @patch("hummingbot.connector.utils.get_tracking_nonce")
    def test_client_order_id_on_order(self, mocked_nonce):
        """Test that client order IDs are generated correctly for buy and sell orders."""
        mocked_nonce.return_value = 7

        result = self.exchange.buy(
            trading_pair=self.trading_pair,
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            price=Decimal("2"),
        )
        expected_client_order_id = get_new_client_order_id(
            is_buy=True,
            trading_pair=self.trading_pair,
            hbot_order_id_prefix=CONSTANTS.HBOT_ORDER_ID_PREFIX,
            max_id_len=CONSTANTS.MAX_ORDER_ID_LEN,
        )

        self.assertEqual(result, expected_client_order_id)

        result = self.exchange.sell(
            trading_pair=self.trading_pair,
            amount=Decimal("1"),
            order_type=OrderType.LIMIT,
            price=Decimal("2"),
        )
        expected_client_order_id = get_new_client_order_id(
            is_buy=False,
            trading_pair=self.trading_pair,
            hbot_order_id_prefix=CONSTANTS.HBOT_ORDER_ID_PREFIX,
            max_id_len=CONSTANTS.MAX_ORDER_ID_LEN,
        )

        self.assertEqual(result, expected_client_order_id)

    def test_time_synchronizer_related_request_error_detection(self):
        """Test that Evedex always returns False for time synchronizer errors (not used)."""
        exception = IOError("Error executing request POST https://exchange-api.evedex.com/api/v2/order/limit. "
                            "HTTP status is 400.")
        self.assertFalse(self.exchange._is_request_exception_related_to_time_synchronizer(exception))

        exception = IOError("Some other error")
        self.assertFalse(self.exchange._is_request_exception_related_to_time_synchronizer(exception))

    @aioresponses()
    def test_update_order_fills_from_trades_triggers_filled_event(self, mock_api):
        """Test that updating order fills from trades triggers the filled event."""
        self.exchange._set_current_timestamp(1640780000)
        self.exchange._last_poll_timestamp = (self.exchange.current_timestamp -
                                              self.exchange.UPDATE_ORDER_STATUS_MIN_INTERVAL - 1)

        self.exchange.start_tracking_order(
            order_id="OID1",
            exchange_order_id="00001:00000000000000000000000001",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
        )
        order = self.exchange.in_flight_orders["OID1"]

        url = web_utils.private_rest_url(CONSTANTS.ORDER_FILLS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        trade_fill = {
            "list": [
                {
                    "executionId": "exec123",
                    "orderId": order.exchange_order_id,
                    "instrument": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                    "fillQuantity": 1.0,
                    "fillPrice": 9999.0,
                    "fillRole": "TAKER",
                    "createdAt": "2024-01-01T00:00:00.000Z"
                }
            ],
            "count": 1
        }

        mock_api.get(regex_url, body=json.dumps(trade_fill))

        self.async_run_with_timeout(self.exchange._update_order_fills_from_trades())

        self.assertEqual(1, len(self.order_filled_logger.event_log))
        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
        self.assertEqual(order.client_order_id, fill_event.order_id)
        self.assertEqual(order.trading_pair, fill_event.trading_pair)
        self.assertEqual(order.trade_type, fill_event.trade_type)
        self.assertEqual(order.order_type, fill_event.order_type)
        self.assertEqual(Decimal("9999"), fill_event.price)
        self.assertEqual(Decimal("1"), fill_event.amount)

    @aioresponses()
    def test_update_order_fills_from_trades_with_repeated_fill_triggers_only_one_event(self, mock_api):
        """Test that repeated fills only trigger one event."""
        self.exchange._set_current_timestamp(1640780000)
        self.exchange._last_poll_timestamp = (self.exchange.current_timestamp -
                                              self.exchange.UPDATE_ORDER_STATUS_MIN_INTERVAL - 1)

        self.exchange.start_tracking_order(
            order_id="OID1",
            exchange_order_id="00001:00000000000000000000000001",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
        )
        order = self.exchange.in_flight_orders["OID1"]

        url = web_utils.private_rest_url(CONSTANTS.ORDER_FILLS_PATH_URL)
        regex_url = re.compile(f"^{url}".replace(".", r"\.").replace("?", r"\?"))

        # Same fill repeated twice
        trade_fill = {
            "list": [
                {
                    "executionId": "exec123",
                    "orderId": order.exchange_order_id,
                    "instrument": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                    "fillQuantity": 1.0,
                    "fillPrice": 9999.0,
                    "fillRole": "TAKER",
                    "createdAt": "2024-01-01T00:00:00.000Z"
                },
                {
                    "executionId": "exec123",  # Same execution ID
                    "orderId": order.exchange_order_id,
                    "instrument": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
                    "fillQuantity": 1.0,
                    "fillPrice": 9999.0,
                    "fillRole": "TAKER",
                    "createdAt": "2024-01-01T00:00:00.000Z"
                }
            ],
            "count": 2
        }

        mock_api.get(regex_url, body=json.dumps(trade_fill))

        self.async_run_with_timeout(self.exchange._update_order_fills_from_trades())

        # Should only have one fill event due to duplicate detection
        self.assertEqual(1, len(self.order_filled_logger.event_log))

    # @aioresponses()
    # def test_update_order_status_when_failed(self, mock_api):
    #     """Test order status update when order has failed/rejected status."""
    #     self.exchange._set_current_timestamp(1640780000)
    #     self.exchange._last_poll_timestamp = (self.exchange.current_timestamp -
    #                                           self.exchange.UPDATE_ORDER_STATUS_MIN_INTERVAL - 1)

    #     # Use simpler order ID without special regex characters
    #     exchange_order_id = "100234"
    #     self.exchange.start_tracking_order(
    #         order_id="OID1",
    #         exchange_order_id=exchange_order_id,
    #         trading_pair=self.trading_pair,
    #         order_type=OrderType.LIMIT,
    #         trade_type=TradeType.BUY,
    #         price=Decimal("10000"),
    #         amount=Decimal("1"),
    #     )
    #     order = self.exchange.in_flight_orders["OID1"]

    #     order_status_url = web_utils.private_rest_url(
    #         CONSTANTS.GET_ORDER_PATH_URL.format(orderId=exchange_order_id))
    #     regex_url = re.compile(f"^{order_status_url}".replace(".", r"\.").replace("?", r"\?"))

    #     order_status = {
    #         "id": exchange_order_id,
    #         "user": "user123",
    #         "instrument": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
    #         "type": "LIMIT",
    #         "side": "BUY",
    #         "status": "REJECTED",
    #         "rejectedReason": "Insufficient balance",
    #         "quantity": 1.0,
    #         "limitPrice": 10000.0,
    #         "stopPrice": None,
    #         "group": "manually",
    #         "unFilledQuantity": 1.0,
    #         "cashQuantity": 10000.0,
    #         "filledAvgPrice": 0.0,
    #         "realizedPnL": 0.0,
    #         "fee": [],
    #         "triggeredAt": None,
    #         "exchangeRequestId": "req123",
    #         "createdAt": "2024-01-01T00:00:00.000Z",
    #         "updatedAt": "2024-01-01T00:00:01.000Z"
    #     }

    #     mock_api.get(regex_url, body=json.dumps(order_status))

    #     # Also mock the fills endpoint
    #     fills_url = web_utils.private_rest_url(
    #         CONSTANTS.GET_ORDER_PATH_URL.format(orderId=exchange_order_id) + "/fill")
    #     fills_regex_url = re.compile(f"^{fills_url}".replace(".", r"\.").replace("?", r"\?"))
    #     mock_api.get(fills_regex_url, body=json.dumps({"list": [], "count": 0}))

    #     self.async_run_with_timeout(self.exchange._update_order_status())

    #     failure_event: MarketOrderFailureEvent = self.order_failure_logger.event_log[0]
    #     self.assertEqual(self.exchange.current_timestamp, failure_event.timestamp)
    #     self.assertEqual(order.client_order_id, failure_event.order_id)
    #     self.assertEqual(order.order_type, failure_event.order_type)
    #     self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)

    # def test_user_stream_update_for_order_failure(self):
    #     """Test user stream update for order failure."""
    #     self.exchange._set_current_timestamp(1640780000)

    #     exchange_order_id = "100234"
    #     self.exchange.start_tracking_order(
    #         order_id="OID1",
    #         exchange_order_id=exchange_order_id,
    #         trading_pair=self.trading_pair,
    #         order_type=OrderType.LIMIT,
    #         trade_type=TradeType.BUY,
    #         price=Decimal("10000"),
    #         amount=Decimal("1"),
    #     )
    #     order = self.exchange.in_flight_orders["OID1"]

    #     # Evedex uses Centrifuge channel format: channel + data
    #     event_message = {
    #         "channel": "order-12345",
    #         "data": {
    #             "id": exchange_order_id,
    #             "user": "user123",
    #             "instrument": self.exchange_symbol_for_tokens(self.base_asset, self.quote_asset),
    #             "type": "LIMIT",
    #             "side": "BUY",
    #             "status": "REJECTED",
    #             "rejectedReason": "Insufficient balance",
    #             "quantity": 1.0,
    #             "limitPrice": 10000.0,
    #             "stopPrice": None,
    #             "unFilledQuantity": 1.0,
    #             "cashQuantity": 10000.0,
    #             "filledAvgPrice": 0.0,
    #             "realizedPnL": 0.0,
    #             "fee": [],
    #             "createdAt": "2024-01-01T00:00:00.000Z",
    #             "updatedAt": "2024-01-01T00:00:01.000Z"
    #         }
    #     }

    #     mock_queue = AsyncMock()
    #     mock_queue.get.side_effect = [event_message, asyncio.CancelledError]
    #     self.exchange._user_stream_tracker._user_stream = mock_queue

    #     try:
    #         self.async_run_with_timeout(self.exchange._user_stream_event_listener())
    #     except asyncio.CancelledError:
    #         pass

    #     failure_event: MarketOrderFailureEvent = self.order_failure_logger.event_log[0]
    #     self.assertEqual(self.exchange.current_timestamp, failure_event.timestamp)
    #     self.assertEqual(order.client_order_id, failure_event.order_id)
    #     self.assertEqual(order.order_type, failure_event.order_type)
    #     self.assertNotIn(order.client_order_id, self.exchange.in_flight_orders)
    #     self.assertTrue(order.is_failure)
    #     self.assertTrue(order.is_done)
