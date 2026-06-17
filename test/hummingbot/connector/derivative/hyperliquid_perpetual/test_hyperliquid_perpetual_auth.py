import asyncio
import json
from typing import Awaitable
from unittest import TestCase
from unittest.mock import MagicMock, patch

from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_auth import HyperliquidPerpetualAuth
from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_derivative import (
    HyperliquidPerpetualDerivative,
)
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest


class HyperliquidPerpetualAuthTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.api_address = "testApiAddress"
        self.api_secret = "13e56ca9cceebf1f33065c2c5376ab38570a114bc1b003b60d838f92be9d7930"  # noqa: mock
        self.connection_mode = "arb_wallet"
        self.use_vault = False
        self.trading_required = True  # noqa: mock
        self.auth = HyperliquidPerpetualAuth(
            api_address=self.api_address,
            api_secret=self.api_secret,
            use_vault=self.use_vault
        )

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: int = 1):
        ret = asyncio.get_event_loop().run_until_complete(asyncio.wait_for(coroutine, timeout))
        return ret

    def _get_timestamp(self):
        return 1678974447.926

    @patch(
        "hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_auth.HyperliquidPerpetualAuth._get_timestamp")
    def test_sign_order_params_post_request(self, ts_mock: MagicMock):
        params = {
            "type": "order",
            "grouping": "na",
            "orders": {
                "asset": 4,
                "isBuy": True,
                "limitPx": 1201,
                "sz": 0.01,
                "reduceOnly": False,
                "orderType": {"limit": {"tif": "Gtc"}},
                "cloid": "0x000000000000000000000000000ee056",
            }
        }
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://test.url/exchange",
            data=json.dumps(params),
            is_auth_required=True,
        )
        timestamp = self._get_timestamp()
        ts_mock.return_value = timestamp

        self.async_run_with_timeout(self.auth.rest_authenticate(request))
        # raw_signature = f'/linear/v1/orders&one=1&timestamp={int(self._get_timestamp() * 1e3)}'
        # expected_signature = hmac.new(bytes(self.secret_key.encode("utf-8")),
        #                               raw_signature.encode("utf-8"),
        #                               hashlib.sha256).hexdigest()

        params = json.loads(request.data)
        self.assertEqual(4, len(params))
        self.assertEqual(None, params.get("vaultAddress"))
        self.assertEqual("order", params.get("action")["type"])


class HyperliquidPerpetualAuthWalletAuthorizationTests(TestCase):
    """
    Mode-agnostic authorization check (#7866): a key is authorized if it derives
    to the wallet (arb_wallet) or is an approved agent of it (api_wallet).
    """

    KEY = "13e56ca9cceebf1f33065c2c5376ab38570a114bc1b003b60d838f92be9d7930"  # noqa: mock
    SIGNER = "0x836eE2b55d173245832995082a8600709c38D099"  # address KEY controls
    MASTER = "0x000000000000000000000000000000000000dEaD"  # an unrelated main wallet
    FUTURE_MS = 9_999_999_999_000
    NOW_MS = 1_700_000_000_000

    def _run(self, wallet, agents, now_ms=NOW_MS, expect_post=True):
        async def post_fn(body):
            if not expect_post:
                raise AssertionError("extraAgents must not be queried for an arb_wallet owner key")
            assert body["type"] == "extraAgents"
            assert body["user"] == self.MASTER
            return agents
        return asyncio.get_event_loop().run_until_complete(
            HyperliquidPerpetualAuth.verify_wallet_authorized(self.KEY, wallet, post_fn, now_ms=now_ms)
        )

    def test_derive_signer_address(self):
        self.assertEqual(HyperliquidPerpetualAuth.derive_signer_address(self.KEY), self.SIGNER)

    def test_owner_key_passes_offline(self):
        entry = self._run(self.SIGNER, agents=None, expect_post=False)
        self.assertTrue(entry["owner"])

    def test_approved_agent_passes(self):
        entry = self._run(self.MASTER, [{"address": self.SIGNER, "name": "hbot", "validUntil": self.FUTURE_MS}])
        self.assertEqual(entry["name"], "hbot")

    def test_approved_agent_case_insensitive(self):
        entry = self._run(self.MASTER, [{"address": self.SIGNER.lower(), "validUntil": self.FUTURE_MS}])
        self.assertIsNotNone(entry)

    def test_unapproved_key_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(self.MASTER, [{"address": "0x000000000000000000000000000000000000bEEF", "validUntil": self.FUTURE_MS}])
        self.assertIn("neither", str(ctx.exception))

    def test_empty_agent_list_rejected(self):
        with self.assertRaises(ValueError):
            self._run(self.MASTER, [])

    def test_expired_agent_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(self.MASTER, [{"address": self.SIGNER, "validUntil": self.NOW_MS - 1}])
        self.assertIn("expired", str(ctx.exception))

    def test_invalid_wallet_address_rejected(self):
        async def post_fn(body):
            raise AssertionError("should not reach the network for an invalid address")
        with self.assertRaises(ValueError) as ctx:
            asyncio.get_event_loop().run_until_complete(
                HyperliquidPerpetualAuth.verify_wallet_authorized(self.KEY, "not-an-address", post_fn)
            )
        self.assertIn("Invalid Hyperliquid wallet address", str(ctx.exception))

    def test_malformed_key_rejected(self):
        async def post_fn(body):
            raise AssertionError("should not reach the network for a malformed key")
        with self.assertRaises(ValueError) as ctx:
            asyncio.get_event_loop().run_until_complete(
                HyperliquidPerpetualAuth.verify_wallet_authorized("not-a-real-key", self.SIGNER, post_fn)
            )
        self.assertIn("private key", str(ctx.exception).lower())


