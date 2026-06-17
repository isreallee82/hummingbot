from decimal import Decimal
from typing import Literal, Optional

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

# Maker rebates(-0.02%) are paid out continuously on each trade directly to the trading wallet.(https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)
DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0"),
    taker_percent_fee_decimal=Decimal("0.00025"),
    buy_percent_fee_deducted_from_returns=True
)

CENTRALIZED = True

EXAMPLE_PAIR = "BTC-USD"

BROKER_ID = "HBOT"


def validate_wallet_mode(value: str) -> Optional[str]:
    """
    Check if the value is a valid mode
    """
    allowed = ('arb_wallet', 'api_wallet')

    if isinstance(value, str):
        formatted_value = value.strip().lower()

        if formatted_value in allowed:
            return formatted_value

    raise ValueError(f"Invalid wallet mode '{value}', choose from: {allowed}")


def validate_bool(value: str) -> Optional[str]:
    """
    Permissively interpret a string as a boolean
    """
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        formatted_value = value.strip().lower()
        truthy = {"yes", "y", "true", "1"}
        falsy = {"no", "n", "false", "0"}

        if formatted_value in truthy:
            return True
        if formatted_value in falsy:
            return False

    raise ValueError(f"Invalid value, please choose value from {truthy.union(falsy)}")


def _unwrap_secret(value) -> Optional[str]:
    """
    Return the (stripped) plain string for a SecretStr/str input.

    Returns ``None`` for a value that has not been provided yet (the ``...``
    placeholder left by ``model_construct`` during field-by-field prompting, or
    a non-string). An explicitly empty string is returned as ``""`` so callers
    can distinguish "not provided yet" (defer) from "provided but empty" (reject).
    """
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if not isinstance(value, str):
        return None
    return value.strip()


