from unittest import TestCase

from hummingbot.connector.exchange.hyperliquid.hyperliquid_utils import (
    HyperliquidConfigMap,
    HyperliquidTestnetConfigMap,
    validate_bool,
    validate_wallet_mode,
)


class HyperliquidUtilsTests(TestCase):
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
        validations = [HyperliquidConfigMap.validate_mode(value) for value in allowed]

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
            HyperliquidConfigMap.validate_mode(wrong_value)

        self.assertEqual(f"Invalid wallet mode '{wrong_value}', choose from: {allowed}", str(context.exception))

    def test_cls_testnet_validate_bool_succeed(self):
        allowed = ('arb_wallet', 'api_wallet')
        validations = [HyperliquidTestnetConfigMap.validate_mode(value) for value in allowed]

        for validation in validations:
            self.assertTrue(validation)

    def test_cls_testnet_validate_bool_fails(self):
        wrong_value = "api_vault"
        allowed = ('arb_wallet', 'api_wallet')

        with self.assertRaises(ValueError) as context:
            HyperliquidTestnetConfigMap.validate_mode(wrong_value)

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
        corrected_address = HyperliquidConfigMap.validate_address("HL:abcdef123")

        self.assertEqual(corrected_address, "abcdef123")

    def test_hyperliquid_testnet_address_strips_hl_prefix(self):
        corrected_address = HyperliquidTestnetConfigMap.validate_address("HL:zzz8z8z")

        self.assertEqual(corrected_address, "zzz8z8z")


class HyperliquidKeyValidationTests(TestCase):
    """
    Regression coverage for issue #7866 on the spot connector: connect-time
    config-map validation of the private key (the connect flow constructs the
    connector with trading_required=False, so auth-class validation never ran).
    """

    SECRET = "13e56ca9cceebf1f33065c2c5376ab38570a114bc1b003b60d838f92be9d7930"  # noqa: mock
    GOOD_ADDRESS = "0x836eE2b55d173245832995082a8600709c38D099"  # noqa: mock
    WRONG_ADDRESS = "0x836eE2b55d173245832995082a8600709c38D098"  # noqa: mock

    def _build(self, **overrides):
        params = dict(
            connector="hyperliquid",
            hyperliquid_mode="arb_wallet",
            use_vault=False,
            hyperliquid_address=self.GOOD_ADDRESS,
            hyperliquid_secret_key=self.SECRET,
        )
        params.update(overrides)
        return HyperliquidConfigMap(**params)

    def test_random_private_key_rejected_at_connect(self):
        with self.assertRaises(Exception) as ctx:
            self._build(hyperliquid_secret_key="not-a-real-private-key")
        self.assertIn("private key", str(ctx.exception).lower())

    def test_empty_private_key_rejected(self):
        with self.assertRaises(Exception) as ctx:
            self._build(hyperliquid_secret_key="")
        self.assertIn("non-empty", str(ctx.exception).lower())

    def test_valid_key_wrong_address_rejected(self):
        with self.assertRaises(Exception) as ctx:
            self._build(hyperliquid_address=self.WRONG_ADDRESS)
        self.assertIn("does not derive", str(ctx.exception).lower())

    def test_matching_key_address_pair_accepted(self):
        cfg = self._build()
        self.assertEqual(cfg.hyperliquid_secret_key.get_secret_value(), self.SECRET)

    def test_api_wallet_mode_bypasses_address_match(self):
        cfg = self._build(hyperliquid_mode="api_wallet",
                          hyperliquid_address=self.WRONG_ADDRESS)
        self.assertEqual(cfg.hyperliquid_mode, "api_wallet")

    def test_vault_mode_bypasses_address_match(self):
        cfg = self._build(use_vault=True, hyperliquid_address=self.WRONG_ADDRESS)
        self.assertTrue(cfg.use_vault)

    def test_testnet_random_private_key_rejected(self):
        with self.assertRaises(Exception) as ctx:
            HyperliquidTestnetConfigMap(
                connector="hyperliquid_testnet",
                hyperliquid_testnet_mode="arb_wallet",
                use_vault=False,
                hyperliquid_testnet_address=self.GOOD_ADDRESS,
                hyperliquid_testnet_secret_key="garbage",
            )
        self.assertIn("private key", str(ctx.exception).lower())
