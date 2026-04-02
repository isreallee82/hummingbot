"""
DEPRECATED: This module is kept for backward compatibility.
Use hummingbot.connector.gateway.gateway instead.
"""

# Re-export everything from the combined gateway module for backward compatibility
from hummingbot.connector.gateway.gateway import (
    AMMPoolInfo,
    AMMPositionInfo,
    CLMMPoolInfo,
    CLMMPositionInfo,
    Gateway,
    TokenInfo,
)

# Alias for backward compatibility
GatewayLp = Gateway

__all__ = [
    "AMMPoolInfo",
    "AMMPositionInfo",
    "CLMMPoolInfo",
    "CLMMPositionInfo",
    "Gateway",
    "GatewayLp",
    "TokenInfo",
]
