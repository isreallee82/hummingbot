"""Unit tests for Evedex authentication.

Based on Evedex Swagger API specification:
- Authentication uses X-API-Key header
- Security scheme: ApiKey (apiKey in header)
"""
import asyncio
from unittest import TestCase
from unittest.mock import MagicMock

from typing_extensions import Awaitable

from hummingbot.connector.exchange.evedex.evedex_auth import EvedexAuth
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest


class EvedexAuthTests(TestCase):
    """Test suite for EvedexAuth class based on Evedex API authentication requirements.

    Swagger API Security Scheme:
    - type: apiKey
    - in: header
    - name: X-API-Key
    """

    def setUp(self):
        """Set up test fixtures."""
        self._api_key = "test-api-key-12345"

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: float = 1):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(asyncio.wait_for(coroutine, timeout))

    def test_auth_class_initialization(self):
        """Test that auth class initializes with API key."""
        auth = EvedexAuth(api_key=self._api_key)
        self.assertEqual(auth._api_key, self._api_key)

    def test_header_for_authentication(self):
        """Test that auth headers include X-API-Key as per Evedex Swagger API specification.

        From Swagger:
        securitySchemes:
          ApiKey:
            type: apiKey
            in: header
            name: X-API-Key
        """
        auth = EvedexAuth(api_key=self._api_key)
        headers = auth.header_for_authentication()

        self.assertIn("X-API-Key", headers)
        self.assertEqual(headers["X-API-Key"], self._api_key)
        self.assertIn("Content-Type", headers)
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_rest_authenticate_get_request(self):
        """Test REST GET request authentication adds proper headers.

        Example endpoint: GET /api/user/balance
        """
        auth = EvedexAuth(api_key=self._api_key)
        request = RESTRequest(
            method=RESTMethod.GET,
            url="https://exchange-api.evedex.com/api/user/balance",
            headers={},
            is_auth_required=True
        )

        authenticated_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        self.assertIn("X-API-Key", authenticated_request.headers)
        self.assertEqual(authenticated_request.headers["X-API-Key"], self._api_key)

    def test_rest_authenticate_post_request(self):
        """Test REST POST request authentication for order placement.

        Example endpoint: POST /api/v2/order/limit
        Required fields: instrument, side, quantity, limitPrice, signature, chainId, id
        """
        auth = EvedexAuth(api_key=self._api_key)
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://exchange-api.evedex.com/api/v2/order/limit",
            headers={"Accept": "application/json"},
            is_auth_required=True,
            data='{"instrument": "BTC-USDT", "side": "BUY", "quantity": "0.01"}'
        )

        authenticated_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        self.assertIn("X-API-Key", authenticated_request.headers)
        self.assertEqual(authenticated_request.headers["X-API-Key"], self._api_key)
        # Check that existing headers are preserved
        self.assertEqual(authenticated_request.headers.get("Accept"), "application/json")

    def test_rest_authenticate_delete_request(self):
        """Test REST DELETE request authentication for order cancellation.

        Example endpoint: DELETE /api/order/{orderId}
        """
        auth = EvedexAuth(api_key=self._api_key)
        request = RESTRequest(
            method=RESTMethod.DELETE,
            url="https://exchange-api.evedex.com/api/order/test-order-id",
            headers={},
            is_auth_required=True
        )

        authenticated_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        self.assertIn("X-API-Key", authenticated_request.headers)
        self.assertEqual(authenticated_request.headers["X-API-Key"], self._api_key)

    def test_rest_authenticate_preserves_existing_headers(self):
        """Test that authentication preserves existing headers."""
        auth = EvedexAuth(api_key=self._api_key)
        existing_headers = {"Accept": "application/json", "Custom-Header": "custom-value"}
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://exchange-api.evedex.com/api/v2/order/limit",
            headers=existing_headers,
            is_auth_required=True
        )

        authenticated_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        # Check that existing headers are preserved
        self.assertEqual(authenticated_request.headers.get("Accept"), "application/json")
        self.assertEqual(authenticated_request.headers.get("Custom-Header"), "custom-value")
        # Check that auth header is added
        self.assertEqual(authenticated_request.headers["X-API-Key"], self._api_key)

    def test_auth_with_time_provider(self):
        """Test auth initialization with time provider."""
        time_provider = MagicMock()
        time_provider.time.return_value = 1234567890.0

        auth = EvedexAuth(api_key=self._api_key, time_provider=time_provider)

        self.assertEqual(auth._api_key, self._api_key)
