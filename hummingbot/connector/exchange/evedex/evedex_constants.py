"""Evedex Exchange constants module."""
from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

DEFAULT_DOMAIN = ""

MAX_ORDER_ID_LEN = 50

HBOT_ORDER_ID_PREFIX = "HBOT"

# Chain ID
CHAIN_ID = "161803"

# EvedEx EIP-712 Constants
EVEDEX_DOMAIN_NAME = "EVEDEX"
EVEDEX_DOMAIN_VERSION = "2"
EVEDEX_DOMAIN_SALT = "0x5792f7333c35db190e30acc144f049fd15b24f552c0010b8b3e06f9105c37c5a"  # noqa: mock
MATCHER_PRECISION = 8  # Used for normalizing floating-point numbers

# Exchange name
EXCHANGE_NAME = "evedex"

# Base URLs
REST_URL = "https://exchange-api.evedex.com"
WSS_URL = "wss://ws.evedex.com/connection/websocket"

# Public REST API endpoints
PING_PATH_URL = "/api/ping"
INSTRUMENTS_PATH_URL = "/api/market/instrument"
ORDER_BOOK_PATH_URL = "/api/market/{instrument}/deep"
TRADES_PATH_URL = "/api/market/{instrument}/trades"
RECENT_TRADES_PATH_URL = "/api/market/{instrument}/recent-trades"
TICKER_PATH_URL = "/api/ticker"

# Private REST API endpoints
LIMIT_ORDER_PATH_URL = "/api/v2/order/limit"
MARKET_ORDER_PATH_URL = "/api/v2/order/market"
CANCEL_ORDER_PATH_URL = "/api/order/{orderId}"
GET_ORDER_PATH_URL = "/api/order/{orderId}"
OPEN_ORDERS_PATH_URL = "/api/order/opened"
ORDER_FILLS_PATH_URL = "/api/fill"
USER_BALANCE_PATH_URL = "/api/market/available-balance"
USER_ME_PATH_URL = "/api/user/me"
DX_FEED_AUTH_PATH_URL = "/api/dx-feed/auth"

# WebSocket endpoints
WS_HEARTBEAT_TIME_INTERVAL = 25  # Centrifugo ping interval (send before server timeout)
WS_PING_TIMEOUT = 10  # How long to wait for pong response

# WebSocket channels
WS_ORDERBOOK_CHANNEL = "orderbook"
WS_TRADES_CHANNEL = "trades"
WS_ORDERS_CHANNEL = "orders"
WS_BALANCE_CHANNEL = "balance"
WS_SUBSCRIBE_EVENT = "subscribe"
WS_UNSUBSCRIBE_EVENT = "unsubscribe"
WS_AUTH_EVENT = "auth"

# Order sides
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

# Time in force
TIME_IN_FORCE_GTC = "GTC"
TIME_IN_FORCE_IOC = "IOC"
TIME_IN_FORCE_FOK = "FOK"
TIME_IN_FORCE_DAY = "DAY"

# Order states mapping to Hummingbot OrderState
ORDER_STATE = {
    "INTENTION": OrderState.PENDING_CREATE,
    "NEW": OrderState.OPEN,
    "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
    "FILLED": OrderState.FILLED,
    "CANCELLED": OrderState.CANCELED,
    "REJECTED": OrderState.FAILED,
    "EXPIRED": OrderState.CANCELED,
    "REPLACED": OrderState.OPEN,
    "ERROR": OrderState.FAILED,
}

# Error messages
ORDER_NOT_EXIST_MESSAGE = "Order not found"
UNKNOWN_ORDER_MESSAGE = "Unknown order"

# Rate limits
GLOBAL_LIMIT_ID = "GlobalRate"
ALL_ENDPOINTS_LIMIT = "AllEndpoints"

# Request weight limits
REQUEST_WEIGHT = 1
HEAVY_REQUEST_WEIGHT = 5

# Rate limit definitions
RATE_LIMITS = [
    RateLimit(limit_id=GLOBAL_LIMIT_ID, limit=1200, time_interval=60),
    RateLimit(limit_id=ALL_ENDPOINTS_LIMIT, limit=10, time_interval=1),
    # Public endpoints
    RateLimit(
        limit_id=PING_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=INSTRUMENTS_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=ORDER_BOOK_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=TRADES_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=TICKER_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
    # Private endpoints
    RateLimit(
        limit_id=LIMIT_ORDER_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, HEAVY_REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=MARKET_ORDER_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, HEAVY_REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=CANCEL_ORDER_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=GET_ORDER_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=OPEN_ORDERS_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=ORDER_FILLS_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=USER_BALANCE_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=USER_ME_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
    RateLimit(
        limit_id=DX_FEED_AUTH_PATH_URL,
        limit=10,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_LIMIT_ID, REQUEST_WEIGHT)]
    ),
]
