from decimal import Decimal
from typing import Any, Dict

from pydantic import ConfigDict, Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

CENTRALIZED = True
EXAMPLE_PAIR = "BTC-USDT"

DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.0002"),
    taker_percent_fee_decimal=Decimal("0.0005"),
    buy_percent_fee_deducted_from_returns=False,
)


def is_exchange_information_valid(exchange_info: Dict[str, Any]) -> bool:
    return str(exchange_info.get("kind", "")).upper() == "PERPETUAL"


def normalize_instrument(instrument: str) -> str:
    raw = instrument.strip()
    if "_" in raw:
        parts = raw.split("_")
        if len(parts) >= 2:
            base = parts[0]
            quote = parts[1]
            return f"{base}-{quote}".lower()
    symbol = raw.replace("_", "-").lower()
    if symbol.endswith("-perp"):
        symbol = symbol[:-5]
    return symbol


class GrvtPerpetualConfigMap(BaseConnectorConfigMap):
    connector: str = "grvt_perpetual"
    grvt_perpetual_api_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your GRVT API key",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    grvt_perpetual_sub_account_id: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your GRVT sub account id",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        }
    )
    grvt_perpetual_evm_private_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your GRVT private key (Order signing)",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )

    model_config = ConfigDict(title="grvt_perpetual")


KEYS = GrvtPerpetualConfigMap.model_construct()
