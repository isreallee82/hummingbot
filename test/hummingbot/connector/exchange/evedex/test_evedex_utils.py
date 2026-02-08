import unittest

from hummingbot.connector.exchange.evedex import evedex_utils


class TestEvedexUtils(unittest.TestCase):
    def test_build_api_factory_config_map(self):
        config_map = evedex_utils.build_api_factory_config_map()
        self.assertIn("evedex_api_key", config_map)
        self.assertIn("evedex_private_key", config_map)


if __name__ == "__main__":
    unittest.main()
