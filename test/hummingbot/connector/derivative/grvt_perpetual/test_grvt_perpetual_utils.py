import unittest

from hummingbot.connector.derivative.grvt_perpetual import grvt_perpetual_utils as utils


class GrvtPerpetualUtilsTests(unittest.TestCase):

    def test_symbol_normalization(self):
        self.assertEqual("btc-usdt", utils.normalize_instrument("BTC_USDT_Perp"))
