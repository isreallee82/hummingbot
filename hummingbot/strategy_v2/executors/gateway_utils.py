"""
Shared utilities for Gateway executors.

Provides connector validation and normalization for SwapExecutor and LPExecutor.
"""
import logging
from typing import Callable, List, Optional, Tuple

from hummingbot.client.settings import GATEWAY_CONNECTORS

logger = logging.getLogger(__name__)


def validate_and_normalize_connector(
    connector_name: str,
    required_type: str,
    on_error: Callable[[str], None],
) -> Tuple[Optional[str], bool]:
    """
    Validate and normalize connector name for Gateway executors.

    - If connector already has a type suffix (e.g., /router, /clmm), validates it
    - If connector is base name only (e.g., "jupiter"), auto-appends the required type
    - Uses GATEWAY_CONNECTORS list populated at gateway startup
    - If GATEWAY_CONNECTORS is empty, skips validation and normalizes the name
      (Gateway will validate at execution time)

    Args:
        connector_name: Connector name from config (e.g., "jupiter", "meteora/clmm")
        required_type: The connector type suffix required (e.g., "router", "clmm")
        on_error: Callback function to log error messages

    Returns:
        Tuple of (normalized_connector_name, success)
        - If validation succeeds: (normalized_name, True)
        - If validation fails: (None, False)

    Example:
        # For SwapExecutor (requires router)
        result, ok = validate_and_normalize_connector("jupiter", "router", logger.error)
        # Returns ("jupiter/router", True) if jupiter/router exists

        # For LPExecutor (requires clmm)
        result, ok = validate_and_normalize_connector("meteora", "clmm", logger.error)
        # Returns ("meteora/clmm", True) if meteora/clmm exists
    """
    type_suffix = f"/{required_type}"

    # If already has suffix, validate it matches the required type
    if "/" in connector_name:
        base, connector_type = connector_name.split("/", 1)

        if connector_type != required_type:
            on_error(
                f"Executor requires /{required_type} connector type. "
                f"'{connector_type}' is not supported."
            )
            return None, False

        # If GATEWAY_CONNECTORS is empty, skip validation (API context without monitor loop)
        # Gateway will validate at execution time
        if not GATEWAY_CONNECTORS:
            logger.debug(
                f"GATEWAY_CONNECTORS empty, skipping validation for {connector_name}. "
                "Gateway will validate at execution time."
            )
            return connector_name, True

        if connector_name not in GATEWAY_CONNECTORS:
            matching_connectors = [c for c in GATEWAY_CONNECTORS if type_suffix in c]
            on_error(
                f"Connector '{connector_name}' not found in Gateway. "
                f"Available {required_type} connectors: {matching_connectors}"
            )
            return None, False

        return connector_name, True

    # Base name only - auto-append the required type
    normalized_name = f"{connector_name}/{required_type}"

    # If GATEWAY_CONNECTORS is empty, skip validation (API context without monitor loop)
    # Just normalize the name and let Gateway validate at execution time
    if not GATEWAY_CONNECTORS:
        logger.debug(
            f"GATEWAY_CONNECTORS empty, normalizing {connector_name} -> {normalized_name}. "
            "Gateway will validate at execution time."
        )
        return normalized_name, True

    if normalized_name in GATEWAY_CONNECTORS:
        return normalized_name, True

    # Check if connector exists at all with any type
    matching = [c for c in GATEWAY_CONNECTORS if c.startswith(f"{connector_name}/")]
    if matching:
        on_error(
            f"Connector '{connector_name}' doesn't support /{required_type}. "
            f"Available types for {connector_name}: {matching}"
        )
    else:
        matching_connectors = [c for c in GATEWAY_CONNECTORS if type_suffix in c]
        on_error(
            f"Connector '{connector_name}' not found in Gateway. "
            f"Available {required_type} connectors: {matching_connectors}"
        )

    return None, False


def get_connectors_by_type(connector_type: str) -> List[str]:
    """
    Get all Gateway connectors of a specific type.

    Args:
        connector_type: The connector type (e.g., "router", "clmm", "amm")

    Returns:
        List of connector names matching the type
    """
    type_suffix = f"/{connector_type}"
    return [c for c in GATEWAY_CONNECTORS if type_suffix in c]
