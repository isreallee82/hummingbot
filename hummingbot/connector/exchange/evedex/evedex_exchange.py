"""Evedex Exchange Connector."""
import asyncio
import time
import uuid
from collections.abc import AsyncIterable
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.evedex import evedex_constants as CONSTANTS, evedex_web_utils as web_utils
from hummingbot.connector.exchange.evedex.evedex_api_order_book_data_source import EvedexAPIOrderBookDataSource
from hummingbot.connector.exchange.evedex.evedex_api_user_stream_data_source import EvedexAPIUserStreamDataSource
from hummingbot.connector.exchange.evedex.evedex_auth import EvedexAuth
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import DeductedFromReturnsTradeFee, TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class EvedexExchange(ExchangePyBase):
    """Evedex exchange connector."""

    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    web_utils = web_utils

    def __init__(
            self,
            evedex_api_key: str,
            evedex_private_key: str = "",
            balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
            rate_limits_share_pct: Decimal = Decimal("100"),
            trading_pairs: Optional[List[str]] = None,
            trading_required: bool = True,
            domain: str = CONSTANTS.DEFAULT_DOMAIN):
        self.evedex_api_key = evedex_api_key
        self.evedex_private_key = evedex_private_key
        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._last_trades_poll_timestamp = 1.0
        self._order_status_cache: Dict[str, Dict[str, Any]] = {}
        self._auth: Optional[EvedexAuth] = None
        super().__init__(balance_asset_limit, rate_limits_share_pct)

    @staticmethod
    def evedex_order_type(order_type: OrderType) -> str:
        """Convert Hummingbot order type to Evedex order type."""
        return order_type.name.upper()

    @staticmethod
    def to_hb_order_type(evedex_type: str) -> OrderType:
        """Convert Evedex order type to Hummingbot order type."""
        return OrderType[evedex_type]

    @property
    def authenticator(self) -> EvedexAuth:
        if self._auth is None:
            self._auth = EvedexAuth(
                api_key=self.evedex_api_key,
                time_provider=self._time_synchronizer,
                private_key=self.evedex_private_key or "",
            )
        return self._auth

    @property
    def name(self) -> str:
        return CONSTANTS.EXCHANGE_NAME

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
        return CONSTANTS.INSTRUMENTS_PATH_URL

    @property
    def trading_pairs_request_path(self):
        return CONSTANTS.INSTRUMENTS_PATH_URL

    @property
    def check_network_request_path(self):
        return CONSTANTS.PING_PATH_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    def supported_order_types(self):
        return [OrderType.LIMIT, OrderType.MARKET]

    async def get_all_pairs_prices(self) -> List[Dict[str, str]]:
        """
        Fetches prices for all trading pairs from EvedEx.
        Used by rate oracle for price discovery.

        :return: List of dicts with 'symbol' and 'price' keys
        """
        results: List[Dict[str, str]] = []
        try:
            response = await self._api_get(
                path_url=CONSTANTS.INSTRUMENTS_PATH_URL,
                params={"fields": "metrics"},
            )
            instruments = response if isinstance(response, list) else [response]
            for instrument in instruments:
                symbol = instrument.get("name")
                price = instrument.get("lastPrice") or instrument.get("markPrice")
                if symbol and price:
                    results.append({
                        "symbol": symbol,
                        "price": str(price),
                    })
        except Exception:
            self.logger().exception("Error fetching all pairs prices from EvedEx")
        return results

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        # Evedex doesn't use time-based authentication
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return CONSTANTS.ORDER_NOT_EXIST_MESSAGE in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        error_str = str(cancelation_exception)
        return (CONSTANTS.ORDER_NOT_EXIST_MESSAGE in error_str or
                CONSTANTS.UNKNOWN_ORDER_MESSAGE in error_str)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            domain=self._domain,
            auth=self._auth)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return EvedexAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            domain=self.domain,
            api_factory=self._web_assistants_factory)

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return EvedexAPIUserStreamDataSource(
            auth=self._auth,
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 amount: Decimal,
                 price: Decimal = s_decimal_NaN,
                 is_maker: Optional[bool] = None) -> TradeFeeBase:
        is_maker = is_maker or False
        return DeductedFromReturnsTradeFee(percent=self.estimate_fee_pct(is_maker))

    def _generate_order_id(self) -> str:
        """Generate Evedex-compatible order ID in format: XXXXX:XXXXXXXXXXXXXXXXXXXXXXXXXX

        The first 5 digits represent the number of days since Evedex epoch (July 24, 2025).
        The remaining 26 characters are a random lowercase hex string.
        """
        # Evedex epoch: July 24, 2025 = day 20293 since Unix epoch
        EVEDEX_EPOCH_DAYS = 20293
        days_since_unix_epoch = int(time.time() / 86400)
        days_since_evedex_epoch = days_since_unix_epoch - EVEDEX_EPOCH_DAYS
        prefix = str(days_since_evedex_epoch).zfill(5)
        suffix = uuid.uuid4().hex[:26]  # lowercase hex
        return f"{prefix}:{suffix}"

    @staticmethod
    def _is_maker_from_fill(fill_data: Dict[str, Any]) -> bool:
        role_raw = fill_data.get("fillRole") or fill_data.get("makerTakerFlag") or fill_data.get("role")
        if isinstance(role_raw, bool):
            return role_raw
        role = str(role_raw or "").upper()
        return role in {"MAKER", "M"}

    @staticmethod
    def _parse_flat_fees(fee_list: Any) -> List[TokenAmount]:
        flat_fees: List[TokenAmount] = []
        for fee_item in fee_list or []:
            coin = str(fee_item.get("coin", "USDT")).upper()
            if coin == "TOTAL":
                continue
            amount = Decimal(str(fee_item.get("quantity", 0)))
            if amount == 0:
                continue
            flat_fees.append(TokenAmount(amount=amount, token=coin))
        return flat_fees

    def _build_trade_fee(self, trade_type: TradeType, fill_data: Dict[str, Any], flat_fees: List[TokenAmount]) -> TradeFeeBase:
        fee_schema = self.trade_fee_schema()
        if flat_fees:
            fee_rate = Decimal("0")
            percent_token = flat_fees[0].token
        else:
            is_maker = self._is_maker_from_fill(fill_data)
            fee_rate = fee_schema.maker_percent_fee_decimal if is_maker else fee_schema.taker_percent_fee_decimal
            percent_token = fee_schema.percent_fee_token
        return TradeFeeBase.new_spot_fee(
            fee_schema=fee_schema,
            trade_type=trade_type,
            percent=fee_rate,
            percent_token=percent_token,
            flat_fees=flat_fees,
        )

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        exchange_order_id = await tracked_order.get_exchange_order_id()
        path_url = CONSTANTS.CANCEL_ORDER_PATH_URL.format(orderId=exchange_order_id)

        await self._api_delete(
            path_url=path_url,
            is_auth_required=True,
            limit_id=CONSTANTS.CANCEL_ORDER_PATH_URL)

        return True

    async def _place_order(
            self,
            order_id: str,
            trading_pair: str,
            amount: Decimal,
            trade_type: TradeType,
            order_type: OrderType,
            price: Decimal,
            **kwargs,
    ) -> Tuple[str, float]:
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

        # Generate Evedex-compatible order ID
        evedex_order_id = self._generate_order_id()
        side = CONSTANTS.SIDE_BUY if trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL
        chain_id = CONSTANTS.CHAIN_ID

        cash_quantity = None
        limit_id = None
        leverage = 2  # Spot trading uses leverage 2 on EvedEx
        if order_type == OrderType.MARKET:
            path_url = CONSTANTS.MARKET_ORDER_PATH_URL
            cash_quantity = amount * price if price != s_decimal_NaN else amount

            api_params = {
                "id": evedex_order_id,
                "instrument": symbol,
                "side": side,
                "cashQuantity": str(cash_quantity),
                "timeInForce": CONSTANTS.TIME_IN_FORCE_IOC,
                "leverage": leverage,
                "chainId": chain_id,
            }
        else:
            path_url = CONSTANTS.LIMIT_ORDER_PATH_URL

            api_params = {
                "id": evedex_order_id,
                "instrument": symbol,
                "side": side,
                "quantity": str(amount),
                "limitPrice": str(price),
                "timeInForce": CONSTANTS.TIME_IN_FORCE_GTC,
                "leverage": leverage,
                "chainId": chain_id,
            }

        # Add EIP-712 signature (required by EvedEx)
        if self.authenticator.wallet_address is None:
            raise ValueError(
                "EvedEx requires a private key for order signing. "
                "Please configure evedex_perpetual_private_key in your connector settings."
            )

        if order_type == OrderType.MARKET:
            api_params["signature"] = self.authenticator.sign_market_order(
                order_id=evedex_order_id,
                instrument=symbol,
                side=side,
                time_in_force=CONSTANTS.TIME_IN_FORCE_IOC,
                leverage=leverage,
                cash_quantity=cash_quantity,
                chain_id=chain_id,
            )
        else:
            api_params["signature"] = self.authenticator.sign_limit_order(
                order_id=evedex_order_id,
                instrument=symbol,
                side=side,
                leverage=leverage,
                quantity=amount,
                limit_price=price,
                chain_id=chain_id,
            )

        try:
            order_result = await self._api_post(
                path_url=path_url,
                data=api_params,
                is_auth_required=True,
                limit_id=limit_id)

            exchange_order_id = str(order_result.get("id", evedex_order_id))
            transact_time = order_result.get("createdAt", time.time())

        except IOError as e:
            error_description = str(e)
            if "503" in error_description:
                exchange_order_id = "UNKNOWN"
                transact_time = time.time()
            else:
                raise

        return exchange_order_id, transact_time

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        """
        Queries the necessary API endpoint and initialize the TradingRule object for each trading pair being traded.
        """
        rules = exchange_info_dict if isinstance(exchange_info_dict, list) else exchange_info_dict.get("list", [])
        retval = []

        for rule in rules:
            try:
                if not web_utils.is_exchange_information_valid(rule):
                    continue

                instrument_name = rule.get("name") or rule.get("instrument", "")
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=instrument_name)

                required_fields = ("minQuantity", "priceIncrement", "quantityIncrement")
                missing_fields = [field for field in required_fields if field not in rule]
                if missing_fields:
                    raise ValueError(f"Missing required fields: {', '.join(missing_fields)}")

                min_order_size = Decimal(str(rule.get("minQuantity", "0.001")))
                tick_size = Decimal(str(rule.get("priceIncrement", "0.01")))
                step_size = Decimal(str(rule.get("quantityIncrement", "0.001")))
                # Prefer minVolume (min notional) when provided by the exchange
                min_price = Decimal(str(rule.get("minPrice", "0.01")))
                min_volume = Decimal(str(rule.get("minVolume", "0")))
                if min_volume > 0:
                    min_notional = min_volume
                else:
                    min_notional = min_order_size * min_price if min_price > 0 else Decimal("10")

                if min_notional <= 0:
                    min_price = Decimal(str(rule.get("minPrice", "0.01")))
                    min_notional = min_order_size * min_price if min_price > 0 else Decimal("10")

                retval.append(
                    TradingRule(
                        trading_pair,
                        min_order_size=min_order_size,
                        min_price_increment=tick_size,
                        min_base_amount_increment=step_size,
                        min_notional_size=min_notional))
            except Exception:
                self.logger().error(
                    f"Error parsing the trading pair rule {rule}. Skipping.", exc_info=True
                )
        return retval

    async def _status_polling_loop_fetch_updates(self):
        await self._update_order_fills_from_trades()
        await super()._status_polling_loop_fetch_updates()

    async def _update_trading_fees(self):
        """Update fees information from the exchange."""
        pass

    async def _iter_user_event_queue(self) -> AsyncIterable[Dict[str, any]]:
        while True:
            try:
                yield await self._user_stream_tracker.user_stream.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().network(
                    "Unknown error. Retrying after 1 seconds.",
                    exc_info=True,
                    app_warning_msg="Could not fetch user events from Evedex. Check API key and network connection.",
                )
                await self._sleep(1.0)

    async def _user_stream_event_listener(self):
        """
        Wait for new messages from _user_stream_tracker.user_stream queue and processes them according to their
        message channels. The respective UserStreamDataSource queues these messages.
        """
        async for event_message in self._iter_user_event_queue():
            try:
                await self._process_user_stream_event(event_message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error in user stream listener loop.", exc_info=True)
                await self._sleep(5.0)

    async def _process_user_stream_event(self, event_message: Dict[str, Any]):
        """
        Process user stream events from Centrifugo.

        Handles both push format and direct format:
        - Push: {"push": {"channel": "futures-perp:order:123", "pub": {"data": {...}}}}
        - Direct: {"channel": "futures-perp:order:123", "data": {...}}
        """
        if not isinstance(event_message, dict):
            raise ValueError("Invalid user stream event message")

        async def _route_event(channel: str, data: Dict[str, Any]):
            if not channel:
                return

            if "futures-perp:orderFilled" in channel:
                await self._process_order_fill(data)
            elif "futures-perp:order" in channel:
                await self._process_order_update(data)
            elif "futures-perp:user" in channel:
                await self._process_account_update(data)
            elif "spot:orderFilled" in channel:
                await self._process_order_fill(data)
            elif "spot:order" in channel:
                await self._process_order_update(data)
            elif "spot:user" in channel or "spot:balance" in channel:
                await self._process_balance_update(data)
            elif "orderFilled" in channel:
                await self._process_order_fill(data)
            elif channel.startswith("order") or "order-" in channel:
                await self._process_order_update(data)
            elif "balance" in channel or "user" in channel:
                await self._process_balance_update(data)

        # Handle Centrifugo push message format
        if "push" in event_message:
            push_data = event_message.get("push", {})
            channel = push_data.get("channel", "")
            pub_data = push_data.get("pub", {})
            data = pub_data.get("data", {})
            await _route_event(channel, data)
        else:
            channel = event_message.get("channel", "")
            data = event_message.get("data", event_message)
            if isinstance(data, dict):
                await _route_event(channel, data)

    async def _process_account_update(self, account_data: Dict[str, Any]):
        """
        Process AccountEvent from user-{userExchangeId} channel.
        AccountEvent: { user: string, marginCall: boolean, updatedAt: string }
        """
        # Account updates indicate margin call status - log for now
        margin_call = account_data.get("marginCall", False)
        if margin_call:
            self.logger().warning("Margin call triggered!")

    async def _process_order_fill(self, fill_data: Dict[str, Any]):
        """
        Process OrderFill from orderFills-{userExchangeId} channel.
        OrderFill: { executionId, instrument, side, fillQuantity, fillPrice, createdAt, makerTakerFlag, orderId }
        """
        order_id = str(fill_data.get("orderId") or fill_data.get("order", ""))

        # Find tracked order by exchange_order_id
        tracked_order = None
        client_order_id = None
        for coid, order in self._order_tracker.all_fillable_orders.items():
            if order.exchange_order_id == order_id:
                tracked_order = order
                client_order_id = coid
                break

        if tracked_order is not None:
            flat_fees = self._parse_flat_fees(fill_data.get("fee", []))
            fee = self._build_trade_fee(tracked_order.trade_type, fill_data, flat_fees)

            trade_update: TradeUpdate = TradeUpdate(
                trade_id=str(fill_data.get("executionId", "")),
                client_order_id=client_order_id,
                exchange_order_id=order_id,
                trading_pair=tracked_order.trading_pair,
                fill_timestamp=time.time(),
                fill_price=Decimal(str(fill_data.get("fillPrice", 0))),
                fill_base_amount=Decimal(str(fill_data.get("fillQuantity", 0))),
                fill_quote_amount=Decimal(str(fill_data.get("fillQuantity", 0))) * Decimal(str(fill_data.get("fillPrice", 0))),
                fee=fee,
            )
            self._order_tracker.process_trade_update(trade_update)

    async def _process_order_update(self, order_data: Dict[str, Any]):
        # Order.id is the EXCHANGE order ID, not the client order ID
        exchange_order_id = str(order_data.get("id", ""))

        # Find tracked order by exchange_order_id
        tracked_order = None
        client_order_id = None
        for coid, order in self._order_tracker.all_fillable_orders.items():
            if order.exchange_order_id == exchange_order_id:
                tracked_order = order
                client_order_id = coid
                break

        if tracked_order is not None:
            # Calculate filled quantity from Order type fields: quantity - unFilledQuantity
            total_quantity = Decimal(str(order_data.get("quantity", 0)))
            unfilled_quantity = Decimal(str(order_data.get("unFilledQuantity", 0)))
            filled_quantity = total_quantity - unfilled_quantity

            # Only process if there's a new fill (compare with tracked order's executed amount)
            if filled_quantity > tracked_order.executed_amount_base:
                new_fill_amount = filled_quantity - tracked_order.executed_amount_base

                flat_fees = self._parse_flat_fees(order_data.get("fee", []))
                fee = self._build_trade_fee(tracked_order.trade_type, order_data, flat_fees)

                trade_update: TradeUpdate = TradeUpdate(
                    trade_id=f"{exchange_order_id}_{int(time.time() * 1000)}",
                    client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id,
                    trading_pair=tracked_order.trading_pair,
                    fill_timestamp=time.time(),
                    fill_price=Decimal(str(order_data.get("filledAvgPrice", 0))),
                    fill_base_amount=new_fill_amount,
                    fill_quote_amount=new_fill_amount * Decimal(str(order_data.get("filledAvgPrice", 0))),
                    fee=fee,
                )
                self._order_tracker.process_trade_update(trade_update)

        # Process order status update - find by exchange_order_id
        tracked_order = None
        client_order_id = None
        for coid, order in self._order_tracker.all_updatable_orders.items():
            if order.exchange_order_id == exchange_order_id:
                tracked_order = order
                client_order_id = coid
                break

        if tracked_order is not None:
            new_state = CONSTANTS.ORDER_STATE.get(order_data.get("status", ""), tracked_order.current_state)

            order_update: OrderUpdate = OrderUpdate(
                trading_pair=tracked_order.trading_pair,
                update_timestamp=time.time(),
                new_state=new_state,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
            )
            self._order_tracker.process_order_update(order_update)

    async def _process_balance_update(self, balance_data: Dict[str, Any]):
        # Handle AvailableBalance type structure from types.ts:
        # {
        #   currency: string,
        #   funding: { currency: string, balance: number },
        #   availableBalance: number,
        #   position: AvailableBalancePosition[],
        #   openOrder: AvailableBalanceOpenOrder[],
        #   updatedAt: string
        # }
        if isinstance(balance_data, dict):
            # Single AvailableBalance object
            funding = balance_data.get("funding", {})
            asset_name = str(balance_data.get("currency", funding.get("currency", "USDT"))).upper()
            total_balance = Decimal(str(funding.get("balance", 0)))
            available_balance = Decimal(str(balance_data.get("availableBalance", total_balance)))

            self._account_balances[asset_name] = total_balance
            self._account_available_balances[asset_name] = available_balance
        elif isinstance(balance_data, list):
            # List of WalletBalance objects: { currency, balance, availableBalance, balanceUSD }
            for balance in balance_data:
                asset_name = str(balance.get("currency", "USDT")).upper()
                total_balance = Decimal(str(balance.get("balance", 0)))
                available_balance = Decimal(str(balance.get("availableBalance", total_balance)))
                self._account_balances[asset_name] = total_balance
                self._account_available_balances[asset_name] = available_balance

    async def _update_order_fills_from_trades(self):
        last_tick = int(self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)
        current_tick = int(self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)

        if current_tick > last_tick and len(self._order_tracker.active_orders) > 0:
            try:
                active_orders = list(self._order_tracker.active_orders.values())
                orders_by_exchange_id = {
                    str(order.exchange_order_id): order
                    for order in active_orders
                    if order.exchange_order_id is not None
                }
                remaining_ids = set(orders_by_exchange_id.keys())
                if not remaining_ids:
                    return

                limit = 50
                offset = 0
                max_pages = 5
                total_count = None

                for _ in range(max_pages):
                    response = await self._api_get(
                        path_url=CONSTANTS.GET_ORDERS_PATH_URL,
                        params={
                            "status": "FILLED",
                            "groupStatus": "CLOSED",
                            "sort": "created-at-desc",
                            "offset": offset,
                            "limit": limit,
                        },
                        is_auth_required=True,
                        limit_id=CONSTANTS.GET_ORDERS_PATH_URL,
                    )
                    if isinstance(response, dict):
                        orders = response.get("list", []) or []
                        if total_count is None:
                            total_count = response.get("count")
                    else:
                        orders = response or []
                        if total_count is None:
                            total_count = len(orders)

                    if not orders:
                        break

                    for order_update in orders:
                        exchange_order_id = str(
                            order_update.get("id")
                            or order_update.get("order")
                            or order_update.get("orderId")
                            or ""
                        )
                        if exchange_order_id not in remaining_ids:
                            continue

                        tracked_order = orders_by_exchange_id.get(exchange_order_id)
                        if tracked_order is None:
                            continue

                        total_quantity = Decimal(str(order_update.get("quantity", 0)))
                        unfilled_quantity = Decimal(str(order_update.get("unFilledQuantity", 0)))
                        filled_quantity = total_quantity - unfilled_quantity
                        new_fill_amount = filled_quantity - tracked_order.executed_amount_base

                        if new_fill_amount > 0:
                            fill_price = Decimal(str(order_update.get("filledAvgPrice", 0)))
                            if fill_price > 0:
                                flat_fees = self._parse_flat_fees(order_update.get("fee", []))
                                fee = self._build_trade_fee(tracked_order.trade_type, order_update, flat_fees)

                                trade_update: TradeUpdate = TradeUpdate(
                                    trade_id=f"{exchange_order_id}_{int(time.time() * 1000)}",
                                    client_order_id=tracked_order.client_order_id,
                                    exchange_order_id=exchange_order_id,
                                    trading_pair=tracked_order.trading_pair,
                                    fill_timestamp=time.time(),
                                    fill_price=fill_price,
                                    fill_base_amount=new_fill_amount,
                                    fill_quote_amount=new_fill_amount * fill_price,
                                    fee=fee,
                                )
                                self._order_tracker.process_trade_update(trade_update)

                        remaining_ids.discard(exchange_order_id)

                    if not remaining_ids:
                        break

                    offset += limit
                    if total_count is not None and offset >= total_count:
                        break
                    if total_count is None and len(orders) < limit:
                        break
            except Exception as e:
                self.logger().network(
                    f"Error fetching trades update from order list: {e}.",
                    app_warning_msg="Failed to fetch trade update from order list."
                )
            self._last_trades_poll_timestamp = self.current_timestamp if self.current_timestamp > 0 else time.time()

    async def _update_order_status(self):
        """
        Calls the REST API to get order/trade updates for each fillable order.
        Uses the base implementation to include lost orders and trade updates.
        """
        await super()._update_order_status()

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates = []
        try:
            exchange_order_id = await order.get_exchange_order_id()
            order_update = await self._api_get(
                path_url=CONSTANTS.GET_ORDER_PATH_URL.format(orderId=exchange_order_id),
                is_auth_required=True,
                limit_id=CONSTANTS.GET_ORDER_PATH_URL,
            )
            self._order_status_cache[exchange_order_id] = order_update

            total_quantity = Decimal(str(order_update.get("quantity", 0)))
            unfilled_quantity = Decimal(str(order_update.get("unFilledQuantity", 0)))
            filled_quantity = total_quantity - unfilled_quantity
            new_fill_amount = filled_quantity - order.executed_amount_base

            if new_fill_amount > 0:
                fill_price = Decimal(str(order_update.get("filledAvgPrice", 0)))
                if fill_price > 0:
                    flat_fees = self._parse_flat_fees(order_update.get("fee", []))
                    fee = self._build_trade_fee(order.trade_type, order_update, flat_fees)

                    trade_update: TradeUpdate = TradeUpdate(
                        trade_id=f"{exchange_order_id}_{int(time.time() * 1000)}",
                        client_order_id=order.client_order_id,
                        exchange_order_id=exchange_order_id,
                        trading_pair=order.trading_pair,
                        fill_timestamp=time.time(),
                        fill_price=fill_price,
                        fill_base_amount=new_fill_amount,
                        fill_quote_amount=new_fill_amount * fill_price,
                        fee=fee,
                    )
                    trade_updates.append(trade_update)

        except asyncio.TimeoutError:
            raise IOError(f"Skipped order update with order fills for {order.client_order_id} "
                          "- waiting for exchange order id.")

        return trade_updates

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        exchange_order_id = await tracked_order.get_exchange_order_id()
        path_url = CONSTANTS.GET_ORDER_PATH_URL.format(orderId=exchange_order_id)

        order_update = self._order_status_cache.pop(exchange_order_id, None)
        if order_update is None:
            order_update = await self._api_get(
                path_url=path_url,
                is_auth_required=True,
                limit_id=CONSTANTS.GET_ORDER_PATH_URL)

        new_state = CONSTANTS.ORDER_STATE.get(order_update.get("status", ""), tracked_order.current_state)

        _order_update: OrderUpdate = OrderUpdate(
            trading_pair=tracked_order.trading_pair,
            update_timestamp=time.time(),
            new_state=new_state,
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=str(order_update.get("id", exchange_order_id)),
        )
        return _order_update

    async def _update_balances(self):
        """
        Calls the REST API to update total and available balances.
        """
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        # Get available balance info
        available_balance_info = await self._api_get(
            path_url=CONSTANTS.USER_BALANCE_PATH_URL,
            is_auth_required=True,
            limit_id=CONSTANTS.USER_BALANCE_PATH_URL)

        if isinstance(available_balance_info, list):
            for balance in available_balance_info:
                currency = str(balance.get("currency", "USDT")).upper()
                remote_asset_names.add(currency)
            await self._process_balance_update(available_balance_info)
        elif isinstance(available_balance_info, dict):
            # API returns: {"currency": "usdt", "funding": {"currency": "usdt", "balance": <num>}, "availableBalance": <num>, ...}
            funding = available_balance_info.get("funding", {})
            # Convert currency to uppercase as Hummingbot expects "USDT" not "usdt"
            currency = str(funding.get("currency", "usdt")).upper()
            # Total balance is in funding.balance
            balance = Decimal(str(funding.get("balance", 0)))
            # Available balance is at root level availableBalance
            available = Decimal(str(available_balance_info.get("availableBalance", 0)))

            self._account_balances[currency] = balance
            self._account_available_balances[currency] = available
            remote_asset_names.add(currency)

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        rules = exchange_info if isinstance(exchange_info, list) else exchange_info.get("list", [])

        for symbol_data in filter(web_utils.is_exchange_information_valid, rules):
            instrument_name = symbol_data.get("name", "")
            from_coin = symbol_data.get("from", {})
            to_coin = symbol_data.get("to", {})

            base = from_coin.get("symbol", "")
            quote = to_coin.get("symbol", "")

            if base and quote:
                trading_pair = combine_to_hb_trading_pair(base, quote)
                # Only convert USD to USDT if not already USDT
                if quote == "USD":
                    trading_pair = trading_pair.replace("-USD", "-USDT")
                mapping[instrument_name] = trading_pair

        self._set_trading_pair_symbol_map(mapping)

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        try:
            response = await self._api_get(
                path_url=CONSTANTS.INSTRUMENTS_PATH_URL,
                params={"fields": "metrics", "instrument": exchange_symbol},
            )
            if isinstance(response, list) and len(response) > 0:
                return float(response[0].get("lastPrice", 0))
            return float(response.get("lastPrice", 0))
        except Exception:
            self.logger().exception(f"Error fetching last traded price for {trading_pair} from EvedEx")
            raise
