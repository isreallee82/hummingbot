import asyncio
import time
import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Any, AsyncIterable, Dict, List, Optional, Tuple

from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.derivative.evedex_perpetual import (
    evedex_perpetual_constants as CONSTANTS,
    evedex_perpetual_web_utils as web_utils,
)
from hummingbot.connector.derivative.evedex_perpetual.evedex_perpetual_api_order_book_data_source import (
    EvedexPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.evedex_perpetual.evedex_perpetual_auth import EvedexPerpetualAuth
from hummingbot.connector.derivative.evedex_perpetual.evedex_perpetual_user_stream_data_source import (
    EvedexPerpetualUserStreamDataSource,
)
from hummingbot.connector.derivative.position import Position
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_gather
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

epm_logger = None


class EvedexPerpetualDerivative(PerpetualDerivativePyBase):
    web_utils = web_utils
    SHORT_POLL_INTERVAL = 5.0
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    LONG_POLL_INTERVAL = 120.0

    def __init__(
            self,
            balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
            rate_limits_share_pct: Decimal = Decimal("100"),
            evedex_perpetual_api_key: str = None,
            evedex_perpetual_private_key: str = None,
            trading_pairs: Optional[List[str]] = None,
            trading_required: bool = True,
            domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ):
        self.evedex_perpetual_api_key = evedex_perpetual_api_key
        self.evedex_perpetual_private_key = evedex_perpetual_private_key
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._domain = domain
        self._position_mode = PositionMode.ONEWAY  # Evedex uses one-way mode
        self._last_trade_history_timestamp = None
        self._auth: Optional[EvedexPerpetualAuth] = None
        super().__init__(balance_asset_limit, rate_limits_share_pct)

    @property
    def name(self) -> str:
        return CONSTANTS.EXCHANGE_NAME

    @property
    def authenticator(self) -> EvedexPerpetualAuth:
        if self._auth is None:
            self._auth = EvedexPerpetualAuth(
                api_key=self.evedex_perpetual_api_key,
                time_provider=self._time_synchronizer,
                private_key=self.evedex_perpetual_private_key or ""
            )
            self._auth.set_token_fetcher(self._fetch_access_token)
        return self._auth

    async def _fetch_access_token(self) -> dict:
        """
        Fetch the access token for WebSocket authentication from /api/dx-feed/auth.
        Returns the token data including 'token', 'tokenId', and 'expireAt'.
        """
        try:
            token_data = await self._api_get(
                path_url=CONSTANTS.DX_FEED_AUTH_PATH_URL,
                is_auth_required=True
            )
            return token_data
        except Exception as e:
            self.logger().warning(f"Failed to fetch access token: {e}")
            return {}

    @property
    def rate_limits_rules(self) -> List[RateLimit]:
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def client_order_id_max_length(self) -> int:
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self) -> str:
        return CONSTANTS.HBOT_ORDER_ID_PREFIX

    @property
    def trading_rules_request_path(self) -> str:
        return CONSTANTS.INSTRUMENTS_PATH_URL

    @property
    def trading_pairs_request_path(self) -> str:
        return CONSTANTS.INSTRUMENTS_PATH_URL

    @property
    def check_network_request_path(self) -> str:
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

    @property
    def funding_fee_poll_interval(self) -> int:
        return 600

    def supported_order_types(self) -> List[OrderType]:
        """
        :return a list of OrderType supported by this connector
        """
        return [OrderType.LIMIT, OrderType.MARKET]

    def supported_position_modes(self):
        """
        Evedex supports one-way position mode
        """
        return [PositionMode.ONEWAY]

    def get_buy_collateral_token(self, trading_pair: str) -> str:
        trading_rule: TradingRule = self._trading_rules[trading_pair]
        return trading_rule.buy_order_collateral_token

    def get_sell_collateral_token(self, trading_pair: str) -> str:
        trading_rule: TradingRule = self._trading_rules[trading_pair]
        return trading_rule.sell_order_collateral_token

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
        # Evedex doesn't use timestamp-based authentication
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return CONSTANTS.ORDER_NOT_EXIST_MESSAGE in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return CONSTANTS.ORDER_NOT_EXIST_MESSAGE in str(cancelation_exception)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            domain=self._domain,
            auth=self._auth)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return EvedexPerpetualAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return EvedexPerpetualUserStreamDataSource(
            auth=self._auth,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 position_action: PositionAction,
                 amount: Decimal,
                 price: Decimal = s_decimal_NaN,
                 is_maker: Optional[bool] = None) -> TradeFeeBase:
        is_maker = is_maker or False
        fee = build_trade_fee(
            self.name,
            is_maker,
            base_currency=base_currency,
            quote_currency=quote_currency,
            order_type=order_type,
            order_side=order_side,
            amount=amount,
            price=price,
        )
        return fee

    async def _update_trading_fees(self):
        """
        Update fees information from the exchange
        """
        pass

    async def _status_polling_loop_fetch_updates(self):
        await safe_gather(
            self._update_order_fills_from_trades(),
            self._update_order_status(),
            self._update_balances(),
            self._update_positions(),
        )

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
            position_action: PositionAction = PositionAction.NIL,
            **kwargs,
    ) -> Tuple[str, float]:
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

        # Generate Evedex-compatible order ID
        evedex_order_id = self._generate_order_id()

        # Get leverage
        leverage = self.get_leverage(trading_pair)
        side = CONSTANTS.SIDE_BUY if trade_type is TradeType.BUY else CONSTANTS.SIDE_SELL
        chain_id = CONSTANTS.CHAIN_ID

        cash_quantity = None
        limit_id = None
        if position_action == PositionAction.CLOSE:
            path_url = CONSTANTS.CLOSE_POSITION_PATH_URL.format(instrument=symbol)
            limit_id = CONSTANTS.CLOSE_POSITION_PATH_URL
            api_params = {
                "id": evedex_order_id,
                "instrument": symbol,
                "leverage": int(leverage),
                "quantity": str(amount),
                "chainId": chain_id,
            }
        elif order_type == OrderType.MARKET:
            path_url = CONSTANTS.MARKET_ORDER_PATH_URL
            cash_quantity = amount * price if price != s_decimal_NaN else amount

            api_params = {
                "id": evedex_order_id,
                "instrument": symbol,
                "side": side,
                "cashQuantity": str(cash_quantity),
                "timeInForce": CONSTANTS.TIME_IN_FORCE_IOC,
                "leverage": int(leverage),
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
                "leverage": int(leverage),
                "chainId": chain_id,
            }

        # Add EIP-712 signature (required by EvedEx)
        if self.authenticator.wallet_address is None:
            raise ValueError(
                "EvedEx requires a private key for order signing. "
                "Please configure evedex_perpetual_private_key in your connector settings."
            )

        if position_action == PositionAction.CLOSE:
            api_params["signature"] = self.authenticator.sign_position_close(
                order_id=evedex_order_id,
                instrument=symbol,
                leverage=int(leverage),
                quantity=amount,
                chain_id=chain_id,
            )
        elif order_type == OrderType.MARKET:
            api_params["signature"] = self.authenticator.sign_market_order(
                order_id=evedex_order_id,
                instrument=symbol,
                side=side,
                time_in_force=CONSTANTS.TIME_IN_FORCE_IOC,
                leverage=int(leverage),
                cash_quantity=cash_quantity,
                chain_id=chain_id,
            )
        else:
            api_params["signature"] = self.authenticator.sign_limit_order(
                order_id=evedex_order_id,
                instrument=symbol,
                side=side,
                leverage=int(leverage),
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

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates = []
        try:
            exchange_order_id = await order.get_exchange_order_id()
            path_url = CONSTANTS.GET_ORDER_PATH_URL.format(orderId=exchange_order_id) + "/fill"

            all_fills_response = await self._api_get(
                path_url=path_url,
                is_auth_required=True,
                limit_id=CONSTANTS.GET_ORDER_PATH_URL)

            for trade in all_fills_response:
                position_action = PositionAction.OPEN if order.trade_type is TradeType.BUY else PositionAction.CLOSE

                fee_list = trade.get("fee", [])
                flat_fees = []
                for fee_item in fee_list:
                    coin = str(fee_item.get("coin", "USDT")).upper()
                    if coin == "TOTAL":
                        continue
                    amount = Decimal(str(fee_item.get("quantity", 0)))
                    if amount == 0:
                        continue
                    flat_fees.append(TokenAmount(amount=amount, token=coin))

                fee = TradeFeeBase.new_perpetual_fee(
                    fee_schema=self.trade_fee_schema(),
                    position_action=position_action,
                    percent_token="USDT",
                    flat_fees=flat_fees
                )

                trade_update: TradeUpdate = TradeUpdate(
                    trade_id=str(trade.get("executionId", "")),
                    client_order_id=order.client_order_id,
                    exchange_order_id=exchange_order_id,
                    trading_pair=order.trading_pair,
                    fill_timestamp=time.time(),
                    fill_price=Decimal(str(trade.get("fillPrice", 0))),
                    fill_base_amount=Decimal(str(trade.get("fillQuantity", 0))),
                    fill_quote_amount=Decimal(str(trade.get("fillQuantity", 0))) * Decimal(str(trade.get("fillPrice", 0))),
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
            except Exception as e:
                self.logger().error(f"Unexpected error in user stream listener loop: {e}", exc_info=True)
                await self._sleep(5.0)

    async def _process_user_stream_event(self, event_message: Dict[str, Any]):
        """
        Process user stream events from Centrifugo.

        Handles both push format and direct format:
        - Push: {"push": {"channel": "futures-perp:order:123", "pub": {"data": {...}}}}
        - Direct: {"channel": "futures-perp:order:123", "data": {...}}
        """
        # Handle Centrifugo push message format
        if "push" in event_message:
            push_data = event_message.get("push", {})
            channel = push_data.get("channel", "")
            pub_data = push_data.get("pub", {})
            data = pub_data.get("data", {})

            # Centrifugo channel patterns: futures-perp:{type}:{userExchangeId}
            if "futures-perp:order" in channel and "futures-perp:orderFilled" not in channel:
                await self._process_order_update(data)
            elif "futures-perp:position" in channel:
                await self._process_position_update(data)
            elif "futures-perp:user" in channel:
                # user channel provides AccountEvent
                await self._process_account_update(data)
            elif "futures-perp:orderFilled" in channel:
                await self._process_order_fill(data)
            elif "futures-perp:position" in channel:
                await self._process_funding_update(data)

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
        order_id = str(fill_data.get("orderId", ""))

        # Find tracked order by exchange_order_id
        tracked_order = None
        client_order_id = None
        for coid, order in self._order_tracker.all_fillable_orders.items():
            if order.exchange_order_id == order_id:
                tracked_order = order
                client_order_id = coid
                break

        if tracked_order is not None:
            position_action = PositionAction.OPEN if tracked_order.trade_type is TradeType.BUY else PositionAction.CLOSE

            fee = TradeFeeBase.new_perpetual_fee(
                fee_schema=self.trade_fee_schema(),
                position_action=position_action,
                percent_token="USDT",
                flat_fees=[],
            )

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

    async def _process_funding_update(self, funding_data: Dict[str, Any]):
        """
        Process FundingEvent from funding-{userExchangeId} channel.
        FundingEvent: { user: string, coin: string, quantity: string, updatedAt: string }
        """
        # Funding updates can be used to track funding payments
        coin = funding_data.get("coin", "")
        quantity = Decimal(str(funding_data.get("quantity", 0)))
        self.logger().info(f"Funding update: {quantity} {coin}")

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
                position_action = PositionAction.OPEN if tracked_order.trade_type is TradeType.BUY else PositionAction.CLOSE

                fee_list = order_data.get("fee", [])
                flat_fees = []
                for fee_item in fee_list:
                    coin = str(fee_item.get("coin", "USDT")).upper()
                    self.logger().debug(f"Processing fee item: {fee_item}, parsed coin: {coin}")
                    if coin == "TOTAL":
                        continue
                    amount = Decimal(str(fee_item.get("quantity", 0)))
                    if amount == 0:
                        continue
                    flat_fees.append(TokenAmount(amount=amount, token=coin))

                fee = TradeFeeBase.new_perpetual_fee(
                    fee_schema=self.trade_fee_schema(),
                    position_action=position_action,
                    percent_token="USDT",
                    flat_fees=flat_fees,
                )

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

    async def _process_position_update(self, position_data: Dict[str, Any]):
        positions = position_data if isinstance(position_data, list) else [position_data]

        for position in positions:
            instrument = position.get("instrument", "")
            try:
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(instrument)
            except KeyError:
                continue

            side = PositionSide.LONG if position.get("side") == "BUY" else PositionSide.SHORT
            amount = Decimal(str(position.get("quantity", 0)))
            pos_key = self._perpetual_trading.position_key(trading_pair, side)

            if amount != 0:
                _position = Position(
                    trading_pair=trading_pair,
                    position_side=side,
                    unrealized_pnl=Decimal(str(position.get("unRealizedPnL", 0))),
                    entry_price=Decimal(str(position.get("avgPrice", 0))),
                    amount=amount,
                    leverage=Decimal(str(position.get("leverage", 1)))
                )
                self._perpetual_trading.set_position(pos_key, _position)
            else:
                self._perpetual_trading.remove_position(pos_key)

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
            asset_name = balance_data.get("currency", funding.get("currency", "USDT"))
            total_balance = Decimal(str(funding.get("balance", 0)))
            available_balance = Decimal(str(balance_data.get("availableBalance", total_balance)))

            self._account_balances[asset_name] = total_balance
            self._account_available_balances[asset_name] = available_balance
        elif isinstance(balance_data, list):
            # List of WalletBalance objects: { currency, balance, balanceUSD }
            for balance in balance_data:
                asset_name = balance.get("currency", "USDT")
                total_balance = Decimal(str(balance.get("balance", 0)))
                self._account_balances[asset_name] = total_balance
                if asset_name not in self._account_available_balances:
                    self._account_available_balances[asset_name] = total_balance

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        """
        Queries the necessary API endpoint and initialize the TradingRule object for each trading pair being traded.
        """
        rules = exchange_info_dict if isinstance(exchange_info_dict, list) else exchange_info_dict.get("list", [])
        return_val: list = []

        for rule in rules:
            try:
                if web_utils.is_exchange_information_valid(rule):
                    instrument_name = rule.get("name", "")
                    trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=instrument_name)

                    min_order_size = Decimal(str(rule.get("minQuantity", "0.001")))
                    tick_size = Decimal(str(rule.get("priceIncrement", "0.01")))
                    step_size = Decimal(str(rule.get("quantityIncrement", "0.001")))
                    # Instrument type doesn't have minVolume - calculate from minQuantity * minPrice
                    min_price = Decimal(str(rule.get("minPrice", "0.01")))
                    min_notional = min_order_size * min_price if min_price > 0 else Decimal("10")

                    # Get quote asset from the instrument
                    # to_coin = rule.get("to", {})
                    collateral_token = "USDT"

                    return_val.append(
                        TradingRule(
                            trading_pair,
                            min_order_size=min_order_size,
                            min_price_increment=tick_size,
                            min_base_amount_increment=step_size,
                            min_notional_size=min_notional,
                            buy_order_collateral_token=collateral_token,
                            sell_order_collateral_token=collateral_token,
                        )
                    )
            except Exception as e:
                self.logger().error(
                    f"Error parsing the trading pair rule {rule}. Error: {e}. Skipping...", exc_info=True
                )
        return return_val

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
        # Prefer the in-memory order book to avoid blocking REST calls during startup.
        try:
            mid_price = self.get_mid_price(trading_pair)
            if mid_price is not None and not mid_price.is_nan() and mid_price > 0:
                return float(mid_price)
        except Exception:
            pass

        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        try:
            response = await self._api_get(
                path_url=CONSTANTS.INSTRUMENTS_PATH_URL,
                params={"fields": "metrics"},
            )
            instruments = response if isinstance(response, list) else [response]
            for instrument in instruments:
                symbol = instrument.get("name")
                price = instrument.get("lastPrice")
                if symbol == exchange_symbol and price:
                    return float(price)
        except Exception:
            self.logger().exception(f"Error fetching last traded price for {trading_pair} from EvedEx")
        return 0.0

    async def _update_balances(self):
        """
        Calls the REST API to update total and available balances.
        """
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        # Get available balance info
        available_balance_info = await self._api_get(
            path_url=CONSTANTS.AVAILABLE_BALANCE_PATH_URL,
            is_auth_required=True,
            limit_id=CONSTANTS.AVAILABLE_BALANCE_PATH_URL)

        # Process funding balance
        # API returns: {"currency": "usdt", "funding": {"currency": "usdt", "balance": <num>}, "availableBalance": <num>, ...}
        funding = available_balance_info.get("funding", {})
        # Convert currency to uppercase as Hummingbot expects "USDT" not "usdt"
        currency = funding.get("currency", "usdt").upper()
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

    async def _update_positions(self):
        positions_response = await self._api_get(
            path_url=CONSTANTS.POSITIONS_PATH_URL,
            is_auth_required=True)

        positions = positions_response.get("list", []) if isinstance(positions_response, dict) else positions_response

        for position in positions:
            instrument = position.get("instrument", "")
            try:
                hb_trading_pair = await self.trading_pair_associated_to_exchange_symbol(instrument)
            except KeyError:
                continue

            position_side = PositionSide.LONG if position.get("side") == "BUY" else PositionSide.SHORT
            unrealized_pnl = Decimal(str(position.get("unRealizedPnL", 0)))
            entry_price = Decimal(str(position.get("avgPrice", 0)))
            amount = Decimal(str(position.get("quantity", 0)))
            leverage = Decimal(str(position.get("leverage", 1)))
            pos_key = self._perpetual_trading.position_key(hb_trading_pair, position_side)

            if amount != 0:
                _position = Position(
                    trading_pair=hb_trading_pair,
                    position_side=position_side,
                    unrealized_pnl=unrealized_pnl,
                    entry_price=entry_price,
                    amount=amount,
                    leverage=leverage
                )
                self._perpetual_trading.set_position(pos_key, _position)
            else:
                self._perpetual_trading.remove_position(pos_key)

    async def _update_order_fills_from_trades(self):
        last_tick = int(self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)
        current_tick = int(self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)

        if current_tick > last_tick and len(self._order_tracker.active_orders) > 0:
            trading_pairs_to_order_map: Dict[str, Dict[str, Any]] = defaultdict(lambda: {})
            for order in self._order_tracker.active_orders.values():
                trading_pairs_to_order_map[order.trading_pair][order.exchange_order_id] = order

            trading_pairs = list(trading_pairs_to_order_map.keys())

            # Use the fill endpoint to get recent fills
            for trading_pair in trading_pairs:
                try:
                    exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                    fills = await self._api_get(
                        path_url=CONSTANTS.ORDER_FILLS_PATH_URL,
                        params={
                            "instrument": exchange_symbol,
                            "after": "2020-01-01T00:00:00Z",
                            "before": "2030-01-01T00:00:00Z"
                        },
                        is_auth_required=True)

                    fill_list = fills.get("list", []) if isinstance(fills, dict) else fills
                    order_map = trading_pairs_to_order_map.get(trading_pair, {})

                    for fill in fill_list:
                        order_id = str(fill.get("orderId", ""))
                        if order_id in order_map:
                            tracked_order: InFlightOrder = order_map.get(order_id)
                            position_action = PositionAction.OPEN if tracked_order.trade_type is TradeType.BUY else PositionAction.CLOSE

                            fee = TradeFeeBase.new_perpetual_fee(
                                fee_schema=self.trade_fee_schema(),
                                position_action=position_action,
                                percent_token="USDT",
                                flat_fees=[]
                            )

                            trade_update: TradeUpdate = TradeUpdate(
                                trade_id=str(fill.get("executionId", "")),
                                client_order_id=tracked_order.client_order_id,
                                exchange_order_id=order_id,
                                trading_pair=trading_pair,
                                fill_timestamp=time.time(),
                                fill_price=Decimal(str(fill.get("fillPrice", 0))),
                                fill_base_amount=Decimal(str(fill.get("fillQuantity", 0))),
                                fill_quote_amount=Decimal(str(fill.get("fillQuantity", 0))) * Decimal(str(fill.get("fillPrice", 0))),
                                fee=fee,
                            )
                            self._order_tracker.process_trade_update(trade_update)
                except Exception as e:
                    self.logger().network(
                        f"Error fetching trades update for {trading_pair}: {e}.",
                        app_warning_msg=f"Failed to fetch trade update for {trading_pair}."
                    )

    async def _update_order_status(self):
        """
        Calls the REST API to get order/trade updates for each in-flight order.
        """
        last_tick = int(self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)
        current_tick = int(self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)

        if current_tick > last_tick and len(self._order_tracker.active_orders) > 0:
            tracked_orders = list(self._order_tracker.active_orders.values())

            for tracked_order in tracked_orders:
                try:
                    exchange_order_id = await tracked_order.get_exchange_order_id()
                    path_url = CONSTANTS.GET_ORDER_PATH_URL.format(orderId=exchange_order_id)

                    order_update = await self._api_get(
                        path_url=path_url,
                        is_auth_required=True,
                        limit_id=CONSTANTS.GET_ORDER_PATH_URL)

                    new_state = CONSTANTS.ORDER_STATE.get(order_update.get("status", ""), tracked_order.current_state)

                    new_order_update: OrderUpdate = OrderUpdate(
                        trading_pair=tracked_order.trading_pair,
                        update_timestamp=time.time(),
                        new_state=new_state,
                        client_order_id=tracked_order.client_order_id,
                        exchange_order_id=str(order_update.get("id", exchange_order_id)),
                    )
                    self._order_tracker.process_order_update(new_order_update)

                except Exception as e:
                    self.logger().network(
                        f"Error fetching status update for order {tracked_order.client_order_id}: {e}."
                    )

    async def _get_position_mode(self) -> Optional[PositionMode]:
        # Evedex uses one-way position mode
        return PositionMode.ONEWAY

    async def _trading_pair_position_mode_set(self, mode: PositionMode, trading_pair: str) -> Tuple[bool, str]:
        # Evedex only supports one-way mode
        if mode == PositionMode.ONEWAY:
            return True, ""
        return False, "Evedex only supports one-way position mode"

    async def _set_trading_pair_leverage(self, trading_pair: str, leverage: int) -> Tuple[bool, str]:
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair)
        path_url = CONSTANTS.SET_LEVERAGE_PATH_URL.format(instrument=symbol)

        try:
            set_leverage_result = await self._api_put(
                path_url=path_url,
                data={"leverage": leverage},
                is_auth_required=True,
                limit_id=CONSTANTS.SET_LEVERAGE_PATH_URL,
            )

            if set_leverage_result.get("leverage") == leverage:
                return True, ""
            return True, ""  # Leverage set successfully
        except Exception as e:
            return False, f"Unable to set leverage: {str(e)}"

    async def _fetch_last_fee_payment(self, trading_pair: str) -> Tuple[int, Decimal, Decimal]:
        """
        Fetches the last funding fee payment for a trading pair.
        """
        try:
            exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)

            # Get funding info from user endpoint
            funding_response = await self._api_get(
                path_url=CONSTANTS.POSITIONS_PATH_URL,
                is_auth_required=True,
                limit_id=CONSTANTS.POSITIONS_PATH_URL)

            # Initialize default values
            timestamp = 0
            funding_rate = Decimal("-1")
            payment = Decimal("-1")

            for funding in funding_response.get("list", []):
                if funding.get("coin") == exchange_symbol.split("-")[0]:
                    payment = Decimal(str(funding.get("quantity", 0)))
                    funding_rate = Decimal(str(funding.get("fundingRate", 0)))
                    timestamp = funding.get("updatedAt", 0)
                    break

            return timestamp, funding_rate, payment

        except Exception as e:
            self.logger().error(f"Error fetching funding fee: {e}")
            return 0, Decimal("-1"), Decimal("-1")
