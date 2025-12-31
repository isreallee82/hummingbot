from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

EXCHANGE_NAME = "pacifica_perpetual"
DEFAULT_DOMAIN = "pacifica_perpetual"
HB_OT_ID_PREFIX = "HBOT"

# Base URLs
REST_URL = "https://api.pacifica.fi/api/v1"
WSS_URL = "wss://ws.pacifica.fi/ws"

TESTNET_DOMAIN = "pacifica_perpetual_testnet"
TESTNET_REST_URL = "https://test-api.pacifica.fi/api/v1"
TESTNET_WSS_URL = "wss://test-ws.pacifica.fi/ws"

# order status mapping
ORDER_STATE = {
    "open": OrderState.OPEN,
    "filled": OrderState.FILLED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "cancelled": OrderState.CANCELED,
    "rejected": OrderState.FAILED,
}

GET_MARKET_ORDER_BOOK_SNAPSHOT_PATH_URL = "/book"
GET_ORDER_HISTORY_PATH_URL = "/orders/history_by_id"
GET_CANDLES_PATH_URL = "/kline"
GET_PRICES_PATH_URL = "/info/prices"
GET_POSITIONS_PATH_URL = "/positions"
GET_FUNDING_HISTORY_PATH_URL = "/funding/history"
SET_LEVERAGE_PATH_URL = "/account/leverage"
CANCEL_ORDER_PATH_URL = "/orders/cancel"
EXCHANGE_INFO_PATH_URL = "/info"
GET_ACCOUNT_INFO_PATH_URL = "/account"
GET_ACCOUNT_API_CONFIG_KEYS = "/account/api_keys"
CREATE_ACCOUNT_API_CONFIG_KEY = "/account/api_keys/create"
GET_TRADE_HISTORY_PATH_URL = "/trades/history"
GET_FEES_INFO_PATH_URL = "/info/fees"

# the API endpoints for market / limit / stop orders are different
# the support for stop orders is out of the scope for this integration
CREATE_MARKET_ORDER_PATH_URL = "/orders/create_market"
CREATE_LIMIT_ORDER_PATH_URL = "/orders/create"

# WebSocket Channels

WS_ORDER_BOOK_SNAPSHOT_CHANNEL = "book"
WS_TRADES_CHANNEL = "trades"
WS_PRICES_CHANNEL = "prices"

WS_ACCOUNT_ORDER_UPDATES_CHANNEL = "account_order_updates"
WS_ACCOUNT_POSITIONS_CHANNEL = "account_positions"
WS_ACCOUNT_INFO_CHANNEL = "account_info"
WS_ACCOUNT_TRADES_CHANNEL = "account_trades"

WS_PING_INTERVAL = 30  # Keep connection alive

# the exchange has different "costs" of the calls for every endpoint
# plus there're exactly 2 tiers of rate limits: (1) Unidentified IP (2) Valid API Config Key
# below you could find (in the comments) -- the costs (aka "weight") of each endpoints group

PACIFICA_LIMIT_ID = "PACIFICA_LIMIT"

PACIFICA_TIER_1_LIMIT = 1250  # Unidentified IP
PACIFICA_TIER_2_LIMIT = 3000  # Valid API Config Key
PACIFICA_LIMIT_INTERVAL = 60

# Costs
STANDARD_REQUEST_COST = 10
ORDER_CANCELLATION_COST = 5
HEAVY_GET_REQUEST_COST = 120  # All GET requests are treated as "Heavy" because there is no way to distinguish them in advance.

RATE_LIMITS = [
    RateLimit(limit_id=PACIFICA_LIMIT_ID, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL),
    RateLimit(limit_id=GET_MARKET_ORDER_BOOK_SNAPSHOT_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
    RateLimit(limit_id=CREATE_LIMIT_ORDER_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=STANDARD_REQUEST_COST)]),
    RateLimit(limit_id=CREATE_MARKET_ORDER_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=STANDARD_REQUEST_COST)]),
    RateLimit(limit_id=CANCEL_ORDER_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=ORDER_CANCELLATION_COST)]),
    RateLimit(limit_id=SET_LEVERAGE_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=STANDARD_REQUEST_COST)]),
    RateLimit(limit_id=GET_FUNDING_HISTORY_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
    RateLimit(limit_id=GET_POSITIONS_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
    RateLimit(limit_id=GET_ORDER_HISTORY_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
    RateLimit(limit_id=GET_CANDLES_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
    RateLimit(limit_id=EXCHANGE_INFO_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
    RateLimit(limit_id=GET_PRICES_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
    RateLimit(limit_id=GET_ACCOUNT_INFO_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
    RateLimit(limit_id=GET_ACCOUNT_API_CONFIG_KEYS, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
    RateLimit(limit_id=CREATE_ACCOUNT_API_CONFIG_KEY, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
    RateLimit(limit_id=GET_TRADE_HISTORY_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
    RateLimit(limit_id=GET_FEES_INFO_PATH_URL, limit=PACIFICA_TIER_1_LIMIT, time_interval=PACIFICA_LIMIT_INTERVAL,
              linked_limits=[LinkedLimitWeightPair(limit_id=PACIFICA_LIMIT_ID, weight=HEAVY_GET_REQUEST_COST)]),
]