def validate_private_key_format(value):
    """
    Field-level validator: ensure the supplied private key is a non-empty string
    that ``eth_account`` can parse. Runs at connect time (config-map validation),
    which the auth-class validation does not because the connector is constructed
    with ``trading_required=False`` during ``connect`` (issue #7866 / PR #8212).
    """
    secret = _unwrap_secret(value)
    if secret is None:
        # Not yet provided (field-by-field prompting / model_construct); defer.
        return value
    if secret == "":
        raise ValueError("Hyperliquid private key must be a non-empty string.")
    import eth_account
    try:
        eth_account.Account.from_key(secret)
    except ValueError:
        raise ValueError(
            "Invalid Hyperliquid private key: not a valid 32-byte hex private key."
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError(f"Invalid Hyperliquid private key format: {exc}") from exc
    return value


def validate_key_address_pair(mode: str, use_vault: bool, address, secret) -> None:
    """
    Model-level validator: in ``arb_wallet`` mode without a vault, the private key
    MUST derive to the supplied Arbitrum wallet address. This is the check that
    catches the silent-wrong-key bug (#7866) at ``connect`` time. Skipped for
    ``api_wallet`` (agent key by design does not derive to the trading address)
    and for vault mode (the address is the vault, not the wallet).
    """
    address = _unwrap_secret(address)
    secret = _unwrap_secret(secret)
    if address is None or secret is None:
        # Inputs not fully provided yet; defer until both are set.
        return
    if secret == "":
        raise ValueError("Hyperliquid private key must be a non-empty string.")
    if address == "":
        raise ValueError("Hyperliquid wallet/vault address must be a non-empty string.")
    if use_vault or mode == "api_wallet":
        return

    import eth_account
    from eth_utils import is_hex_address, to_checksum_address

    if not is_hex_address(address):
        raise ValueError(
            f"Invalid Hyperliquid wallet address {address!r}; "
            "expected a 0x-prefixed 20-byte hex address."
        )
    try:
        wallet = eth_account.Account.from_key(secret)
    except ValueError:
        raise ValueError(
            "Invalid Hyperliquid private key: not a valid 32-byte hex private key."
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError(f"Invalid Hyperliquid private key format: {exc}") from exc

    derived = to_checksum_address(wallet.address)
    provided = to_checksum_address(address)
    if derived != provided:
        raise ValueError(
            "Hyperliquid private key does not derive to the supplied Arbitrum wallet "
            f"address. Derived: {derived}; provided: {provided}. Verify the private key "
            "matches the wallet address, answer Yes to the Vault prompt if the supplied "
            "address is a vault address, or select the api_wallet connection mode if you "
            "are using a Hyperliquid API/agent wallet key."
        )


class HyperliquidPerpetualConfigMap(BaseConnectorConfigMap):
    connector: str = "hyperliquid_perpetual"
    hyperliquid_perpetual_mode: Literal["arb_wallet", "api_wallet"] = Field(
        default="arb_wallet",
        json_schema_extra={
            "prompt": "Select connection mode (arb_wallet/api_wallet)",
            "is_secure": False,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    use_vault: bool = Field(
        default="no",
        json_schema_extra={
            "prompt": "Do you want to use the Vault address? (Yes/No)",
            "is_secure": False,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    hyperliquid_perpetual_address: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": lambda cm: (
                "Enter your Vault address"
                if getattr(cm, "use_vault", False)
                else "Enter your Arbitrum wallet address"
            ),
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    hyperliquid_perpetual_secret_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": lambda cm: {
                "arb_wallet": "Enter your Arbitrum wallet private key",
                "api_wallet": "Enter your API wallet private key (from https://app.hyperliquid.xyz/API)"
            }.get(getattr(cm, "hyperliquid_perpetual_mode", "arb_wallet")),
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    model_config = ConfigDict(title="hyperliquid_perpetual")

    @field_validator("hyperliquid_perpetual_mode", mode="before")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        """Used for client-friendly error output."""
        return validate_wallet_mode(value)

    @field_validator("use_vault", mode="before")
    @classmethod
    def validate_use_vault(cls, value: str):
        """Used for client-friendly error output."""
        return validate_bool(value)

    @field_validator("hyperliquid_perpetual_address", mode="before")
    @classmethod
    def validate_address(cls, value: str):
        """Used for client-friendly error output."""
        if isinstance(value, str):
            if value.startswith("HL:"):
                # Strip out the "HL:" that the HyperLiquid Vault page adds to vault addresses
                return value[3:]
        return value

    @field_validator("hyperliquid_perpetual_secret_key", mode="before")
    @classmethod
    def validate_secret_key(cls, value):
        """Reject malformed private keys at connect time (issue #7866)."""
        return validate_private_key_format(value)

    @model_validator(mode="after")
    def validate_key_matches_address(self):
        """Ensure the private key derives to the supplied wallet address (#7866)."""
        validate_key_address_pair(
            getattr(self, "hyperliquid_perpetual_mode", "arb_wallet"),
            getattr(self, "use_vault", False),
            getattr(self, "hyperliquid_perpetual_address", None),
            getattr(self, "hyperliquid_perpetual_secret_key", None),
        )
        return self


KEYS = HyperliquidPerpetualConfigMap.model_construct()

OTHER_DOMAINS = ["hyperliquid_perpetual_testnet"]
OTHER_DOMAINS_PARAMETER = {"hyperliquid_perpetual_testnet": "hyperliquid_perpetual_testnet"}
OTHER_DOMAINS_EXAMPLE_PAIR = {"hyperliquid_perpetual_testnet": "BTC-USD"}
OTHER_DOMAINS_DEFAULT_FEES = {"hyperliquid_perpetual_testnet": [0, 0.025]}


class HyperliquidPerpetualTestnetConfigMap(BaseConnectorConfigMap):
    connector: str = "hyperliquid_perpetual_testnet"
    hyperliquid_perpetual_testnet_mode: Literal["arb_wallet", "api_wallet"] = Field(
        default="arb_wallet",
        json_schema_extra={
            "prompt": "Select connection mode (arb_wallet/api_wallet)",
            "is_secure": False,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    use_vault: bool = Field(
        default="no",
        json_schema_extra={
            "prompt": "Do you want to use the Vault address? (Yes/No)",
            "is_secure": False,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    hyperliquid_perpetual_testnet_address: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": lambda cm: (
                "Enter your Vault address"
                if getattr(cm, "use_vault", False)
                else "Enter your Arbitrum wallet address"
            ),
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    hyperliquid_perpetual_testnet_secret_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": lambda cm: {
                "arb_wallet": "Enter your Arbitrum wallet private key",
                "api_wallet": "Enter your API wallet private key (from https://app.hyperliquid.xyz/API)"
            }.get(getattr(cm, "hyperliquid_perpetual_testnet_mode", "arb_wallet")),
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    model_config = ConfigDict(title="hyperliquid_perpetual")

    @field_validator("hyperliquid_perpetual_testnet_mode", mode="before")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        """Used for client-friendly error output."""
        return validate_wallet_mode(value)

    @field_validator("use_vault", mode="before")
    @classmethod
    def validate_use_vault(cls, value: str):
        """Used for client-friendly error output."""
        return validate_bool(value)

    @field_validator("hyperliquid_perpetual_testnet_address", mode="before")
    @classmethod
    def validate_address(cls, value: str):
        """Used for client-friendly error output."""
        if isinstance(value, str):
            if value.startswith("HL:"):
                # Strip out the "HL:" that the HyperLiquid Vault page adds to vault addresses
                return value[3:]
        return value

    @field_validator("hyperliquid_perpetual_testnet_secret_key", mode="before")
    @classmethod
    def validate_secret_key(cls, value):
        """Reject malformed private keys at connect time (issue #7866)."""
        return validate_private_key_format(value)

    @model_validator(mode="after")
    def validate_key_matches_address(self):
        """Ensure the private key derives to the supplied wallet address (#7866)."""
        validate_key_address_pair(
            getattr(self, "hyperliquid_perpetual_testnet_mode", "arb_wallet"),
            getattr(self, "use_vault", False),
            getattr(self, "hyperliquid_perpetual_testnet_address", None),
            getattr(self, "hyperliquid_perpetual_testnet_secret_key", None),
        )
        return self


OTHER_DOMAINS_KEYS = {
    "hyperliquid_perpetual_testnet": HyperliquidPerpetualTestnetConfigMap.model_construct()
}
