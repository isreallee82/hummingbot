"""Evedex web utilities module."""
import time
from typing import Any, Callable, Dict, Optional

from hummingbot.connector.exchange.evedex import evedex_constants as CONSTANTS
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest
from hummingbot.core.web_assistant.rest_pre_processors import RESTPreProcessorBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class EvedexRESTPreProcessor(RESTPreProcessorBase):
    """
    REST pre-processor for Evedex API requests.
    Adds Content-Type header for all requests.
    """

    async def pre_process(self, request: RESTRequest) -> RESTRequest:
        if request.headers is None:
            request.headers = {}
        request.headers["Content-Type"] = "application/json"
        return request


def public_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Construct a public REST URL.
    """
    return CONSTANTS.REST_URL + path_url


def private_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Construct a private REST URL.
    """
    return CONSTANTS.REST_URL + path_url


def wss_url(domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Return WebSocket URL.
    """
    return CONSTANTS.WSS_URL


def build_api_factory(
        throttler: Optional[AsyncThrottler] = None,
        time_synchronizer: Optional[TimeSynchronizer] = None,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
        time_provider: Optional[Callable] = None,
        auth: Optional[AuthBase] = None) -> WebAssistantsFactory:
    """
    Build the API factory for Evedex connector.
    """
    throttler = throttler or create_throttler()
    time_synchronizer = time_synchronizer or TimeSynchronizer()

    api_factory = WebAssistantsFactory(
        throttler=throttler,
        auth=auth,
        rest_pre_processors=[
            EvedexRESTPreProcessor()
        ])
    return api_factory


def build_api_factory_without_time_synchronizer_pre_processor(
        throttler: AsyncThrottler) -> WebAssistantsFactory:
    """
    Build API factory without time synchronizer.
    """
    api_factory = WebAssistantsFactory(
        throttler=throttler,
        rest_pre_processors=[
            EvedexRESTPreProcessor()
        ])
    return api_factory


def create_throttler() -> AsyncThrottler:
    """
    Create async throttler with Evedex rate limits.
    """
    return AsyncThrottler(CONSTANTS.RATE_LIMITS)


async def get_current_server_time(
        throttler: Optional[AsyncThrottler] = None,
        domain: str = CONSTANTS.DEFAULT_DOMAIN) -> float:
    """
    Get current server time.
    Evedex doesn't require time synchronization, so we return local time.
    """
    return time.time()


def is_exchange_information_valid(exchange_info: Dict[str, Any]) -> bool:
    """
    Check if exchange information (trading pair) is valid for trading.

    :param exchange_info: Dictionary with instrument information
    :return: True if valid for trading
    """
    # Check if the instrument is active/enabled
    is_active = exchange_info.get("isActive", True)

    # Check if trading is allowed ("all" or unset means tradable, "restricted" means not)
    trading = exchange_info.get("trading", "all")
    is_trading_allowed = trading in ("all", None)

    # Check if it has required fields
    has_name = "name" in exchange_info or "instrument" in exchange_info
    has_from = "from" in exchange_info
    has_to = "to" in exchange_info

    return is_active and is_trading_allowed and has_name and has_from and has_to
