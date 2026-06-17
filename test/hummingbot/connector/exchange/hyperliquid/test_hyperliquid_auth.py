import asyncio
import json
from typing import Awaitable
from unittest import TestCase
from unittest.mock import MagicMock, patch

from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.connector.exchange.hyperliquid import hyperliquid_constants as CONSTANTS
from hummingbot.connector.exchange.hyperliquid.hyperliquid_auth import HyperliquidAuth
from hummingbot.connector.exchange.hyperliquid.hyperliquid_exchange import HyperliquidExchange
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest


class HyperliquidAuthTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.api_address = "0x000000000000000000000000000000000000dead"
        self.api_secret = "13e56ca9cceebf1f33065c2c5376ab38570a114bc1b003b60d838f92be9d7930"  # noqa: mock
        self.connection_mode = "arb_wallet"
        self.use_vault = False
        self.trading_required = True  # noqa: mock
        self.auth = HyperliquidAuth(
            api_address=self.api_address,
            api_secret=self.api_secret,
            use_vault=self.use_vault
        )

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: int = 1):
        return asyncio.get_event_loop().run_until_complete(asyncio.wait_for(coroutine, timeout))

    def _get_timestamp(self):
        return 1678974447.926

    @patch("hummingbot.connector.exchange.hyperliquid.hyperliquid_auth._NonceManager.next_ms")
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
            },
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
        params = json.loads(request.data)
        self.assertEqual(4, len(params))
        self.assertEqual(None, params.get("vaultAddress"))
        self.assertEqual("order", params.get("action")["type"])

    @patch("hummingbot.connector.exchange.hyperliquid.hyperliquid_auth._NonceManager.next_ms")
    def test_sign_multiple_orders_has_unique_nonce(self, ts_mock: MagicMock):
        """
        Simulates signing multiple orders quickly to ensure nonce/ts uniqueness
        and prevent duplicate nonce errors.
        """
        base_params = {
            "type": "order",
            "grouping": "na",
            "orders": {
                "asset": 4,
                "isBuy": True,
                "limitPx": 1201,
                "sz": 0.01,
                "reduceOnly": False,
                "orderType": {"limit": {"tif": "Gtc"}},
            },
        }

        # simulate 2 consecutive calls with same timestamp
        ts_mock.return_value = self._get_timestamp()

        requests = []
        for idx in range(2):
            params = dict(base_params)
            params["orders"] = dict(base_params["orders"])
            params["orders"]["cloid"] = f"0x{idx:02x}"
            request = RESTRequest(
                method=RESTMethod.POST,
                url="https://test.url/exchange",
                data=json.dumps(params),
                is_auth_required=True,
            )
            self.async_run_with_timeout(self.auth.rest_authenticate(request))
            requests.append(request)

        # Verify both have unique signed content despite same timestamp
        signed_payloads = [json.loads(req.data) for req in requests]
        self.assertNotEqual(
            signed_payloads[0]["signature"], signed_payloads[1]["signature"],
            "Signatures must differ to avoid duplicate nonce issues"
        )

    @patch("hummingbot.connector.exchange.hyperliquid.hyperliquid_auth._NonceManager.next_ms")
    def test_approve_agent(self, ts_mock: MagicMock):
        ts_mock.return_value = 1234567890000

        auth = HyperliquidAuth(
            api_address=self.api_address,
            api_secret=self.api_secret,
            use_vault=self.use_vault
        )

        result = auth.approve_agent(CONSTANTS.BASE_URL)

        # --- Basic shape checks ---
        self.assertIn("action", result)
        self.assertIn("signature", result)
        self.assertIn("nonce", result)

        self.assertEqual(result["nonce"], 1234567890000)

        action = result["action"]

        # --- Action structure checks ---
        self.assertEqual(action["type"], "approveAgent")
        self.assertEqual(action["agentAddress"], self.api_address)
        self.assertEqual(action["hyperliquidChain"], "Mainnet")
        self.assertEqual(action["signatureChainId"], "0xa4b1")

        # signature must contain EIP-712 fields r/s/v
        signature = result["signature"]
        self.assertIn("r", signature)
        self.assertIn("s", signature)
        self.assertIn("v", signature)


class HyperliquidAuthWalletAuthorizationTests(TestCase):
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
            HyperliquidAuth.verify_wallet_authorized(self.KEY, wallet, post_fn, now_ms=now_ms)
        )

    def test_derive_signer_address(self):
        self.assertEqual(HyperliquidAuth.derive_signer_address(self.KEY), self.SIGNER)

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
        self.assertIn("approved agents", str(ctx.exception))

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
                HyperliquidAuth.verify_wallet_authorized(self.KEY, "not-an-address", post_fn)
            )
        self.assertIn("Invalid Hyperliquid wallet address", str(ctx.exception))


class HyperliquidExchangeWalletGuardTests(TestCase):
    """Wiring: the auth check runs through the connector's connect-time balance refresh, once, both modes."""

    KEY = "13e56ca9cceebf1f33065c2c5376ab38570a114bc1b003b60d838f92be9d7930"  # noqa: mock
    SIGNER = "0x836eE2b55d173245832995082a8600709c38D099"
    MASTER = "0x000000000000000000000000000000000000dEaD"

    def _connector(self, mode, address, use_vault=False):
        client_config_map = ClientConfigAdapter(ClientConfigMap())
        return HyperliquidExchange(
            client_config_map,
            hyperliquid_address=address,
            hyperliquid_secret_key=self.KEY,
            hyperliquid_mode=mode,
            use_vault=use_vault,
            trading_pairs=["BTC-USDC"],
            trading_required=False,  # how `connect` builds the connector
        )

    @staticmethod
    def _patch_api_post(connector, agents):
        async def fake_api_post(path_url=None, data=None, **kwargs):
            if data and data.get("type") == "extraAgents":
                fake_api_post.extra_agents_calls += 1
                return agents
            return {"balances": []}
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
