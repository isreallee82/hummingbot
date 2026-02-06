"""Evedex utilities and configuration module."""
from decimal import Decimal
from typing import Any, Dict

from pydantic import ConfigDict, Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

# Default trading fee schema for Evedex
DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.0002"),  # 0.02% maker fee
    taker_percent_fee_decimal=Decimal("0.0005"),  # 0.05% taker fee
    buy_percent_fee_deducted_from_returns=False,
)

CENTRALIZED = True
EXAMPLE_PAIR = "BTC-USDT"

# Trading pair format utilities


def is_exchange_information_valid(exchange_info: Dict[str, Any]) -> bool:
    """
    Verifies if a trading pair is valid.
    """
    is_active = exchange_info.get("isActive", True)
    has_name = "name" in exchange_info or "instrument" in exchange_info
    return is_active and has_name


class EvedexConfigMap(BaseConnectorConfigMap):
    """Configuration map for Evedex exchange connector."""

    connector: str = "evedex"

    evedex_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": lambda cm: "Enter your Evedex API key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )

    model_config = ConfigDict(title="evedex")


KEYS = EvedexConfigMap.model_construct()


def build_api_factory_config_map() -> Dict[str, str]:
    """
    Build config map for API factory.
    """
    return {
        "evedex_api_key": KEYS.evedex_api_key.get_secret_value() if KEYS.evedex_api_key else "",
    }
