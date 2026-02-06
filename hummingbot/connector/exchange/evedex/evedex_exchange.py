"""Evedex Exchange Connector."""
import asyncio
import time
import uuid
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
                limit_id=CONSTANTS.INSTRUMENTS_PATH_URL,
            )
            instruments = response if isinstance(response, list) else [response]
            for instrument in instruments:
                symbol = instrument.get("name") or instrument.get("instrument") or instrument.get("symbol")
                price = instrument.get("lastPrice") or instrument.get("markPrice")
                if symbol and price is not None:
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
        """Generate Evedex-compatible order ID in format: XXXXX:XXXXXXXXXXXXXXXXXXXXXXXXXX"""
        prefix = str(int(time.time() * 1000) % 100000).zfill(5)
        suffix = uuid.uuid4().hex[:26].upper()
        return f"{prefix}:{suffix}"

    async def _place_order(self,
                           order_id: str,
                           trading_pair: str,
                           amount: Decimal,
                           trade_type: TradeType,
                           order_type: OrderType,
                           price: Decimal,
                           **kwargs) -> Tuple[str, float]:
        amount_str = f"{amount:f}"
        side_str = CONSTANTS.SIDE_BUY if trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

        # Generate Evedex-compatible order ID
        evedex_order_id = self._generate_order_id()
        chain_id = CONSTANTS.CHAIN_ID
        leverage = int(kwargs.get("leverage", 1))

        if order_type == OrderType.MARKET:
            path_url = CONSTANTS.MARKET_ORDER_PATH_URL
            cash_quantity = amount * price if price != s_decimal_NaN else amount
            api_params = {
                "instrument": symbol,
                "side": side_str,
                "cashQuantity": str(cash_quantity),
                "timeInForce": CONSTANTS.TIME_IN_FORCE_IOC,
                "id": evedex_order_id,
                "leverage": leverage,
                "chainId": chain_id,
            }
        else:
            path_url = CONSTANTS.LIMIT_ORDER_PATH_URL
            price_str = f"{price:f}"
            api_params = {
                "instrument": symbol,
                "side": side_str,
                "quantity": amount_str,
                "limitPrice": price_str,
                "timeInForce": CONSTANTS.TIME_IN_FORCE_GTC,
                "id": evedex_order_id,
                "leverage": leverage,
                "chainId": chain_id,
            }

        if self.authenticator.wallet_address is None:
            raise ValueError(
                "EvedEx requires a private key for order signing. "
                "Please configure evedex_private_key in your connector settings."
            )

        if order_type == OrderType.MARKET:
            api_params["signature"] = self.authenticator.sign_market_order(
                order_id=evedex_order_id,
                instrument=symbol,
                side=side_str,
                time_in_force=CONSTANTS.TIME_IN_FORCE_IOC,
                leverage=leverage,
                cash_quantity=cash_quantity,
                chain_id=chain_id,
            )
        else:
            api_params["signature"] = self.authenticator.sign_limit_order(
                order_id=evedex_order_id,
                instrument=symbol,
                side=side_str,
                leverage=leverage,
                quantity=amount,
                limit_price=price,
                chain_id=chain_id,
            )

        try:
            order_result = await self._api_post(
                path_url=path_url,
                data=api_params,
                is_auth_required=True)

            o_id = str(order_result.get("id", evedex_order_id))
            transact_time = order_result.get("createdAt", self._time_synchronizer.time())
        except IOError as e:
            error_description = str(e)
            if "503" in error_description:
                o_id = "UNKNOWN"
                transact_time = self._time_synchronizer.time()
            else:
                raise

        return o_id, transact_time

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        exchange_order_id = await tracked_order.get_exchange_order_id()
        path_url = CONSTANTS.CANCEL_ORDER_PATH_URL.format(orderId=exchange_order_id)

        await self._api_delete(
            path_url=path_url,
            is_auth_required=True,
            limit_id=CONSTANTS.CANCEL_ORDER_PATH_URL)

        return True

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        """
        Queries the necessary API endpoint and initialize the TradingRule object for each trading pair being traded.
        """
        rules = exchange_info_dict if isinstance(exchange_info_dict, list) else exchange_info_dict.get("list", [])
        retval = []

        for rule in rules:
            try:
                required_fields = ("minQuantity", "priceIncrement", "quantityIncrement")
                if any(field not in rule for field in required_fields):
                    raise KeyError(f"Missing required fields: {required_fields}")

                instrument_name = rule.get("name", "")
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=instrument_name)

                min_order_size = Decimal(str(rule.get("minQuantity", "0.001")))
                tick_size = Decimal(str(rule.get("priceIncrement", "0.01")))
                step_size = Decimal(str(rule.get("quantityIncrement", "0.001")))
                min_notional = Decimal(str(rule.get("minVolume", "0")))
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
                self.logger().exception(f"Error parsing the trading pair rule {rule}. Skipping.")

        return retval

    async def _status_polling_loop_fetch_updates(self):
        await self._update_order_fills_from_trades()
        await super()._status_polling_loop_fetch_updates()

    async def _update_trading_fees(self):
        """Update fees information from the exchange."""
        pass

    async def _user_stream_event_listener(self):
        """
        Process events from the user stream.
        Uses Centrifuge channel naming: {type}-{userExchangeId}
        """
        async for event_message in self._iter_user_event_queue():
            try:
                # Handle Centrifugo push message format
                if "push" in event_message:
                    push_data = event_message.get("push", {})
                    channel = push_data.get("channel", "")
                    pub_data = push_data.get("pub", {})
                    data = pub_data.get("data", {})

                # Centrifuge channels: order-{id}, user-{id}, orderFills-{id}
                if "order-" in channel and "orderFilled" not in channel:
                    await self._process_order_update(data)
                elif "user-" in channel:
                    await self._process_account_update(data)
                elif "orderFilled-" in channel:
                    await self._process_order_fill(data)

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error in user stream listener loop.", exc_info=True)
                await self._sleep(5.0)

    async def _process_account_update(self, account_data: Dict[str, Any]):
        """
        Process account update from user-{userExchangeId} channel.

        User channel data structure (AvailableBalance):
        {
            currency: string,
            funding: { currency: string, balance: number },
            availableBalance: number,
            position: AvailableBalancePosition[],
            openOrder: AvailableBalanceOpenOrder[],
            updatedAt: string
        }
        """
        await self._process_balance_update(account_data)

    async def _process_order_fill(self, fill_data: Dict[str, Any]):
        """
        Process order fill from orderFills-{userExchangeId} channel.

        OrderFill structure from types.ts:
        {
            orderId: string,
            instrumentName: string,
            executionId: string,
            side: string,
            fillPrice: number,
            fillQuantity: number,
            fillValue: number,
            fee: TradingFee[],
            pnl?: number,
            isPnlRealized?: boolean,
            createdAt: string
        }
        """
        # orderId is the exchange order ID
        exchange_order_id = str(fill_data.get("orderId", ""))

        # Find tracked order by exchange_order_id
        tracked_order = None
        client_order_id = None
        for coid, order in self._order_tracker.all_fillable_orders.items():
            if order.exchange_order_id == exchange_order_id:
                tracked_order = order
                client_order_id = coid
                break

        if tracked_order is not None:
            fill_quantity = Decimal(str(fill_data.get("fillQuantity", 0)))
            fill_price = Decimal(str(fill_data.get("fillPrice", 0)))
            fill_value = Decimal(str(fill_data.get("fillValue", 0)))

            # Use executionId for trade_id (unique identifier for this fill)
            execution_id = fill_data.get("executionId", f"{exchange_order_id}_{int(time.time() * 1000)}")

            fee_list = fill_data.get("fee", [])
            flat_fees = []
            for fee_item in fee_list:
                flat_fees.append(TokenAmount(
                    amount=Decimal(str(fee_item.get("quantity", 0))),
                    token=fee_item.get("coin", "USDT")
                ))

            fee = TradeFeeBase.new_spot_fee(
                fee_schema=self.trade_fee_schema(),
                trade_type=tracked_order.trade_type,
                percent_token=tracked_order.quote_asset,
                flat_fees=flat_fees
            )

            trade_update = TradeUpdate(
                trade_id=str(execution_id),
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                trading_pair=tracked_order.trading_pair,
                fee=fee,
                fill_base_amount=fill_quantity,
                fill_quote_amount=fill_value if fill_value > 0 else fill_quantity * fill_price,
                fill_price=fill_price,
                fill_timestamp=time.time(),
            )
            self._order_tracker.process_trade_update(trade_update)

    async def _process_order_update(self, order_data: Dict[str, Any]):
        """Process order update from user stream."""
        # Order.id is the EXCHANGE order ID, not the client order ID
        exchange_order_id = str(order_data.get("id", ""))

        # Find tracked order by exchange_order_id for fill processing
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
                fee_list = order_data.get("fee", [])
                flat_fees = []
                for fee_item in fee_list:
                    flat_fees.append(TokenAmount(
                        amount=Decimal(str(fee_item.get("quantity", 0))),
                        token=fee_item.get("coin", "USDT")
                    ))

                fee = TradeFeeBase.new_spot_fee(
                    fee_schema=self.trade_fee_schema(),
                    trade_type=tracked_order.trade_type,
                    percent_token=tracked_order.quote_asset,
                    flat_fees=flat_fees
                )

                trade_update = TradeUpdate(
                    trade_id=f"{exchange_order_id}_{int(time.time() * 1000)}",
                    client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id,
                    trading_pair=tracked_order.trading_pair,
                    fee=fee,
                    fill_base_amount=new_fill_amount,
                    fill_quote_amount=new_fill_amount * Decimal(str(order_data.get("filledAvgPrice", 0))),
                    fill_price=Decimal(str(order_data.get("filledAvgPrice", 0))),
                    fill_timestamp=time.time(),
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

            order_update = OrderUpdate(
                trading_pair=tracked_order.trading_pair,
                update_timestamp=time.time(),
                new_state=new_state,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
            )
            self._order_tracker.process_order_update(order_update=order_update)

    async def _process_balance_update(self, balance_data: Dict[str, Any]):
        """Process balance update from user stream."""
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
            asset_name = balance_data.get("currency", funding.get("currency", "USDT"))
            total_balance = Decimal(str(funding.get("balance", 0)))
            available_balance = Decimal(str(balance_data.get("availableBalance", total_balance)))

            self._account_balances[asset_name] = total_balance
            self._account_available_balances[asset_name] = available_balance
        elif isinstance(balance_data, list):
            # List of WalletBalance objects: { currency, balance, balanceUSD }
            for balance_entry in balance_data:
                asset_name = balance_entry.get("currency", "USDT")
                total_balance = Decimal(str(balance_entry.get("balance", 0)))
                self._account_balances[asset_name] = total_balance
                if asset_name not in self._account_available_balances:
                    self._account_available_balances[asset_name] = total_balance

    async def _update_order_fills_from_trades(self):
        """
        Update order fills from trades endpoint.
        """
        small_interval_last_tick = self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL
        small_interval_current_tick = self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL
        long_interval_last_tick = self._last_poll_timestamp / self.LONG_POLL_INTERVAL
        long_interval_current_tick = self.current_timestamp / self.LONG_POLL_INTERVAL

        if (long_interval_current_tick > long_interval_last_tick
                or (self.in_flight_orders and small_interval_current_tick > small_interval_last_tick)):

            self._last_trades_poll_timestamp = self._time_synchronizer.time()
            order_by_exchange_id_map = {}

            for order in self._order_tracker.all_fillable_orders.values():
                order_by_exchange_id_map[order.exchange_order_id] = order

            for trading_pair in self.trading_pairs:
                try:
                    exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                    fills = await self._api_get(
                        path_url=CONSTANTS.ORDER_FILLS_PATH_URL,
                        params={"instrument": exchange_symbol},
                        is_auth_required=True)

                    fill_list = fills.get("list", []) if isinstance(fills, dict) else fills

                    for fill in fill_list:
                        order_id = str(fill.get("orderId") or fill.get("order", ""))
                        if order_id in order_by_exchange_id_map:
                            tracked_order = order_by_exchange_id_map[order_id]
                            fee_list = fill.get("fee", [])
                            flat_fees = []
                            for fee_item in fee_list:
                                flat_fees.append(TokenAmount(
                                    amount=Decimal(str(fee_item.get("quantity", 0))),
                                    token=fee_item.get("coin", "USDT")
                                ))

                            fee = TradeFeeBase.new_spot_fee(
                                fee_schema=self.trade_fee_schema(),
                                trade_type=tracked_order.trade_type,
                                percent_token=tracked_order.quote_asset,
                                flat_fees=flat_fees
                            )

                            trade_update = TradeUpdate(
                                trade_id=str(fill.get("executionId") or fill.get("id", "")),
                                client_order_id=tracked_order.client_order_id,
                                exchange_order_id=order_id,
                                trading_pair=trading_pair,
                                fee=fee,
                                fill_base_amount=Decimal(str(fill.get("fillQuantity", 0))),
                                fill_quote_amount=Decimal(str(fill.get("fillQuantity", 0))) * Decimal(str(fill.get("fillPrice", 0))),
                                fill_price=Decimal(str(fill.get("fillPrice", 0))),
                                fill_timestamp=time.time(),
                            )
                            self._order_tracker.process_trade_update(trade_update)

                except Exception as e:
                    self.logger().network(
                        f"Error fetching trades update for {trading_pair}: {e}.",
                        app_warning_msg=f"Failed to fetch trade update for {trading_pair}."
                    )

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates = []

        if order.exchange_order_id is not None:
            try:
                exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=order.trading_pair)
                all_fills_response = await self._api_get(
                    path_url=CONSTANTS.ORDER_FILLS_PATH_URL,
                    params={"instrument": exchange_symbol},
                    is_auth_required=True,
                    limit_id=CONSTANTS.ORDER_FILLS_PATH_URL,
                )

                fill_list = all_fills_response.get("list", []) if isinstance(all_fills_response, dict) else all_fills_response
                for trade in fill_list:
                    exchange_order_id = str(trade.get("orderId") or trade.get("order", ""))
                    if exchange_order_id != order.exchange_order_id:
                        continue
                    fee_list = trade.get("fee", [])
                    flat_fees = []
                    for fee_item in fee_list:
                        flat_fees.append(TokenAmount(
                            amount=Decimal(str(fee_item.get("quantity", 0))),
                            token=fee_item.get("coin", "USDT")
                        ))

                    fee = TradeFeeBase.new_spot_fee(
                        fee_schema=self.trade_fee_schema(),
                        trade_type=order.trade_type,
                        percent_token=order.quote_asset,
                        flat_fees=flat_fees
                    )

                    trade_update = TradeUpdate(
                        trade_id=str(trade.get("executionId") or trade.get("id", "")),
                        client_order_id=order.client_order_id,
                        exchange_order_id=exchange_order_id,
                        trading_pair=order.trading_pair,
                        fee=fee,
                        fill_base_amount=Decimal(str(trade.get("fillQuantity", 0))),
                        fill_quote_amount=Decimal(str(trade.get("fillQuantity", 0))) * Decimal(str(trade.get("fillPrice", 0))),
                        fill_price=Decimal(str(trade.get("fillPrice", 0))),
                        fill_timestamp=time.time(),
                    )
                    trade_updates.append(trade_update)
            except Exception:
                pass

        return trade_updates

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        exchange_order_id = await tracked_order.get_exchange_order_id()
        path_url = CONSTANTS.GET_ORDER_PATH_URL.format(orderId=exchange_order_id)

        updated_order_data = await self._api_get(
            path_url=path_url,
            is_auth_required=True,
            limit_id=CONSTANTS.GET_ORDER_PATH_URL)

        new_state = CONSTANTS.ORDER_STATE.get(updated_order_data.get("status", ""), tracked_order.current_state)

        order_update = OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=str(updated_order_data.get("id", exchange_order_id)),
            trading_pair=tracked_order.trading_pair,
            update_timestamp=time.time(),
            new_state=new_state,
        )

        return order_update

    async def _update_balances(self):
        """Update account balances from exchange."""
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        try:
            account_info = await self._api_get(
                path_url=CONSTANTS.USER_BALANCE_PATH_URL,
                is_auth_required=True)

            balances = account_info if isinstance(account_info, list) else account_info.get("list", [account_info])

            for balance_entry in balances:
                asset_name = balance_entry.get("currency", "USDT")
                free_balance = Decimal(str(balance_entry.get("availableBalance", balance_entry.get("balance", 0))))
                total_balance = Decimal(str(balance_entry.get("balance", 0)))
                self._account_available_balances[asset_name] = free_balance
                self._account_balances[asset_name] = total_balance
                remote_asset_names.add(asset_name)

            asset_names_to_remove = local_asset_names.difference(remote_asset_names)
            for asset_name in asset_names_to_remove:
                del self._account_available_balances[asset_name]
                del self._account_balances[asset_name]
        except Exception as e:
            self.logger().network(
                f"Error fetching balances: {e}.",
                app_warning_msg="Failed to fetch balance update."
            )

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
                mapping[instrument_name] = trading_pair

        self._set_trading_pair_symbol_map(mapping)

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        params = {
            "instrument": await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair),
            "fields": "metrics"
        }

        resp_json = await self._api_get(
            path_url=CONSTANTS.INSTRUMENTS_PATH_URL,
            params=params)

        if isinstance(resp_json, list) and len(resp_json) > 0:
            return float(resp_json[0].get("lastPrice", 0))
        return float(resp_json.get("lastPrice", 0))
