import unittest

from hummingbot.connector.derivative.grvt_perpetual import (
    grvt_perpetual_constants as CONSTANTS,
    grvt_perpetual_web_utils as web_utils,
)


class GrvtPerpetualWebUtilsTests(unittest.TestCase):

    def test_public_and_private_rest_url(self):
        public_url = web_utils.public_rest_url(CONSTANTS.EXCHANGE_INFO_PATH_URL)
        private_url = web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL)
        auth_url = web_utils.auth_rest_url(CONSTANTS.LOGIN_PATH_URL)

        self.assertEqual(
            f"https://{CONSTANTS.REST_MARKET_DATA_SUBDOMAIN}.{CONSTANTS.BASE_DOMAIN}{CONSTANTS.REST_API_PREFIX}"
            f"{CONSTANTS.EXCHANGE_INFO_PATH_URL}",
            public_url,
        )
        self.assertEqual(
            f"https://{CONSTANTS.REST_TRADING_SUBDOMAIN}.{CONSTANTS.BASE_DOMAIN}{CONSTANTS.REST_API_PREFIX}"
            f"{CONSTANTS.ORDER_PATH_URL}",
            private_url,
        )
        self.assertEqual(
            f"https://{CONSTANTS.REST_AUTH_SUBDOMAIN}.{CONSTANTS.BASE_DOMAIN}{CONSTANTS.REST_AUTH_PREFIX}"
            f"{CONSTANTS.LOGIN_PATH_URL}",
            auth_url,
        )

    def test_rest_and_ws_urls_for_testnet_domain(self):
        domain = CONSTANTS.TESTNET_DOMAIN
        testnet_host = f"{CONSTANTS.TESTNET_PREFIX}.{CONSTANTS.BASE_DOMAIN}"

        self.assertEqual(
            f"https://{CONSTANTS.REST_TRADING_SUBDOMAIN}.{testnet_host}{CONSTANTS.REST_API_PREFIX}"
            f"{CONSTANTS.ORDER_PATH_URL}",
            web_utils.private_rest_url(CONSTANTS.ORDER_PATH_URL, domain=domain),
        )
        self.assertEqual(
            f"https://{CONSTANTS.REST_MARKET_DATA_SUBDOMAIN}.{testnet_host}{CONSTANTS.REST_API_PREFIX}"
            f"{CONSTANTS.EXCHANGE_INFO_PATH_URL}",
            web_utils.public_rest_url(CONSTANTS.EXCHANGE_INFO_PATH_URL, domain=domain),
        )
        self.assertEqual(
            f"wss://{CONSTANTS.WSS_MARKET_DATA_SUBDOMAIN}.{testnet_host}{CONSTANTS.WS_API_PREFIX}",
            web_utils.public_wss_url(domain=domain),
        )
        self.assertEqual(
            f"wss://{CONSTANTS.WSS_TRADING_SUBDOMAIN}.{testnet_host}{CONSTANTS.WS_API_PREFIX}",
            web_utils.private_wss_url(domain=domain),
        )

    def test_ws_urls(self):
        self.assertEqual(
            f"wss://{CONSTANTS.WSS_MARKET_DATA_SUBDOMAIN}.{CONSTANTS.BASE_DOMAIN}{CONSTANTS.WS_API_PREFIX}",
            web_utils.public_wss_url(),
        )
        self.assertEqual(
            f"wss://{CONSTANTS.WSS_TRADING_SUBDOMAIN}.{CONSTANTS.BASE_DOMAIN}{CONSTANTS.WS_API_PREFIX}",
            web_utils.private_wss_url(),
        )

    def test_create_throttler_uses_rate_limits(self):
        throttler = web_utils.create_throttler()
        self.assertEqual(len(CONSTANTS.RATE_LIMITS), len(throttler._rate_limits))


class GrvtPerpetualWebUtilsAsyncTests(unittest.IsolatedAsyncioTestCase):

    async def test_get_current_server_time_accepts_expected_kwargs(self):
        throttler = web_utils.create_throttler()
        server_time = await web_utils.get_current_server_time(
            throttler=throttler,
            domain=CONSTANTS.DEFAULT_DOMAIN,
        )
        self.assertIsInstance(server_time, float)
