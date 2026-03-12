from typing import Callable, Optional

from hummingbot.connector.derivative.grvt_perpetual import grvt_perpetual_constants as CONSTANTS
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.connector.utils import TimeSynchronizerRESTPreProcessor
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


def rest_url(
    path_url: str,
    domain: str = CONSTANTS.DEFAULT_DOMAIN,
    subdomain: str = CONSTANTS.REST_TRADING_SUBDOMAIN,
    path_prefix: str = CONSTANTS.REST_API_PREFIX,
) -> str:
    host = (
        f"{CONSTANTS.TESTNET_PREFIX}.{CONSTANTS.BASE_DOMAIN}"
        if domain == CONSTANTS.TESTNET_DOMAIN
        else CONSTANTS.BASE_DOMAIN
    )
    return f"https://{subdomain}.{host}{path_prefix}{path_url}"


def private_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    return rest_url(
        path_url=path_url,
        domain=domain,
        subdomain=CONSTANTS.REST_TRADING_SUBDOMAIN,
        path_prefix=CONSTANTS.REST_API_PREFIX,
    )


def public_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    return rest_url(
        path_url=path_url,
        domain=domain,
        subdomain=CONSTANTS.REST_MARKET_DATA_SUBDOMAIN,
        path_prefix=CONSTANTS.REST_API_PREFIX,
    )


def auth_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    return rest_url(
        path_url=path_url,
        domain=domain,
        subdomain=CONSTANTS.REST_AUTH_SUBDOMAIN,
        path_prefix=CONSTANTS.REST_AUTH_PREFIX,
    )


def auth_api_key_login_url(domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    return rest_url(
        path_url=CONSTANTS.API_KEY_LOGIN_PATH_URL,
        domain=domain,
        subdomain=CONSTANTS.REST_AUTH_SUBDOMAIN,
        path_prefix=CONSTANTS.REST_AUTH_PREFIX,
    )


def wss_url(domain: str = CONSTANTS.DEFAULT_DOMAIN, subdomain: str = CONSTANTS.WSS_MARKET_DATA_SUBDOMAIN) -> str:
    host = (
        f"{CONSTANTS.TESTNET_PREFIX}.{CONSTANTS.BASE_DOMAIN}"
        if domain == CONSTANTS.TESTNET_DOMAIN
        else CONSTANTS.BASE_DOMAIN
    )
    return f"wss://{subdomain}.{host}{CONSTANTS.WS_API_PREFIX}"


def public_wss_url(domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    return wss_url(domain=domain, subdomain=CONSTANTS.WSS_MARKET_DATA_SUBDOMAIN)


def private_wss_url(domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    return wss_url(domain=domain, subdomain=CONSTANTS.WSS_TRADING_SUBDOMAIN)


def build_api_factory(
    throttler: Optional[AsyncThrottler] = None,
    time_synchronizer: Optional[TimeSynchronizer] = None,
    domain: str = CONSTANTS.DEFAULT_DOMAIN,
    time_provider: Optional[Callable] = None,
    auth: Optional[AuthBase] = None,
) -> WebAssistantsFactory:
    throttler = throttler or create_throttler()
    time_synchronizer = time_synchronizer or TimeSynchronizer()
    time_provider = time_provider or (lambda: get_current_server_time(throttler=throttler, domain=domain))

    return WebAssistantsFactory(
        throttler=throttler,
        auth=auth,
        rest_pre_processors=[
            TimeSynchronizerRESTPreProcessor(synchronizer=time_synchronizer, time_provider=time_provider),
        ],
    )


def create_throttler() -> AsyncThrottler:
    return AsyncThrottler(CONSTANTS.RATE_LIMITS)


def build_api_factory_without_time_synchronizer_pre_processor(throttler: AsyncThrottler) -> WebAssistantsFactory:
    return WebAssistantsFactory(throttler=throttler)


async def get_current_server_time(
    throttler: Optional[AsyncThrottler] = None,
    domain: str = CONSTANTS.DEFAULT_DOMAIN,
) -> float:
    throttler = throttler or create_throttler()
    api_factory = build_api_factory_without_time_synchronizer_pre_processor(throttler=throttler)
    rest_assistant = await api_factory.get_rest_assistant()

    response = await rest_assistant.execute_request(
        url=rest_url(
            path_url=CONSTANTS.TIME_PATH_URL,
            domain=domain,
            subdomain=CONSTANTS.REST_MARKET_DATA_SUBDOMAIN,
            path_prefix="",
        ),
        method=RESTMethod.GET,
        throttler_limit_id=CONSTANTS.GLOBAL_PUBLIC_LIMIT_ID,
    )
    return float(response["server_time"])
