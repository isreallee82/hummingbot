from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

DEFAULT_DOMAIN = "grvt_perpetual"
TESTNET_DOMAIN = "grvt_perpetual_testnet"

HBOT_ORDER_ID_PREFIX = "HBOT"
MAX_ORDER_ID_LEN = 32

BASE_DOMAIN = "grvt.io"
TESTNET_PREFIX = "testnet"

REST_TRADING_SUBDOMAIN = "trades"
REST_MARKET_DATA_SUBDOMAIN = "market-data"
REST_AUTH_SUBDOMAIN = "edge"
WSS_TRADING_SUBDOMAIN = "trades"
WSS_MARKET_DATA_SUBDOMAIN = "market-data"

REST_API_PREFIX = "/full"
REST_AUTH_PREFIX = "/auth"
WS_API_PREFIX = "/ws/full"

WS_HEARTBEAT_TIME_INTERVAL = 30

# Public market data endpoints
TIME_PATH_URL = "/time"
EXCHANGE_INFO_PATH_URL = "/v1/instruments"
ALL_INSTRUMENTS_PATH_URL = "/v1/all_instruments"
TRADING_RULES_PATH_URL = "/v1/instruments"
TICKER_PATH_URL = "/v1/ticker"
SNAPSHOT_PATH_URL = "/v1/book"
ORDER_BOOK_SNAPSHOT_DEPTHS = (50, 10, 100, 500)

# Auth endpoint
LOGIN_PATH_URL = "/login"
API_KEY_LOGIN_PATH_URL = "/api_key/login"

# Private trading endpoints
CREATE_ORDER_PATH_URL = "/v1/create_order"
CANCEL_ORDER_PATH_URL = "/v1/cancel_order"
ORDER_PATH_URL = "/v1/order"
FILL_HISTORY_PATH_URL = "/v1/fill_history"
FUNDING_PAYMENT_HISTORY_PATH_URL = "/v1/funding_payment_history"
POSITIONS_PATH_URL = "/v1/positions"
SET_INITIAL_LEVERAGE_PATH_URL = "/v1/set_initial_leverage"
ACCOUNT_SUMMARY_PATH_URL = "/v1/account_summary"

# WS streams
WS_PUBLIC_TICKERS_STREAM = "v1.tickers"
WS_PUBLIC_ORDERBOOK_SNAPSHOT_STREAM = "v1.orderbook_levels"
WS_PUBLIC_ORDERBOOK_DIFF_STREAM = "v1.orderbook_levels_deltas"
WS_PUBLIC_TRADES_STREAM = "v1.trades"
WS_PUBLIC_FUNDING_STREAM = "v1.funding_rates"

WS_PRIVATE_ORDERS_STREAM = "v1.orders"
WS_PRIVATE_ORDER_STATE_STREAM = "v1.order_state"
WS_PRIVATE_FILLS_STREAM = "v1.fills"
WS_PRIVATE_POSITIONS_STREAM = "v1.positions"

ORDER_STATE = {
    "pending": OrderState.PENDING_CREATE,
    "pending_open": OrderState.PENDING_CREATE,
    "open": OrderState.OPEN,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "filled": OrderState.FILLED,
    "cancelled": OrderState.CANCELED,
    "rejected": OrderState.FAILED,
    "expired": OrderState.FAILED,
}

ORDER_NOT_EXIST_MESSAGE = "not found"
UNKNOWN_ORDER_MESSAGE = "not found"

GLOBAL_PUBLIC_LIMIT_ID = "GLOBAL_PUBLIC"
GLOBAL_PRIVATE_LIMIT_ID = "GLOBAL_PRIVATE"
AUTH_LIMIT_ID = "AUTH"
WS_CONNECTION_LIMIT_ID = "WS_CONNECTIONS"
WS_REQUEST_LIMIT_ID = "WS_REQUESTS"

RATE_LIMITS = [
    RateLimit(limit_id=GLOBAL_PUBLIC_LIMIT_ID, limit=500, time_interval=1),
    RateLimit(limit_id=GLOBAL_PRIVATE_LIMIT_ID, limit=200, time_interval=1),
    RateLimit(limit_id=AUTH_LIMIT_ID, limit=20, time_interval=1),
    RateLimit(limit_id=WS_CONNECTION_LIMIT_ID, limit=120, time_interval=60),
    RateLimit(limit_id=WS_REQUEST_LIMIT_ID, limit=500, time_interval=1),
    RateLimit(
        limit_id=API_KEY_LOGIN_PATH_URL,
        limit=20,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(AUTH_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=EXCHANGE_INFO_PATH_URL,
        limit=100,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PUBLIC_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=ALL_INSTRUMENTS_PATH_URL,
        limit=100,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PUBLIC_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=SNAPSHOT_PATH_URL,
        limit=20,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PUBLIC_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=TICKER_PATH_URL,
        limit=50,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PUBLIC_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=CREATE_ORDER_PATH_URL,
        limit=100,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PRIVATE_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=CANCEL_ORDER_PATH_URL,
        limit=100,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PRIVATE_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=ORDER_PATH_URL,
        limit=50,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PRIVATE_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=FILL_HISTORY_PATH_URL,
        limit=20,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PRIVATE_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=FUNDING_PAYMENT_HISTORY_PATH_URL,
        limit=20,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PRIVATE_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=POSITIONS_PATH_URL,
        limit=20,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PRIVATE_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=SET_INITIAL_LEVERAGE_PATH_URL,
        limit=20,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PRIVATE_LIMIT_ID)],
    ),
    RateLimit(
        limit_id=ACCOUNT_SUMMARY_PATH_URL,
        limit=20,
        time_interval=1,
        linked_limits=[LinkedLimitWeightPair(GLOBAL_PRIVATE_LIMIT_ID)],
    ),
]