class HyperliquidPerpetualWalletGuardTests(TestCase):
    """Wiring: the auth check runs through the connector's connect-time balance refresh, once, both modes."""

    KEY = "13e56ca9cceebf1f33065c2c5376ab38570a114bc1b003b60d838f92be9d7930"  # noqa: mock
    SIGNER = "0x836eE2b55d173245832995082a8600709c38D099"
    MASTER = "0x000000000000000000000000000000000000dEaD"

    def _connector(self, mode, address, use_vault=False):
        client_config_map = ClientConfigAdapter(ClientConfigMap())
        return HyperliquidPerpetualDerivative(
            client_config_map,
            hyperliquid_perpetual_address=address,
            hyperliquid_perpetual_secret_key=self.KEY,
            hyperliquid_perpetual_mode=mode,
            use_vault=use_vault,
            trading_pairs=["BTC-USD"],
            trading_required=False,  # how `connect` builds the connector
        )

    @staticmethod
    def _patch_api_post(connector, agents):
        async def fake_api_post(path_url=None, data=None, **kwargs):
            if data and data.get("type") == "extraAgents":
                fake_api_post.extra_agents_calls += 1
                return agents
            return {}
        fake_api_post.extra_agents_calls = 0
        connector._api_post = fake_api_post
        return fake_api_post

    def test_arb_wallet_owner_key_passes_without_network(self):
        connector = self._connector(mode="arb_wallet", address=self.SIGNER)
        fake = self._patch_api_post(connector, agents=[])
        asyncio.get_event_loop().run_until_complete(connector._verify_wallet_authorized_once())
        self.assertEqual(fake.extra_agents_calls, 0)
        self.assertTrue(connector._api_wallet_verified)

    def test_api_wallet_approved_agent_verified_once(self):
        connector = self._connector(mode="api_wallet", address=self.MASTER)
        fake = self._patch_api_post(connector, agents=[{"address": self.SIGNER, "validUntil": 9_999_999_999_000}])
        loop = asyncio.get_event_loop()
        loop.run_until_complete(connector._verify_wallet_authorized_once())
        loop.run_until_complete(connector._verify_wallet_authorized_once())
        self.assertEqual(fake.extra_agents_calls, 1)  # once-flag

    def test_unapproved_key_fails_at_connect(self):
        # The guard runs at the top of _update_balances, before any balance parsing.
        connector = self._connector(mode="api_wallet", address=self.MASTER)
        self._patch_api_post(connector, agents=[])
        with self.assertRaises(ValueError) as ctx:
            asyncio.get_event_loop().run_until_complete(connector._update_balances())
        self.assertIn("neither", str(ctx.exception))

    def test_vault_mode_skips_check(self):
        connector = self._connector(mode="arb_wallet", address=self.MASTER, use_vault=True)
        fake = self._patch_api_post(connector, agents=[])
        asyncio.get_event_loop().run_until_complete(connector._verify_wallet_authorized_once())
        self.assertEqual(fake.extra_agents_calls, 0)
