"""Unit tests for Evedex web utilities module."""
import unittest
from unittest.mock import MagicMock

from hummingbot.connector.exchange.evedex import evedex_constants as CONSTANTS, evedex_web_utils as web_utils
from hummingbot.core.web_assistant.connections.data_types import RESTRequest


class TestEvedexWebUtils(unittest.TestCase):
    """Test suite for Evedex web utility functions."""

    def test_public_rest_url(self):
        """Test public REST URL construction."""
        path = "/api/market/instrument"
        url = web_utils.public_rest_url(path)

        expected = f"{CONSTANTS.REST_URL}{path}"
        self.assertEqual(url, expected)

    def test_public_rest_url_with_domain(self):
        """Test public REST URL with domain parameter (should be ignored for Evedex)."""
        path = "/api/market/instrument"
        url = web_utils.public_rest_url(path, domain="test_domain")

        # Domain is ignored, URL should be same as without domain
        expected = f"{CONSTANTS.REST_URL}{path}"
        self.assertEqual(url, expected)

    def test_private_rest_url(self):
        """Test private REST URL construction."""
        path = "/api/user/balance"
        url = web_utils.private_rest_url(path)

        expected = f"{CONSTANTS.REST_URL}{path}"
        self.assertEqual(url, expected)

    def test_private_rest_url_with_domain(self):
        """Test private REST URL with domain parameter."""
        path = "/api/user/balance"
        url = web_utils.private_rest_url(path, domain="test_domain")

        expected = f"{CONSTANTS.REST_URL}{path}"
        self.assertEqual(url, expected)

    def test_wss_url(self):
        """Test WebSocket URL."""
        url = web_utils.wss_url()
        self.assertEqual(url, CONSTANTS.WSS_URL)

    def test_wss_url_with_domain(self):
        """Test WebSocket URL with domain parameter."""
        url = web_utils.wss_url(domain="test_domain")
        self.assertEqual(url, CONSTANTS.WSS_URL)

    def test_create_throttler(self):
        """Test throttler creation with rate limits."""
        throttler = web_utils.create_throttler()

        self.assertIsNotNone(throttler)
        # Throttler should be configured with RATE_LIMITS from constants

    def test_build_api_factory(self):
        """Test API factory creation."""
        api_factory = web_utils.build_api_factory()

        self.assertIsNotNone(api_factory)
        # API factory should have REST pre-processors

    def test_build_api_factory_with_auth(self):
        """Test API factory creation with authentication."""
        mock_auth = MagicMock()
        api_factory = web_utils.build_api_factory(auth=mock_auth)

        self.assertIsNotNone(api_factory)

    def test_build_api_factory_without_time_synchronizer(self):
        """Test API factory creation without time synchronizer."""
        throttler = web_utils.create_throttler()
        api_factory = web_utils.build_api_factory_without_time_synchronizer_pre_processor(throttler)

        self.assertIsNotNone(api_factory)


class TestEvedexRESTPreProcessor(unittest.IsolatedAsyncioTestCase):
    """Test suite for EvedexRESTPreProcessor."""

    async def test_pre_process_adds_content_type(self):
        """Test that pre-processor adds Content-Type header."""
        pre_processor = web_utils.EvedexRESTPreProcessor()
        request = RESTRequest(
            method="GET",
            url="https://exchange-api.evedex.com/api/market/instrument"
        )

        processed_request = await pre_processor.pre_process(request)

        self.assertIn("Content-Type", processed_request.headers)
        self.assertEqual(processed_request.headers["Content-Type"], "application/json")

    async def test_pre_process_preserves_existing_headers(self):
        """Test that pre-processor preserves existing headers."""
        pre_processor = web_utils.EvedexRESTPreProcessor()
        request = RESTRequest(
            method="GET",
            url="https://exchange-api.evedex.com/api/market/instrument",
            headers={"X-Custom-Header": "custom-value"}
        )

        processed_request = await pre_processor.pre_process(request)

        self.assertEqual(processed_request.headers["X-Custom-Header"], "custom-value")
        self.assertEqual(processed_request.headers["Content-Type"], "application/json")

    async def test_pre_process_with_none_headers(self):
        """Test that pre-processor handles None headers."""
        pre_processor = web_utils.EvedexRESTPreProcessor()
        request = RESTRequest(
            method="POST",
            url="https://exchange-api.evedex.com/api/v2/order/limit"
        )
        request.headers = None

        processed_request = await pre_processor.pre_process(request)

        self.assertIsNotNone(processed_request.headers)
        self.assertEqual(processed_request.headers["Content-Type"], "application/json")


class TestEvedexServerTime(unittest.IsolatedAsyncioTestCase):
    """Test suite for server time functions."""

    async def test_get_current_server_time(self):
        """Test getting current server time (uses local time for Evedex)."""
        import time

        before = time.time()
        server_time = await web_utils.get_current_server_time()
        after = time.time()

        # Server time should be between before and after
        self.assertGreaterEqual(server_time, before)
        self.assertLessEqual(server_time, after)

    async def test_get_current_server_time_with_throttler(self):
        """Test getting server time with throttler parameter."""
        throttler = web_utils.create_throttler()
        server_time = await web_utils.get_current_server_time(throttler=throttler)

        self.assertIsInstance(server_time, float)
        self.assertGreater(server_time, 0)


