import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_auth import GrvtPerpetualAuth
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest


class GrvtPerpetualAuthTests(unittest.IsolatedAsyncioTestCase):

    async def test_rest_authenticate_adds_required_headers(self):
        auth = GrvtPerpetualAuth(session_cookie="session=abc", account_id="123")
        request = RESTRequest(
            method=RESTMethod.GET,
            url="https://api-staging.grvt.io/trading/v1/order",
            headers={"X-Test": "1"},
        )

        authenticated = await auth.rest_authenticate(request)

        self.assertEqual("session=abc", authenticated.headers["Cookie"])
        self.assertEqual("123", authenticated.headers["X-Grvt-Account-Id"])
        self.assertEqual("1", authenticated.headers["X-Test"])

    def test_build_create_order_payload_uses_provided_signature(self):
        auth = GrvtPerpetualAuth(session_cookie="session=abc", account_id="123")
        payload = auth.build_create_order_payload(
            order={
                "sub_account_id": "8289849667772468",
                "is_market": False,
                "time_in_force": "GOOD_TILL_TIME",
                "post_only": False,
                "reduce_only": False,
                "legs": [{
                    "instrument": "BTC_USDT_Perp",
                    "size": "1.013",
                    "limit_price": "68900.5",
                    "is_buying_asset": False,
                }],
                "signature": {
                    "expiration": "1730800479321350000",
                    "nonce": 828700936,
                },
                "metadata": {"client_order_id": "HBOT-1"},
            },
            instrument_data={"instrument_hash": "0x030501", "base_decimals": 9},
            provided_signature={"r": "0x1", "s": "0x2", "v": 27, "expiration": "1", "nonce": 1, "signer": "0x0"},
        )

        self.assertIn("order", payload)
        self.assertIn("signature", payload["order"])
        self.assertEqual("BTC_USDT_Perp", payload["order"]["legs"][0]["instrument"])
        self.assertEqual("0x1", payload["order"]["signature"]["r"])

    def test_sign_order_payload_raises_without_private_key(self):
        auth = GrvtPerpetualAuth(
            session_cookie="session=abc",
            account_id="123",
            evm_private_key="",
        )

        with self.assertRaises(ValueError):
            auth.sign_order_payload(
                order={
                    "sub_account_id": "8289849667772468",
                    "is_market": False,
                    "time_in_force": "GOOD_TILL_TIME",
                    "post_only": False,
                    "reduce_only": False,
                    "legs": [{
                        "instrument": "BTC_USDT_Perp",
                        "size": "1.013",
                        "limit_price": "68900.5",
                        "is_buying_asset": False,
                    }],
                    "signature": {
                        "expiration": "1730800479321350000",
                        "nonce": 828700936,
                    },
                    "metadata": {"client_order_id": "HBOT-1"},
                },
                instrument_data={"instrument_hash": "0x030501", "base_decimals": 9},
            )

    def test_sign_order_payload_matches_known_signature(self):
        auth = GrvtPerpetualAuth(
            session_cookie="session=abc",
            account_id="123",
            evm_private_key="f7934647276a6e1fa0af3f4467b4b8ddaf45d25a7368fa1a295eef49a446819d",  # noqa: mock
            domain="grvt_perpetual_testnet",
        )
        signature = auth.sign_order_payload(
            order={
                "sub_account_id": "8289849667772468",
                "is_market": False,
                "time_in_force": "GOOD_TILL_TIME",
                "post_only": False,
                "reduce_only": False,
                "legs": [{
                    "instrument": "BTC_USDT_Perp",
                    "size": "1.013",
                    "limit_price": "68900.5",
                    "is_buying_asset": False,
                }],
                "signature": {
                    "expiration": "1730800479321350000",
                    "nonce": 828700936,
                },
                "metadata": {"client_order_id": "1"},
            },
            instrument_data={"instrument_hash": "0x030501", "base_decimals": 9},
        )

        self.assertEqual("0xb00512d986a718b15136a8ba23de1c1ec84bbdb9958629cbbe4909bae620bb04", signature["r"])  # noqa: mock
        self.assertEqual("0x79f706de61c68cc14d7734594b5d8689df2b2a7b25951f9a3f61d799f4327ffc", signature["s"])  # noqa: mock
        self.assertEqual(28, signature["v"])
        self.assertNotIn("chain_id", signature)

    @patch("hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_auth.Account.from_key")
    @patch("hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_auth.Account.sign_message")
    def test_sign_order_payload_normalizes_recovery_id_to_27_28(self, mock_sign_message, mock_from_key):
        auth = GrvtPerpetualAuth(
            session_cookie="session=abc",
            account_id="123",
            evm_private_key="f7934647276a6e1fa0af3f4467b4b8ddaf45d25a7368fa1a295eef49a446819d",  # noqa: mock
            domain="grvt_perpetual_testnet",
        )
        mock_from_key.return_value = SimpleNamespace(address="0xabc")
        mock_sign_message.return_value = SimpleNamespace(r=1, s=2, v=1)

        signature = auth.sign_order_payload(
            order={
                "sub_account_id": "8289849667772468",
                "is_market": False,
                "time_in_force": "GOOD_TILL_TIME",
                "post_only": False,
                "reduce_only": False,
                "legs": [{
                    "instrument": "BTC_USDT_Perp",
                    "size": "1.013",
                    "limit_price": "68900.5",
                    "is_buying_asset": False,
                }],
                "signature": {
                    "expiration": "1730800479321350000",
                    "nonce": 828700936,
                },
                "metadata": {"client_order_id": "1"},
            },
            instrument_data={"instrument_hash": "0x030501", "base_decimals": 9},
        )

        self.assertEqual(28, signature["v"])
        self.assertNotIn("chain_id", signature)
