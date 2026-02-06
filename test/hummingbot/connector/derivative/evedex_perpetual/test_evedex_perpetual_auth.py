"""Unit tests for Evedex Perpetual authentication."""
import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from hummingbot.connector.derivative.evedex_perpetual.evedex_perpetual_auth import EvedexPerpetualAuth, to_eth_number
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSJSONRequest


class TestEvedexPerpetualAuth(unittest.TestCase):
    """
    Test suite for EvedexPerpetualAuth class.

    Evedex uses X-API-Key header for authentication as per Swagger API specification.
    """

    def setUp(self):
        """Set up test fixtures."""
        self.api_key = "test-api-key-perpetual-12345"
        self.private_key = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: mock
        self.time_provider = MagicMock()
        self.auth = EvedexPerpetualAuth(
            api_key=self.api_key,
            time_provider=self.time_provider,
            private_key=self.private_key
        )

    def test_auth_class_initialization(self):
        """Test that auth class initializes with API key and time provider."""
        self.assertEqual(self.auth._api_key, self.api_key)
        self.assertEqual(self.auth._time_provider, self.time_provider)

    def test_auth_class_initialization_with_private_key(self):
        """Test that auth class initializes wallet with private key."""
        self.assertIsNotNone(self.auth._wallet)
        self.assertEqual(len(self.auth._wallet.address), 42)  # Ethereum address length

    def test_header_for_authentication(self):
        """Test that auth headers include X-API-Key."""
        headers = self.auth.header_for_authentication()

        self.assertIn("X-API-Key", headers)
        self.assertEqual(headers["X-API-Key"], self.api_key)


class TestToEthNumber(unittest.TestCase):
    """Test suite for to_eth_number conversion function."""

    def test_to_eth_number_basic(self):
        """Test basic conversion with MATCHER_PRECISION of 8."""
        # 1.0 * 10^8 = 100000000
        result = to_eth_number(Decimal("1.0"))
        self.assertEqual(result, 100000000)

    def test_to_eth_number_with_decimals(self):
        """Test conversion with decimal values."""
        # 0.00000001 * 10^8 = 1
        result = to_eth_number(Decimal("0.00000001"))
        self.assertEqual(result, 1)

    def test_to_eth_number_large_value(self):
        """Test conversion with larger values."""
        # 50000.5 * 10^8 = 5000050000000
        result = to_eth_number(Decimal("50000.5"))
        self.assertEqual(result, 5000050000000)

    def test_to_eth_number_rounding(self):
        """Test that rounding uses ROUND_HALF_UP."""
        # 1.000000005 * 10^8 = 100000000.5 -> rounds to 100000001
        result = to_eth_number(Decimal("1.000000005"))
        self.assertEqual(result, 100000001)


class TestEvedexPerpetualAuthAsync(unittest.IsolatedAsyncioTestCase):
    """Async test suite for EvedexPerpetualAuth class."""

    def setUp(self):
        """Set up test fixtures."""
        self.api_key = "test-api-key-async-perpetual-12345"
        self.private_key = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: mock
        self.time_provider = MagicMock()
        self.auth = EvedexPerpetualAuth(
            api_key=self.api_key,
            time_provider=self.time_provider,
            private_key=self.private_key
        )

    async def test_rest_authenticate(self):
        """Test REST request authentication adds proper headers."""
        request = RESTRequest(
            method="GET",
            url="https://exchange-api.evedex.com/api/user/balance",
            headers={}
        )

        authenticated_request = await self.auth.rest_authenticate(request)

        self.assertIn("X-API-Key", authenticated_request.headers)
        self.assertEqual(authenticated_request.headers["X-API-Key"], self.api_key)

    async def test_rest_authenticate_preserves_existing_headers(self):
        """Test that authentication preserves existing headers."""
        existing_headers = {"Accept": "application/json", "Custom-Header": "custom-value"}
        request = RESTRequest(
            method="POST",
            url="https://exchange-api.evedex.com/api/v2/order/limit",
            headers=existing_headers
        )

        authenticated_request = await self.auth.rest_authenticate(request)

        # Check that existing headers are preserved
        self.assertEqual(authenticated_request.headers.get("Accept"), "application/json")
        self.assertEqual(authenticated_request.headers.get("Custom-Header"), "custom-value")
        # Check that auth header is added
        self.assertEqual(authenticated_request.headers["X-API-Key"], self.api_key)

    async def test_rest_authenticate_with_none_headers(self):
        """Test REST authentication when request has None headers."""
        request = RESTRequest(
            method="GET",
            url="https://exchange-api.evedex.com/api/position"
        )
        request.headers = None

        authenticated_request = await self.auth.rest_authenticate(request)

        self.assertIsNotNone(authenticated_request.headers)
        self.assertEqual(authenticated_request.headers["X-API-Key"], self.api_key)

    async def test_ws_authenticate_returns_request_unchanged(self):
        """Test that WS authenticate returns the request (auth is done via message)."""
        request = WSJSONRequest(payload={})
        authenticated_request = await self.auth.ws_authenticate(request)

        # WS auth is pass-through, request should be returned as-is
        self.assertEqual(request, authenticated_request)


class TestEvedexPerpetualAuthHeaderFormat(unittest.TestCase):
    """Test authentication header format matches Swagger API."""

    def test_auth_header_key_is_correct(self):
        """Test that the authentication header key is 'X-API-Key'."""
        auth = EvedexPerpetualAuth(api_key="test-key", time_provider=MagicMock())
        headers = auth.header_for_authentication()

        # Evedex uses X-API-Key header as documented in Swagger
        self.assertIn("X-API-Key", headers)
        # Should not have other auth headers like Authorization
        self.assertNotIn("Authorization", headers)

    def test_auth_header_value_is_api_key_directly(self):
        """Test that the header value is the API key directly (no Bearer prefix)."""
        api_key = "my-secret-api-key"
        auth = EvedexPerpetualAuth(api_key=api_key, time_provider=MagicMock())
        headers = auth.header_for_authentication()

        # Value should be API key directly, not "Bearer <key>"
        self.assertEqual(headers["X-API-Key"], api_key)
        self.assertNotIn("Bearer", headers["X-API-Key"])


if __name__ == "__main__":
    unittest.main()
