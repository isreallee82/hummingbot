import asyncio
from decimal import Decimal
from typing import Any, Dict, Optional

from hummingbot.connector.gateway.gateway_base import GatewayBase
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, TokenAmount
from hummingbot.core.event.events import BuyOrderCreatedEvent, MarketEvent, SellOrderCreatedEvent
from hummingbot.core.utils import async_ttl_cache
from hummingbot.core.utils.async_utils import safe_ensure_future


class GatewaySwap(GatewayBase):
    """
    Handles swap-specific functionality including price quotes and trade execution.
    Maintains order tracking and wallet interactions in the base class.
    """

    @async_ttl_cache(ttl=5, maxsize=10)
    async def get_quote_price(
            self,
            trading_pair: str,
            is_buy: bool,
            amount: Decimal,
            slippage_pct: Optional[Decimal] = None,
            pool_address: Optional[str] = None
    ) -> Optional[Decimal]:
        """
        Retrieves the volume weighted average price. For an AMM DEX connectors, this is the swap price for a given amount.

        :param trading_pair: The market trading pair
        :param is_buy: True for an intention to buy, False for an intention to sell
        :param amount: The amount required (in base token unit)
        :return: The quote price.
        """
        base, quote = trading_pair.split("-")
        side: TradeType = TradeType.BUY if is_buy else TradeType.SELL

        # Pull the price from gateway.
        try:
            resp: Dict[str, Any] = await self._get_gateway_instance().quote_swap(
                network=self.network,
                connector=self.connector_name,
                base_asset=base,
                quote_asset=quote,
                amount=amount,
                side=side,
                slippage_pct=slippage_pct,
                pool_address=pool_address
            )
            price = resp.get("price", None)
            return Decimal(price) if price is not None else None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().network(
                f"Error getting quote price for {trading_pair} {side} order for {amount} amount.",
                exc_info=True,
                app_warning_msg=str(e)
            )

    async def get_order_price(
            self,
            trading_pair: str,
            is_buy: bool,
            amount: Decimal,
    ) -> Decimal:
        """
        Retreives the price required for an order of a given amount. For AMM DEX connectors, this equals the quote price.
        """
        return await self.get_quote_price(trading_pair, is_buy, amount)

    def buy(self, trading_pair: str, amount: Decimal, order_type: OrderType, price: Decimal, **kwargs) -> str:
        """
        Buys an amount of base token for a given price (or cheaper).
        :param trading_pair: The market trading pair
        :param amount: The order amount (in base token unit)
        :param order_type: Any order type is fine, not needed for this.
        :param price: The maximum price for the order.
        :param kwargs: Additional parameters like quote_id
        :return: A newly created order id (internal).
        """
        return self.place_order(True, trading_pair, amount, price, **kwargs)

    def sell(self, trading_pair: str, amount: Decimal, order_type: OrderType, price: Decimal, **kwargs) -> str:
        """
        Sells an amount of base token for a given price (or at a higher price).
        :param trading_pair: The market trading pair
        :param amount: The order amount (in base token unit)
        :param order_type: Any order type is fine, not needed for this.
        :param price: The minimum price for the order.
        :param kwargs: Additional parameters like quote_id
        :return: A newly created order id (internal).
        """
        return self.place_order(False, trading_pair, amount, price, **kwargs)

    def place_order(self, is_buy: bool, trading_pair: str, amount: Decimal, price: Decimal, **request_args) -> str:
        """
        Places an order.
        :param is_buy: True for buy order
        :param trading_pair: The market trading pair
        :param amount: The order amount (in base token unit)
        :param price: The minimum price for the order.
        :return: A newly created order id (internal).
        """
        side: TradeType = TradeType.BUY if is_buy else TradeType.SELL
        order_id: str = self.create_market_order_id(side, trading_pair)
        safe_ensure_future(self._create_order(side, order_id, trading_pair, amount, price, **request_args))
        return order_id

    async def _create_order(
            self,
            trade_type: TradeType,
            order_id: str,
            trading_pair: str,
            amount: Decimal,
            price: Decimal,
            **kwargs
    ):
        """
        Calls buy or sell API end point to place an order, starts tracking the order and triggers relevant order events.
        :param trade_type: BUY or SELL
        :param order_id: Internal order id (also called client_order_id)
        :param trading_pair: The market to place order
        :param amount: The order amount (in base token value)
        :param price: The order price (TO-DO: add limit_price to Gateway execute-swap schema)
        :param kwargs: Additional parameters like quote_id, pool_address, slippage_pct, max_retries
        """

        amount = self.quantize_order_amount(trading_pair, amount)
        price = self.quantize_order_price(trading_pair, price)

        base, quote = trading_pair.split("-")

        # Check if order is already being tracked (prevents duplicate events on retry)
        existing_order = self._order_tracker.fetch_order(order_id)
        if existing_order is not None:
            self.logger().debug(f"Order {order_id} already tracked, skipping event emission")
        else:
            self.start_tracking_order(order_id=order_id,
                                      trading_pair=trading_pair,
                                      trade_type=trade_type,
                                      price=price,
                                      amount=amount)

            # Emit order created event immediately so OrdersRecorder creates the order in DB
            event_class = BuyOrderCreatedEvent if trade_type == TradeType.BUY else SellOrderCreatedEvent
            event_tag = MarketEvent.BuyOrderCreated if trade_type == TradeType.BUY else MarketEvent.SellOrderCreated
            self.trigger_event(
                event_tag,
                event_class(
                    timestamp=self.current_timestamp,
                    type=OrderType.MARKET,
                    trading_pair=trading_pair,
                    amount=amount,
                    price=price,
                    order_id=order_id,
                    creation_timestamp=self.current_timestamp,
                    exchange_order_id=None,  # Not known yet
                )
            )

        # Extract optional parameters
        quote_id = kwargs.get("quote_id")
        pool_address = kwargs.get("pool_address")
        slippage_pct = kwargs.get("slippage_pct")
        max_retries = kwargs.get("max_retries", 10)

        async def execute_gateway_swap() -> Dict[str, Any]:
            if quote_id:
                # Use execute_quote if we have a quote_id
                return await self._get_gateway_instance().execute_quote(
                    connector=self.connector_name,
                    quote_id=quote_id,
                    network=self.network,
                    wallet_address=self.address
                )
            else:
                # Use execute_swap for direct swaps without quote
                return await self._get_gateway_instance().execute_swap(
                    connector=self.connector_name,
                    base_asset=base,
                    quote_asset=quote,
                    side=trade_type,
                    amount=amount,
                    network=self.network,
                    wallet_address=self.address,
                    pool_address=pool_address,
                    slippage_pct=slippage_pct
                )

        try:
            # Execute with retry logic for timeout errors
            order_result = await self._execute_with_retry(
                operation=execute_gateway_swap,
                operation_name=f"swap {trade_type.name} {amount} on {trading_pair}",
                max_retries=max_retries,
            )

            transaction_hash: Optional[str] = order_result.get("signature")
            if transaction_hash is not None and transaction_hash != "":
                self.update_order_from_hash(order_id, trading_pair, transaction_hash, order_result)
                # Store executed amounts for fill event
                self._store_swap_result(order_id, trade_type, trading_pair, amount, order_result, transaction_hash)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._handle_operation_failure(order_id, trading_pair, f"submitting {trade_type.name} swap order", e)

    def _store_swap_result(
        self,
        order_id: str,
        trade_type: TradeType,
        trading_pair: str,
        amount: Decimal,
        order_result: Dict[str, Any],
        transaction_hash: str
    ):
        """
        Store swap result data by creating a TradeUpdate for proper fill tracking.

        :param order_id: The order ID
        :param trade_type: BUY or SELL
        :param trading_pair: The trading pair
        :param amount: The original order amount
        :param order_result: The gateway response
        :param transaction_hash: The blockchain transaction hash/signature
        """
        data = order_result.get("data", {})
        amount_in = Decimal(str(data.get("amountIn", "0")))
        amount_out = Decimal(str(data.get("amountOut", "0")))

        # Calculate executed price based on trade type
        if trade_type == TradeType.SELL:
            executed_price = amount_out / amount_in if amount_in > 0 and amount_out > 0 else Decimal("0")
        else:  # BUY
            executed_price = amount_in / amount_out if amount_in > 0 and amount_out > 0 else Decimal("0")

        # Get the tracked order
        tracked_order = self._order_tracker.fetch_order(order_id)
        if not tracked_order:
            return

        # Create trade fee from gas cost
        fee = Decimal(str(data.get("fee", 0)))
        fee_asset = self._native_currency
        trade_fee = AddedToCostTradeFee(flat_fees=[TokenAmount(fee_asset, fee)])

        # For swaps, use the order's original amount as fill amount
        # This ensures check_filled_condition() passes even with slippage/fees
        # The actual executed_amount is still recorded in the fill_price calculation
        fill_base_amount = tracked_order.amount

        # Create TradeUpdate to properly register the fill
        trade_update = TradeUpdate(
            trade_id=transaction_hash,
            client_order_id=order_id,
            exchange_order_id=transaction_hash,
            trading_pair=trading_pair,
            fill_timestamp=self.current_timestamp,
            fill_price=executed_price,
            fill_base_amount=fill_base_amount,
            fill_quote_amount=fill_base_amount * executed_price,
            fee=trade_fee
        )

        # Process the trade update to register the fill
        self.logger().info(
            f"Processing trade update for {order_id}: fill_amount={fill_base_amount}, "
            f"fill_price={executed_price}, trade_id={transaction_hash}"
        )
        self._order_tracker.process_trade_update(trade_update)

        # Mark order as FILLED
        order_update = OrderUpdate(
            client_order_id=order_id,
            exchange_order_id=transaction_hash,
            trading_pair=trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=OrderState.FILLED,
        )
        self._order_tracker.process_order_update(order_update)
