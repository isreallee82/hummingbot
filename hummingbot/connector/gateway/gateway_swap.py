"""
DEPRECATED: This module is kept for backward compatibility.
Use hummingbot.connector.gateway.gateway instead.
"""

# Re-export Gateway from the combined gateway module for backward compatibility
from hummingbot.connector.gateway.gateway import Gateway

# Alias for backward compatibility
GatewaySwap = Gateway

__all__ = [
    "Gateway",
    "GatewaySwap",
]
