from unittest import TestCase

from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_utils import (
    HyperliquidPerpetualConfigMap,
    HyperliquidPerpetualTestnetConfigMap,
    validate_bool,
    validate_wallet_mode,
)


class HyperliquidPerpetualUtilsTests(TestCase):
    def test_validate_connection_mode_succeed(self):
        allowed = ('arb_wallet', 'api_wallet')
        validations = [validate_wallet_mode(value) for value in allowed]

        for index, validation in enumerate(validations):
            self.assertEqual(validation, allowed[index])

    def test_validate_connection_mode_fails(self):
        wrong_value = "api_vault"
        allowed = ('arb_wallet', 'api_wallet')

        with self.assertRaises(ValueError) as context:
            validate_wallet_mode(wrong_value)

        self.assertEqual(f"Invalid wallet mode '{wrong_value}', choose from: {allowed}", str(context.exception))

    def test_cls_validate_connection_mode_succeed(self):
        allowed = ('arb_wallet', 'api_wallet')
        validations = [HyperliquidPerpetualConfigMap.validate_mode(value) for value in allowed]

        for validation in validations:
            self.assertTrue(validation)

    def test_cls_validate_use_vault_succeed(self):
        truthy = {"yes", "y", "true", "1"}
        falsy = {"no", "n", "false", "0"}
        true_validations = [validate_bool(value) for value in truthy]
        false_validations = [validate_bool(value) for value in falsy]

        for validation in true_validations:
            self.assertTrue(validation)

        for validation in false_validations:
            self.assertFalse(validation)

    def test_cls_validate_connection_mode_fails(self):
        wrong_value = "api_vault"
        allowed = ('arb_wallet', 'api_wallet')

        with self.assertRaises(ValueError) as context:
            HyperliquidPerpetualConfigMap.validate_mode(wrong_value)

        self.assertEqual(f"Invalid wallet mode '{wrong_value}', choose from: {allowed}", str(context.exception))

    def test_cls_testnet_validate_bool_succeed(self):
        allowed = ('arb_wallet', 'api_wallet')
        validations = [HyperliquidPerpetualTestnetConfigMap.validate_mode(value) for value in allowed]

        for validation in validations:
            self.assertTrue(validation)

    def test_cls_testnet_validate_bool_fails(self):
        wrong_value = "api_vault"
        allowed = ('arb_wallet', 'api_wallet')

        with self.assertRaises(ValueError) as context:
            HyperliquidPerpetualTestnetConfigMap.validate_mode(wrong_value)

        self.assertEqual(f"Invalid wallet mode '{wrong_value}', choose from: {allowed}", str(context.exception))

    def test_validate_bool_invalid(self):
        with self.assertRaises(ValueError):
            validate_bool("maybe")

    def test_validate_bool_with_spaces(self):
        self.assertTrue(validate_bool("  YES  "))
        self.assertFalse(validate_bool("  No  "))

    def test_validate_bool_boolean_passthrough(self):
        self.assertTrue(validate_bool(True))
        self.assertFalse(validate_bool(False))

    def test_hyperliquid_address_strips_hl_prefix(self):
        corrected_address = HyperliquidPerpetualConfigMap.validate_address("HL:abcdef123")

        self.assertEqual(corrected_address, "abcdef123")

    def test_hyperliquid_testnet_address_strips_hl_prefix(self):
        corrected_address = HyperliquidPerpetualTestnetConfigMap.validate_address("HL:zzz8z8z")

        self.assertEqual(corrected_address, "zzz8z8z")


class HyperliquidPerpetualEncryptedLoadRegressionTests(TestCase):
    """
    Regression for the login crash (#7866 follow-up): config-map validation must
    NOT reject the still-encrypted secret at load time. ``load_connector_config_map_from_file``
    runs ``model_validate()`` on the raw yml (encrypted SecretStr values) and only
    decrypts afterwards, so any connect-time key check in the config map crashes
    ``Security.decrypt_all`` at startup. Key authorization now lives in the
    connector/auth layer (on the decrypted key), so loading an encrypted config
    must succeed here.
    """

    # hex of a keystore JSON blob -- exactly the shape sitting in the saved yml.
    ENCRYPTED_SECRET = '{"crypto": {"cipher": "aes-128-ctr"}, "alias": ""}'.encode().hex()

    def test_model_validate_accepts_encrypted_secret(self):
        cfg = HyperliquidPerpetualConfigMap.model_validate({
            "connector": "hyperliquid_perpetual",
            "hyperliquid_perpetual_mode": "arb_wallet",
            "use_vault": False,
            "hyperliquid_perpetual_address": self.ENCRYPTED_SECRET,
            "hyperliquid_perpetual_secret_key": self.ENCRYPTED_SECRET,
        })
        self.assertEqual(cfg.hyperliquid_perpetual_secret_key.get_secret_value(), self.ENCRYPTED_SECRET)

    def test_testnet_model_validate_accepts_encrypted_secret(self):
        cfg = HyperliquidPerpetualTestnetConfigMap.model_validate({
            "connector": "hyperliquid_perpetual_testnet",
            "hyperliquid_perpetual_testnet_mode": "arb_wallet",
            "use_vault": False,
            "hyperliquid_perpetual_testnet_address": self.ENCRYPTED_SECRET,
            "hyperliquid_perpetual_testnet_secret_key": self.ENCRYPTED_SECRET,
        })
        self.assertEqual(cfg.hyperliquid_perpetual_testnet_secret_key.get_secret_value(), self.ENCRYPTED_SECRET)
