import asyncio
import hashlib
import random
import re
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.derivative.grvt_perpetual import (
    grvt_perpetual_constants as CONSTANTS,
    grvt_perpetual_utils as utils,
    grvt_perpetual_web_utils as web_utils,
)
from hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_api_order_book_data_source import (
    GrvtPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_api_user_stream_data_source import (
    GrvtPerpetualAPIUserStreamDataSource,
)
from hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_auth import GrvtPerpetualAuth
from hummingbot.connector.derivative.position import Position
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair, get_new_client_order_id
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.order_candidate import PerpetualOrderCandidate
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.event.events import AccountEvent, PositionModeChangeEvent
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class GrvtPerpetualDerivative(PerpetualDerivativePyBase):
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    web_utils = web_utils

    _VALID_ORDER_STREAMS = {
        CONSTANTS.WS_PRIVATE_ORDERS_STREAM,
        CONSTANTS.WS_PRIVATE_ORDER_STATE_STREAM,
    }

    def __init__(
        self,
        grvt_perpetual_api_key: str = "",
        grvt_perpetual_sub_account_id: str = "",
        grvt_perpetual_evm_private_key: str = "",
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
        balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
        rate_limits_share_pct: Decimal = Decimal("100"),
    ):
        self._api_key = grvt_perpetual_api_key
        self._sub_account_id = grvt_perpetual_sub_account_id
        self._evm_private_key = grvt_perpetual_evm_private_key
        self._session_cookie = ""
        self._account_id = ""

        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs or []
        self._position_mode = PositionMode.ONEWAY
        self._session_initialized = False
        self._instrument_info_map: Dict[str, Dict[str, Any]] = {}
        self.real_time_balance_update = False
        super().__init__(balance_asset_limit, rate_limits_share_pct)

    def _sub_account_id_or_fail(self) -> str:
        sub_account_id = (self._sub_account_id or self._account_id or "").strip()
        if not sub_account_id:
            raise ValueError(
                "GRVT private requests require a sub account id. "
                "Set `grvt_perpetual_sub_account_id`."
            )
        return sub_account_id

    @staticmethod
    def _exchange_client_order_id(hb_client_order_id: str) -> str:
        """
        GRVT PySDK examples use numeric client_order_id values.
        Keep Hummingbot internal ids as HBOT..., but derive a stable uint64-style client id for GRVT payloads.
        """
        if hb_client_order_id.isdigit():
            return hb_client_order_id
        digest = hashlib.sha256(hb_client_order_id.encode("utf-8")).digest()
        low_63_bits = int.from_bytes(digest[:8], byteorder="big") & ((1 << 63) - 1)
        return str((1 << 63) | low_63_bits)

    @staticmethod
    def _is_placeholder_order_id(exchange_order_id: Optional[str]) -> bool:
        normalized = str(exchange_order_id or "").strip().lower()
        return normalized in {"", "0", "0x0", "0x00", "none", "null"}

    def _confirmed_exchange_order_id(self, exchange_order_id: Optional[str]) -> Optional[str]:
        if self._is_placeholder_order_id(exchange_order_id):
            return None
        return str(exchange_order_id)

    def _private_request_payload(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = dict(payload or {})
        if data.get("sub_account_id") in (None, ""):
            data["sub_account_id"] = self._sub_account_id_or_fail()
        return data

    async def _private_request_payload_async(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self._sub_account_id and not self._account_id:
            await self._ensure_authenticated_session()
        return self._private_request_payload(payload)

    def _cache_instrument_metadata(self, instrument: str, instrument_data: Dict[str, Any]):
        candidates = {
            instrument,
            instrument.lower(),
            instrument.upper(),
            utils.normalize_instrument(instrument),
            utils.normalize_instrument(instrument).replace("-", "_"),
            utils.normalize_instrument(instrument).replace("-", "_").upper(),
        }
        for key in candidates:
            self._instrument_info_map[key] = instrument_data

    def _find_instrument_metadata(self, instrument: str) -> Optional[Dict[str, Any]]:
        candidates = [
            instrument,
            instrument.lower(),
            instrument.upper(),
            utils.normalize_instrument(instrument),
            utils.normalize_instrument(instrument).replace("-", "_"),
            utils.normalize_instrument(instrument).replace("-", "_").upper(),
        ]
        for key in candidates:
            instrument_data = self._instrument_info_map.get(key)
            if instrument_data is not None:
                return instrument_data
        return None

    def _instrument_metadata_for_order(self, instrument: str) -> Dict[str, Any]:
        instrument_data = self._find_instrument_metadata(instrument)
        if instrument_data is None:
            raise ValueError(
                f"Unable to find cached GRVT instrument metadata for {instrument}. "
                "Expected instrument metadata to be loaded from `/v1/instruments` before order creation."
            )
        if instrument_data.get("instrument_hash") is None or instrument_data.get("base_decimals") is None:
            raise ValueError(
                f"Cached GRVT instrument metadata for {instrument} is missing `instrument_hash` or `base_decimals` "
                "required for EIP-712 signing."
            )

        return instrument_data

    @property
    def authenticator(self) -> GrvtPerpetualAuth:
        return GrvtPerpetualAuth(
            session_cookie=self._session_cookie,
            account_id=self._account_id,
            api_key=self._api_key,
            sub_account_id=self._sub_account_id,
            evm_private_key=self._evm_private_key,
            domain=self._domain,
        )

    @property
    def name(self) -> str:
        return self._domain

    @property
    def rate_limits_rules(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self):
        return self._domain

    @property
    def client_order_id_max_length(self):
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self):
        return CONSTANTS.HBOT_ORDER_ID_PREFIX

    @property
    def trading_rules_request_path(self):
        return CONSTANTS.TRADING_RULES_PATH_URL

    @property
    def trading_pairs_request_path(self):
        return CONSTANTS.EXCHANGE_INFO_PATH_URL

    @property
    def check_network_request_path(self):
        return CONSTANTS.TIME_PATH_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    @property
    def funding_fee_poll_interval(self) -> int:
        return 120

    def supported_order_types(self):
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER, OrderType.MARKET]

    def supported_position_modes(self) -> List[PositionMode]:
        return [PositionMode.ONEWAY]

    def get_buy_collateral_token(self, trading_pair: str) -> str:
        return self._trading_rules[trading_pair].buy_order_collateral_token

    def get_sell_collateral_token(self, trading_pair: str) -> str:
        return self._trading_rules[trading_pair].sell_order_collateral_token

    def in_flight_asset_balances(self, in_flight_orders: Dict[str, InFlightOrder]) -> Dict[str, Decimal]:
        asset_balances: Dict[str, Decimal] = {}
        if in_flight_orders is None:
            return asset_balances

        for order in (o for o in in_flight_orders.values() if not (o.is_done or o.is_failure or o.is_cancelled)):
            outstanding_amount = order.amount - order.executed_amount_base
            if outstanding_amount <= Decimal("0") or order.price is None:
                continue

            order_candidate = PerpetualOrderCandidate(
                trading_pair=order.trading_pair,
                is_maker=order.order_type in {OrderType.LIMIT, OrderType.LIMIT_MAKER},
                order_type=order.order_type,
                order_side=order.trade_type,
                amount=outstanding_amount,
                price=order.price,
                leverage=Decimal(order.leverage),
                position_close=order.position == PositionAction.CLOSE,
            )
            order_candidate = self.budget_checker.populate_collateral_entries(order_candidate)

            for token, amount in order_candidate.collateral_dict.items():
                asset_balances[token] = asset_balances.get(token, Decimal("0")) + amount

        return asset_balances

    def buy(
        self,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType = OrderType.LIMIT,
        price: Decimal = s_decimal_NaN,
        **kwargs,
    ) -> str:
        order_id = get_new_client_order_id(
            is_buy=True,
            trading_pair=trading_pair,
            hbot_order_id_prefix=self.client_order_id_prefix,
            max_id_len=self.client_order_id_max_length,
        )
        safe_ensure_future(
            self._create_order(
                trade_type=TradeType.BUY,
                order_id=order_id,
                trading_pair=trading_pair,
                amount=amount,
                order_type=order_type,
                price=price,
                **kwargs,
            )
        )
        return order_id

    def sell(
        self,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType = OrderType.LIMIT,
        price: Decimal = s_decimal_NaN,
        **kwargs,
    ) -> str:
        order_id = get_new_client_order_id(
            is_buy=False,
            trading_pair=trading_pair,
            hbot_order_id_prefix=self.client_order_id_prefix,
            max_id_len=self.client_order_id_max_length,
        )
        safe_ensure_future(
            self._create_order(
                trade_type=TradeType.SELL,
                order_id=order_id,
                trading_pair=trading_pair,
                amount=amount,
                order_type=order_type,
                price=price,
                **kwargs,
            )
        )
        return order_id

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        text = str(request_exception).lower()
        return "timestamp" in text and ("invalid" in text or "expired" in text)

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return CONSTANTS.ORDER_NOT_EXIST_MESSAGE in str(status_update_exception).lower()

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return CONSTANTS.UNKNOWN_ORDER_MESSAGE in str(cancelation_exception).lower()

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            domain=self._domain,
            auth=self._auth,
        )

    async def _make_network_check_request(self):
        await web_utils.get_current_server_time(throttler=self._throttler, domain=self.domain)

    async def _make_trading_rules_request(self) -> Any:
        return await self._api_post(
            path_url=self.trading_rules_request_path,
            data={"kind": ["PERPETUAL"], "is_active": True, "limit": 1000},
        )

    async def _make_trading_pairs_request(self) -> Any:
        return await self._api_post(
            path_url=self.trading_pairs_request_path,
            data={"kind": ["PERPETUAL"], "is_active": True, "limit": 1000},
        )

    async def start_network(self):
        if self._trading_required:
            await self._ensure_authenticated_session()
        await super().start_network()
        if self._trading_required:
            try:
                await self._update_positions()
                await self._update_balances()
            except Exception:
                self.logger().debug(
                    "Initial GRVT account synchronization failed. "
                    "Regular polling will retry shortly.",
                    exc_info=True,
                )

    async def _api_request(self, *args, **kwargs):
        if kwargs.get("is_auth_required", False):
            await self._ensure_authenticated_session()
        try:
            return await super()._api_request(*args, **kwargs)
        except IOError as request_error:
            method = kwargs.get("method")
            if method == RESTMethod.GET and "HTTP status is 405" in str(request_error):
                retry_kwargs = dict(kwargs)
                retry_kwargs["method"] = RESTMethod.POST
                if retry_kwargs.get("data") is None and retry_kwargs.get("params") is not None:
                    retry_kwargs["data"] = dict(retry_kwargs["params"])
                    retry_kwargs["params"] = None
                self.logger().debug(
                    f"GRVT endpoint rejected GET for {kwargs.get('path_url')}. Retrying as POST."
                )
                return await super()._api_request(*args, **retry_kwargs)
            raise

    async def _ensure_authenticated_session(self):
        if self._session_initialized and not self._api_key:
            return

        # Prefer API-key login whenever API key is configured, to keep cookie/account headers in sync.
        if self._api_key:
            await self._login_with_api_key()
            return

        if self._auth.has_session:
            self._session_cookie = self._auth.auth_headers().get("Cookie", "")
            self._account_id = self._auth.account_id
            if not self._sub_account_id and self._account_id:
                self._sub_account_id = self._account_id
            self._session_initialized = True
            return

        raise ValueError(
            "GRVT authentication is required. "
            "Set `grvt_perpetual_api_key` and `grvt_perpetual_sub_account_id`."
        )

    async def _login_with_api_key(self):
        rest_assistant = await self._web_assistants_factory.get_rest_assistant()
        url = web_utils.auth_api_key_login_url(domain=self._domain)

        payload: Dict[str, Any] = {"api_key": self._api_key}
        if self._sub_account_id:
            payload["sub_account_id"] = self._sub_account_id

        response = await rest_assistant.execute_request_and_get_response(
            url=url,
            data=payload,
            method=RESTMethod.POST,
            is_auth_required=False,
            throttler_limit_id=CONSTANTS.API_KEY_LOGIN_PATH_URL,
            headers={"Cookie": "rm=true;"},
        )
        raw_response = getattr(response, "_aiohttp_response", None)
        headers = response.headers
        account_id = headers.get("X-Grvt-Account-Id") or self._account_id

        # Collect cookie/header candidates from final response and redirect history.
        set_cookie_values: List[str] = []
        cookie_name_values: List[str] = []

        def _collect_from_headers(_headers):
            local_values: List[str] = []
            if hasattr(_headers, "getall"):
                try:
                    local_values.extend(list(_headers.getall("Set-Cookie", [])))
                except Exception:
                    pass
            direct_value = _headers.get("Set-Cookie")
            if direct_value:
                local_values.append(direct_value)
            return local_values

        set_cookie_values.extend(_collect_from_headers(headers))

        if raw_response is not None:
            for morsel in raw_response.cookies.values():
                cookie_name_values.append(f"{morsel.key}={morsel.value}")

            for historical_response in getattr(raw_response, "history", []):
                set_cookie_values.extend(_collect_from_headers(historical_response.headers))
                for morsel in historical_response.cookies.values():
                    cookie_name_values.append(f"{morsel.key}={morsel.value}")
                if not account_id:
                    account_id = historical_response.headers.get("X-Grvt-Account-Id") or account_id

        session_cookie = ""
        cookie_patterns = [
            r"((?:__Secure-|__Host-)?gravity=[^;,\s]+)",
        ]
        for cookie_line in set_cookie_values + cookie_name_values:
            for pattern in cookie_patterns:
                match = re.search(pattern, cookie_line or "")
                if match:
                    session_cookie = match.group(1)
                    break
            if session_cookie:
                break

        if not session_cookie:
            body_text = ""
            try:
                body_text = await response.text()
            except Exception:
                body_text = ""
            raise IOError(
                "Failed to authenticate to GRVT: no `gravity` cookie found in auth response. "
                f"Endpoint={url} status={response.status}. "
                "Check environment endpoint (prod/staging/testnet) and API key validity. "
                f"Response body={body_text}"
            )
        if not account_id:
            body_text = ""
            try:
                body_text = await response.text()
            except Exception:
                body_text = ""
            raise IOError(
                "Failed to authenticate to GRVT: `X-Grvt-Account-Id` header missing in auth response. "
                f"Endpoint={url} status={response.status}. Response body={body_text}"
            )

        self._session_cookie = session_cookie
        self._account_id = account_id
        if not self._sub_account_id:
            self._sub_account_id = account_id
        self._auth.set_session_context(session_cookie=session_cookie, account_id=account_id)
        self._session_initialized = True

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return GrvtPerpetualAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return GrvtPerpetualAPIUserStreamDataSource(
            auth=self._auth,
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _get_fee(
        self,
        base_currency: str,
        quote_currency: str,
        order_type: OrderType,
        order_side: TradeType,
        position_action: PositionAction,
        amount: Decimal,
        price: Decimal = s_decimal_NaN,
        is_maker: Optional[bool] = None,
    ) -> TradeFeeBase:
        maker = is_maker if is_maker is not None else order_type in (OrderType.LIMIT, OrderType.LIMIT_MAKER)
        return AddedToCostTradeFee(percent=self.estimate_fee_pct(maker))

    async def _place_order(
        self,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        trade_type: TradeType,
        order_type: OrderType,
        price: Decimal,
        position_action: PositionAction = PositionAction.NIL,
        **kwargs,
    ) -> Tuple[str, float]:
        if not self._sub_account_id and not self._account_id:
            await self._ensure_authenticated_session()

        instrument = await self.exchange_symbol_associated_to_pair(trading_pair)
        sub_account_id = self._sub_account_id_or_fail()
        exchange_client_order_id = self._exchange_client_order_id(order_id)

        is_market = order_type == OrderType.MARKET
        time_in_force = "IMMEDIATE_OR_CANCEL" if is_market else "GOOD_TILL_TIME"
        if order_type == OrderType.LIMIT_MAKER:
            time_in_force = "GOOD_TILL_TIME"

        expiry_ns = kwargs.get("signature_expiration_ns")
        if expiry_ns is None:
            order_duration_secs = float(kwargs.get("order_duration_secs", 300.0))
            expiry_ns = int(time.time_ns() + order_duration_secs * 1_000_000_000)
        nonce = kwargs.get("signature_nonce")
        if nonce is None:
            nonce = random.randint(0, (2 ** 32) - 1)

        instrument_data = self._instrument_metadata_for_order(instrument=instrument)
        provided_signature: Optional[Dict[str, Any]] = kwargs.get("signature")
        order_payload: Dict[str, Any] = {
            "sub_account_id": sub_account_id,
            "is_market": is_market,
            "time_in_force": time_in_force,
            "post_only": order_type == OrderType.LIMIT_MAKER,
            "reduce_only": position_action == PositionAction.CLOSE,
            "legs": [
                {
                    "instrument": instrument,
                    "size": f"{amount:f}",
                    "limit_price": "0" if is_market else f"{price:f}",
                    "is_buying_asset": trade_type == TradeType.BUY,
                }
            ],
            "signature": {
                "expiration": str(expiry_ns),
                "nonce": int(nonce),
            },
            "metadata": {
                "client_order_id": exchange_client_order_id,
            },
        }

        create_payload = self._auth.build_create_order_payload(
            order=order_payload,
            instrument_data=instrument_data,
            provided_signature=provided_signature,
        )

        try:
            response = await self._api_post(
                path_url=CONSTANTS.CREATE_ORDER_PATH_URL,
                data=create_payload,
                is_auth_required=True,
                limit_id=CONSTANTS.CREATE_ORDER_PATH_URL,
            )
        except IOError as request_error:
            normalized_error = str(request_error).lower()
            if (
                "client order id should be supplied when creating an order" in normalized_error
                or '"code":2011' in normalized_error
            ):
                raise IOError(
                    "GRVT create_order rejected the payload with code 2011 (client_order_id required). "
                    "Connector sends `order.metadata.client_order_id` in SDK-compatible shape. "
                    "Verify account permissions/environment and try a fresh API key session."
                )
            if '"code":1001' in normalized_error and "not authorized to access this functionality" in normalized_error:
                raise IOError(
                    "GRVT create_order authorization failed (code 1001: not authorized). "
                    "Verify API key trading permissions, environment (prod/testnet), and sub-account binding."
                )
            raise

        result = response["result"]
        if not isinstance(result, dict):
            raise IOError("Unexpected GRVT create_order response format: missing result object.")
        order_id_from_exchange = result.get("order_id")
        if order_id_from_exchange is None:
            raise IOError("Unexpected GRVT create_order response format: missing `order_id`.")
        exchange_order_id = (
            exchange_client_order_id
            if self._is_placeholder_order_id(str(order_id_from_exchange))
            else str(order_id_from_exchange)
        )
        ts = int(result["metadata"]["create_time"]) * 1e-9 or self._time_synchronizer.time()

        return exchange_order_id, ts

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        payload: Dict[str, Any] = {
            "client_order_id": self._exchange_client_order_id(order_id),
        }
        confirmed_exchange_order_id = self._confirmed_exchange_order_id(tracked_order.exchange_order_id)
        if confirmed_exchange_order_id is not None:
            payload["order_id"] = confirmed_exchange_order_id
        payload = await self._private_request_payload_async(payload)

        result = await self._api_post(
            path_url=CONSTANTS.CANCEL_ORDER_PATH_URL,
            data=payload,
            is_auth_required=True,
            limit_id=CONSTANTS.CANCEL_ORDER_PATH_URL,
        )
        body = result["result"]
        if isinstance(body, dict):
            state = str(body.get("state", "")).lower()
            if state == "cancelled":
                return True
            if body.get("ack") is True:
                return True
        return False

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        rules: List[TradingRule] = []
        exchange_info = exchange_info_dict["result"]

        for entry in exchange_info or []:
            if not utils.is_exchange_information_valid(entry):
                continue
            try:
                instrument = entry.get("instrument")
                base = entry.get("base")
                quote = entry.get("quote")
                if instrument is None or base is None or quote is None:
                    continue
                self._cache_instrument_metadata(str(instrument), entry)
                trading_pair = combine_to_hb_trading_pair(base=base, quote=quote)
                quote_asset = trading_pair.split("-")[1]

                min_order_size = Decimal(str(entry["min_size"]))
                tick_size = Decimal(str(entry["tick_size"]))
                step_size = min_order_size
                min_notional = Decimal("0")

                if min_order_size <= Decimal("0") or tick_size <= Decimal("0") or step_size <= Decimal("0"):
                    self.logger().warning(
                        f"Skipping invalid GRVT trading rule for {trading_pair}: "
                        f"min_size={min_order_size} tick_size={tick_size}"
                    )
                    continue

                rules.append(
                    TradingRule(
                        trading_pair=trading_pair,
                        min_order_size=min_order_size,
                        min_price_increment=tick_size,
                        min_base_amount_increment=step_size,
                        min_notional_size=min_notional,
                        buy_order_collateral_token=quote_asset,
                        sell_order_collateral_token=quote_asset,
                    )
                )
            except Exception:
                self.logger().exception(f"Error parsing trading rule {entry}. Skipping.")

        return rules

    async def _update_trading_fees(self):
        # Not documented in extracted GRVT sections.
        return

    async def _user_stream_event_listener(self):
        async for event_message in self._iter_user_event_queue():
            try:
                stream = event_message.get("params", {}).get("stream")
                if stream in self._VALID_ORDER_STREAMS:
                    self._process_order_event(event_message)
                elif stream == CONSTANTS.WS_PRIVATE_FILLS_STREAM:
                    self._process_trade_event(event_message)
                elif stream == CONSTANTS.WS_PRIVATE_POSITIONS_STREAM:
                    await self._process_position_event(event_message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error in user stream listener loop.", exc_info=True)
                await self._sleep(1.0)

    @staticmethod
    def _extract_ws_feed_data(event_message: Dict[str, Any]) -> Dict[str, Any]:
        params = event_message.get("params", {})
        if not isinstance(params, dict):
            return {}
        data = params.get("data")
        if not isinstance(data, dict):
            return {}
        feed = data.get("feed")
        if not isinstance(feed, dict):
            return {}
        return feed

    def _process_order_event(self, event_message: Dict[str, Any]):
        stream = event_message.get("params", {}).get("stream")
        data = self._extract_ws_feed_data(event_message)
        if not data:
            return
        exchange_order_id = data.get("order_id")
        if stream == CONSTANTS.WS_PRIVATE_ORDER_STATE_STREAM:
            client_order_id = data["client_order_id"]
            state_obj = data["order_state"]
        else:
            client_order_id = data["metadata"]["client_order_id"]
            state_obj = data["state"]

        tracked_order = self._order_tracker.all_updatable_orders.get(str(client_order_id))
        if tracked_order is None:
            for order in self._order_tracker.all_updatable_orders.values():
                if self._exchange_client_order_id(order.client_order_id) == str(client_order_id):
                    tracked_order = order
                    break

        if tracked_order is None and exchange_order_id is not None:
            for order in self._order_tracker.all_updatable_orders.values():
                if str(order.exchange_order_id) == str(exchange_order_id):
                    tracked_order = order
                    break

        if tracked_order is None:
            return

        state = str(state_obj["status"]).lower()
        order_state = CONSTANTS.ORDER_STATE.get(state, OrderState.FAILED)
        normalized_exchange_order_id = self._confirmed_exchange_order_id(
            str(exchange_order_id) if exchange_order_id is not None else None
        )

        order_update = OrderUpdate(
            trading_pair=tracked_order.trading_pair,
            update_timestamp=int(state_obj["update_time"]) * 1e-9 or self.current_timestamp,
            new_state=order_state,
            client_order_id=str(tracked_order.client_order_id),
            exchange_order_id=normalized_exchange_order_id or tracked_order.exchange_order_id,
        )
        self._order_tracker.process_order_update(order_update=order_update)

    def _process_trade_event(self, event_message: Dict[str, Any]):
        data = self._extract_ws_feed_data(event_message)
        if not data:
            return
        exchange_order_id = data.get("order_id")
        client_order_id = data.get("client_order_id")

        tracked_order = None
        if client_order_id is not None:
            tracked_order = self._order_tracker.all_fillable_orders.get(str(client_order_id))
            if tracked_order is None:
                for order in self._order_tracker.all_fillable_orders.values():
                    if self._exchange_client_order_id(order.client_order_id) == str(client_order_id):
                        tracked_order = order
                        break

        if tracked_order is None and exchange_order_id is not None:
            for order in self._order_tracker.all_fillable_orders.values():
                if str(order.exchange_order_id) == str(exchange_order_id):
                    tracked_order = order
                    break

        if tracked_order is None:
            return

        fee_amount = Decimal(str(data["fee"]))
        fee_token = tracked_order.quote_asset
        fee = TradeFeeBase.new_perpetual_fee(
            fee_schema=self.trade_fee_schema(),
            position_action=PositionAction.NIL,
            flat_fees=[TokenAmount(amount=fee_amount, token=fee_token)] if fee_amount > Decimal("0") else [],
        )

        fill_qty = Decimal(str(data["size"]))
        fill_price = Decimal(str(data["price"]))
        trade_id = data.get("trade_id")
        if trade_id is None:
            return
        normalized_exchange_order_id = self._confirmed_exchange_order_id(
            str(exchange_order_id) if exchange_order_id is not None else None
        )
        trade_exchange_order_id = (
            normalized_exchange_order_id
            or tracked_order.exchange_order_id
            or self._exchange_client_order_id(tracked_order.client_order_id)
        )
        trade_update = TradeUpdate(
            trade_id=str(trade_id),
            client_order_id=str(tracked_order.client_order_id),
            exchange_order_id=trade_exchange_order_id,
            trading_pair=tracked_order.trading_pair,
            fee=fee,
            fill_base_amount=fill_qty,
            fill_quote_amount=fill_qty * fill_price,
            fill_price=fill_price,
            fill_timestamp=int(data["event_time"]) * 1e-9 or self.current_timestamp,
        )
        self._order_tracker.process_trade_update(trade_update)

    async def _process_position_event(self, event_message: Dict[str, Any]):
        data = self._extract_ws_feed_data(event_message)
        if not data:
            return
        symbol = data.get("instrument")
        if symbol is None:
            return

        try:
            trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol)
        except Exception:
            return

        signed_size = self._signed_position_size(data)
        if signed_size == Decimal("0"):
            self._remove_oneway_position(trading_pair=trading_pair)
            return

        side = PositionSide.LONG if signed_size > Decimal("0") else PositionSide.SHORT
        position_key = self._perpetual_trading.position_key(trading_pair, side)

        position = Position(
            trading_pair=trading_pair,
            position_side=side,
            unrealized_pnl=Decimal(str(data["unrealized_pnl"])),
            entry_price=Decimal(str(data["entry_price"])),
            amount=signed_size,
            leverage=Decimal(str(data["leverage"])),
        )
        self._perpetual_trading.set_position(position_key, position)

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        base, quote = order.trading_pair.split("-")
        payload = {
            "base": [base],
            "quote": [quote],
            "limit": 200,
        }
        response = await self._api_post(
            path_url=CONSTANTS.FILL_HISTORY_PATH_URL,
            data=await self._private_request_payload_async(payload),
            is_auth_required=True,
            limit_id=CONSTANTS.FILL_HISTORY_PATH_URL,
        )
        rows = response["result"]

        trade_updates: List[TradeUpdate] = []
        expected_exchange_client_order_id = self._exchange_client_order_id(order.client_order_id)
        for trade in rows or []:
            confirmed_exchange_order_id = self._confirmed_exchange_order_id(order.exchange_order_id)
            if (
                trade.get("order_id")
                and confirmed_exchange_order_id is not None
                and str(trade.get("order_id")) != confirmed_exchange_order_id
            ):
                continue
            if trade.get("client_order_id") and str(trade.get("client_order_id")) != expected_exchange_client_order_id:
                continue

            fee_amount = Decimal(str(trade["fee"]))
            fee_token = order.quote_asset
            fee = TradeFeeBase.new_perpetual_fee(
                fee_schema=self.trade_fee_schema(),
                position_action=PositionAction.NIL,
                flat_fees=[TokenAmount(amount=fee_amount, token=fee_token)] if fee_amount > Decimal("0") else [],
            )
            fill_qty = Decimal(str(trade["size"]))
            fill_price = Decimal(str(trade["price"]))
            normalized_trade_exchange_order_id = self._confirmed_exchange_order_id(
                str(trade.get("order_id")) if trade.get("order_id") is not None else None
            )
            trade_exchange_order_id = (
                normalized_trade_exchange_order_id
                or order.exchange_order_id
                or expected_exchange_client_order_id
            )

            trade_updates.append(
                TradeUpdate(
                    trade_id=str(trade.get("trade_id")),
                    client_order_id=order.client_order_id,
                    exchange_order_id=trade_exchange_order_id,
                    trading_pair=order.trading_pair,
                    fee=fee,
                    fill_base_amount=fill_qty,
                    fill_quote_amount=fill_qty * fill_price,
                    fill_price=fill_price,
                    fill_timestamp=int(trade["event_time"]) * 1e-9 or self.current_timestamp,
                )
            )

        return trade_updates

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        payload = {
            "client_order_id": self._exchange_client_order_id(tracked_order.client_order_id),
        }
        confirmed_exchange_order_id = self._confirmed_exchange_order_id(tracked_order.exchange_order_id)
        if confirmed_exchange_order_id is not None:
            payload["order_id"] = confirmed_exchange_order_id

        response = await self._api_post(
            path_url=CONSTANTS.ORDER_PATH_URL,
            data=await self._private_request_payload_async(payload),
            is_auth_required=True,
            limit_id=CONSTANTS.ORDER_PATH_URL,
        )
        result = response["result"]
        if not isinstance(result, dict):
            raise IOError("Unexpected GRVT order response format: missing result object.")
        state_obj = result["state"]
        state = str(state_obj["status"]).lower()
        order_state = CONSTANTS.ORDER_STATE.get(state, OrderState.FAILED)

        response_exchange_order_id = result.get("order_id")
        normalized_exchange_order_id = self._confirmed_exchange_order_id(
            str(response_exchange_order_id) if response_exchange_order_id is not None else None
        )
        if normalized_exchange_order_id is None:
            normalized_exchange_order_id = tracked_order.exchange_order_id

        return OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=normalized_exchange_order_id,
            trading_pair=tracked_order.trading_pair,
            update_timestamp=int(state_obj["update_time"]) * 1e-9 or self.current_timestamp,
            new_state=order_state,
        )

    async def _update_balances(self):
        response = await self._api_post(
            path_url=CONSTANTS.ACCOUNT_SUMMARY_PATH_URL,
            data=await self._private_request_payload_async(),
            is_auth_required=True,
            limit_id=CONSTANTS.ACCOUNT_SUMMARY_PATH_URL,
        )
        result = response["result"]
        balances = result["spot_balances"]
        settle_currency = str(result["settle_currency"])
        available_settle = Decimal(str(result["available_balance"]))

        local_assets = set(self._account_balances.keys())
        remote_assets = set()

        for entry in balances:
            asset = entry["currency"]
            total = Decimal(str(entry["balance"]))
            available = available_settle if asset == settle_currency else total
            self._account_available_balances[asset] = available
            self._account_balances[asset] = total
            remote_assets.add(asset)

        for asset in local_assets.difference(remote_assets):
            self._account_balances.pop(asset, None)
            self._account_available_balances.pop(asset, None)

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        info = exchange_info["result"]
        mapping = bidict()
        for symbol_data in info or []:
            if not utils.is_exchange_information_valid(symbol_data):
                continue
            instrument = symbol_data.get("instrument")
            base = symbol_data.get("base")
            quote = symbol_data.get("quote")
            if instrument is None or base is None or quote is None:
                continue
            self._cache_instrument_metadata(str(instrument), symbol_data)
            mapping[str(instrument)] = combine_to_hb_trading_pair(base=base, quote=quote)
        self._set_trading_pair_symbol_map(mapping)

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        payload = {
            "instrument": await self.exchange_symbol_associated_to_pair(trading_pair),
        }
        response = await self._api_get(
            path_url=CONSTANTS.TICKER_PATH_URL,
            params=payload,
            limit_id=CONSTANTS.TICKER_PATH_URL,
        )
        result = response["result"]
        return float(result.get("last_price", 0))

    async def _update_positions(self):
        response = await self._api_post(
            path_url=CONSTANTS.POSITIONS_PATH_URL,
            data=await self._private_request_payload_async(),
            is_auth_required=True,
            limit_id=CONSTANTS.POSITIONS_PATH_URL,
        )
        positions = response["result"]

        tracked_position_keys = set()
        for raw_position in positions or []:
            symbol = raw_position.get("instrument")
            if symbol is None:
                continue
            try:
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol)
            except Exception:
                continue

            signed_size = self._signed_position_size(raw_position)
            if signed_size == Decimal("0"):
                self._remove_oneway_position(trading_pair=trading_pair)
                continue
            side = PositionSide.LONG if signed_size > Decimal("0") else PositionSide.SHORT
            key = self._perpetual_trading.position_key(trading_pair, side)
            tracked_position_keys.add(key)

            position = Position(
                trading_pair=trading_pair,
                position_side=side,
                unrealized_pnl=Decimal(str(raw_position["unrealized_pnl"])),
                entry_price=Decimal(str(raw_position["entry_price"])),
                amount=signed_size,
                leverage=Decimal(str(raw_position["leverage"])),
            )
            self._perpetual_trading.set_position(key, position)

        for key in list(self._perpetual_trading.account_positions.keys()):
            if key not in tracked_position_keys:
                self._perpetual_trading.remove_position(key)

    def _remove_oneway_position(self, trading_pair: str):
        key = self._perpetual_trading.position_key(trading_pair)
        self._perpetual_trading.remove_position(key)

    @staticmethod
    def _signed_position_size(position_data: Dict[str, Any]) -> Decimal:
        size = Decimal(str(position_data["size"]))
        if size == Decimal("0"):
            return Decimal("0")

        # GRVT SDK marks shorts with negative notional; use it to keep side deterministic
        # when size is returned as an absolute quantity.
        notional = Decimal(str(position_data["notional"]))
        if size > Decimal("0") and notional < Decimal("0"):
            return -size
        return size

    async def _trading_pair_position_mode_set(self, mode: PositionMode, trading_pair: str) -> Tuple[bool, str]:
        if mode != PositionMode.ONEWAY:
            msg = "GRVT perpetual connector currently supports only ONEWAY position mode."
            self.trigger_event(
                AccountEvent.PositionModeChangeFailed,
                PositionModeChangeEvent(self.current_timestamp, trading_pair, mode, msg),
            )
            return False, msg

        self._position_mode = PositionMode.ONEWAY
        self.trigger_event(
            AccountEvent.PositionModeChangeSucceeded,
            PositionModeChangeEvent(self.current_timestamp, trading_pair, mode),
        )
        return True, ""

    async def _set_trading_pair_leverage(self, trading_pair: str, leverage: int) -> Tuple[bool, str]:
        payload = {
            "instrument": await self.exchange_symbol_associated_to_pair(trading_pair),
            "leverage": str(leverage),
        }
        try:
            response = await self._api_post(
                path_url=CONSTANTS.SET_INITIAL_LEVERAGE_PATH_URL,
                data=await self._private_request_payload_async(payload),
                is_auth_required=True,
                limit_id=CONSTANTS.SET_INITIAL_LEVERAGE_PATH_URL,
            )
            success = response.get("success", True) if isinstance(response, dict) else True
            if success:
                self._perpetual_trading.set_leverage(trading_pair, leverage)
                return True, ""
            return False, str(response)
        except Exception as ex:
            return False, str(ex)

    async def _fetch_last_fee_payment(self, trading_pair: str) -> Tuple[float, Decimal, Decimal]:
        payload = {
            "instrument": await self.exchange_symbol_associated_to_pair(trading_pair),
            "limit": 1,
        }
        response = await self._api_post(
            path_url=CONSTANTS.FUNDING_PAYMENT_HISTORY_PATH_URL,
            data=await self._private_request_payload_async(payload),
            is_auth_required=True,
            limit_id=CONSTANTS.FUNDING_PAYMENT_HISTORY_PATH_URL,
        )
        rows = response["result"]
        if not rows:
            return 0, Decimal("-1"), Decimal("-1")

        last = rows[0]
        timestamp = int(last["event_time"]) * 1e-9
        rate = Decimal("-1")
        amount = Decimal(str(last["amount"]))
        return timestamp, rate, amount