class TestEvedexURLConstants(unittest.TestCase):
    """Test URL constants match official Evedex API."""

    def test_rest_url(self):
        """Test REST base URL."""
        self.assertEqual(CONSTANTS.REST_URL, "https://exchange-api.evedex.com")

    def test_wss_url(self):
        """Test WebSocket URL (Centrifugo)."""
        self.assertEqual(CONSTANTS.WSS_URL, "wss://ws.evedex.com/connection/websocket")

    def test_api_paths(self):
        """Test API path constants match Swagger API."""
        # Market endpoints
        self.assertEqual(CONSTANTS.INSTRUMENTS_PATH_URL, "/api/market/instrument")
        self.assertEqual(CONSTANTS.ORDER_BOOK_PATH_URL, "/api/market/{instrument}/deep")

        # Order endpoints
        self.assertEqual(CONSTANTS.LIMIT_ORDER_PATH_URL, "/api/v2/order/limit")
        self.assertEqual(CONSTANTS.MARKET_ORDER_PATH_URL, "/api/v2/order/market")

        # User endpoints
        self.assertEqual(CONSTANTS.USER_BALANCE_PATH_URL, "/api/market/available-balance")
        self.assertEqual(CONSTANTS.USER_ME_PATH_URL, "/api/user/me")


class TestIsExchangeInformationValid(unittest.TestCase):
    """Test suite for is_exchange_information_valid function."""

    def test_valid_exchange_info(self):
        """Test valid exchange info returns True."""
        valid_info = {
            "name": "BTC-USDT",
            "from": "BTC",
            "to": "USDT",
            "isActive": True,
            "trading": "all"
        }
        self.assertTrue(web_utils.is_exchange_information_valid(valid_info))

    def test_valid_exchange_info_with_instrument_field(self):
        """Test valid exchange info with 'instrument' instead of 'name'."""
        valid_info = {
            "instrument": "BTC-USDT",
            "from": "BTC",
            "to": "USDT",
            "isActive": True,
        }
        self.assertTrue(web_utils.is_exchange_information_valid(valid_info))

    def test_valid_exchange_info_trading_not_set(self):
        """Test valid exchange info when trading field is not set (default allowed)."""
        valid_info = {
            "name": "ETH-USDT",
            "from": "ETH",
            "to": "USDT",
            "isActive": True,
        }
        self.assertTrue(web_utils.is_exchange_information_valid(valid_info))

    def test_invalid_exchange_info_not_active(self):
        """Test invalid exchange info when isActive is False."""
        invalid_info = {
            "name": "BTC-USDT",
            "from": "BTC",
            "to": "USDT",
            "isActive": False,
        }
        self.assertFalse(web_utils.is_exchange_information_valid(invalid_info))

    def test_invalid_exchange_info_trading_restricted(self):
        """Test invalid exchange info when trading is restricted."""
        invalid_info = {
            "name": "BTC-USDT",
            "from": "BTC",
            "to": "USDT",
            "isActive": True,
            "trading": "restricted"
        }
        self.assertFalse(web_utils.is_exchange_information_valid(invalid_info))

    def test_invalid_exchange_info_missing_name(self):
        """Test invalid exchange info when name/instrument is missing."""
        invalid_info = {
            "from": "BTC",
            "to": "USDT",
            "isActive": True,
        }
        self.assertFalse(web_utils.is_exchange_information_valid(invalid_info))

    def test_invalid_exchange_info_missing_from(self):
        """Test invalid exchange info when from field is missing."""
        invalid_info = {
            "name": "BTC-USDT",
            "to": "USDT",
            "isActive": True,
        }
        self.assertFalse(web_utils.is_exchange_information_valid(invalid_info))

    def test_invalid_exchange_info_missing_to(self):
        """Test invalid exchange info when to field is missing."""
        invalid_info = {
            "name": "BTC-USDT",
            "from": "BTC",
            "isActive": True,
        }
        self.assertFalse(web_utils.is_exchange_information_valid(invalid_info))


class TestURLFormatting(unittest.TestCase):
    """Test URL formatting for various API calls."""

    def test_order_book_url_format(self):
        """Test order book URL formatting with instrument parameter."""
        instrument = "BTC-USDT"
        path = CONSTANTS.ORDER_BOOK_PATH_URL.format(instrument=instrument)
        url = web_utils.public_rest_url(path)

        expected = f"{CONSTANTS.REST_URL}/api/market/{instrument}/deep"
        self.assertEqual(url, expected)

    def test_cancel_order_url_format(self):
        """Test cancel order URL formatting with orderId parameter."""
        order_id = "00001:00000000000000000000000001"
        path = CONSTANTS.CANCEL_ORDER_PATH_URL.format(orderId=order_id)
        url = web_utils.private_rest_url(path)

        expected = f"{CONSTANTS.REST_URL}/api/order/{order_id}"
        self.assertEqual(url, expected)

    def test_get_order_url_format(self):
        """Test get order URL formatting with orderId parameter."""
        order_id = "00001:00000000000000000000000001"
        path = CONSTANTS.GET_ORDER_PATH_URL.format(orderId=order_id)
        url = web_utils.private_rest_url(path)

        expected = f"{CONSTANTS.REST_URL}/api/order/{order_id}"
        self.assertEqual(url, expected)


if __name__ == "__main__":
    unittest.main()
