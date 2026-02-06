"""Evedex API order book data source module."""
import asyncio
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from hummingbot.connector.exchange.evedex.evedex_exchange import EvedexExchange

from hummingbot.connector.exchange.evedex import evedex_constants as CONSTANTS, evedex_web_utils as web_utils
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger


class EvedexAPIOrderBookDataSource(OrderBookTrackerDataSource):
    """Order book data source for Evedex exchange using Centrifugo protocol."""

    _logger: Optional[HummingbotLogger] = None
    _trading_pair_symbol_map: Dict[str, str] = {}
    _mapping_initialization_lock = asyncio.Lock()
    _message_id: int = 0

    def __init__(
            self,
            trading_pairs: List[str],
            connector: "EvedexExchange",
            api_factory: WebAssistantsFactory,
            domain: str = CONSTANTS.DEFAULT_DOMAIN):
        super().__init__(trading_pairs)
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._trading_pairs: List[str] = trading_pairs
        self._message_queue: Dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        # Ping task for keeping Centrifugo connection alive
        self._ping_task: Optional[asyncio.Task] = None
        self._ws_assistant: Optional[WSAssistant] = None

    def _next_message_id(self) -> int:
        """Generate the next message ID for Centrifugo protocol."""
        self._message_id += 1
        return self._message_id

    async def _ping_loop(self, websocket_assistant: WSAssistant):
        """
        Sends Centrifugo protocol ping messages to keep the connection alive.
        """
        try:
            while True:
                await asyncio.sleep(CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL)
                ping_payload = {}
                ping_request: WSJSONRequest = WSJSONRequest(payload=ping_payload)
                await websocket_assistant.send(ping_request)
                self.logger().debug("Sent Centrifugo ping (order book)")
        except asyncio.CancelledError:
            self.logger().debug("Order book ping loop cancelled")
            raise
        except Exception as e:
            self.logger().warning(f"Order book ping loop error: {e}")

    async def get_last_traded_prices(self,
                                     trading_pairs: List[str],
                                     domain: Optional[str] = None) -> Dict[str, float]:
        """Get last traded prices for trading pairs."""
        results = {}
        for trading_pair in trading_pairs:
            try:
                symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                response = await self._connector._api_get(
                    path_url=CONSTANTS.INSTRUMENTS_PATH_URL,
                    params={"instrument": symbol, "fields": "metrics"})

                if isinstance(response, list) and len(response) > 0:
                    results[trading_pair] = float(response[0].get("lastPrice", 0))
                else:
                    results[trading_pair] = float(response.get("lastPrice", 0))
            except Exception:
                pass
        return results

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        """Fetch order book snapshot."""
        snapshot_response = await self._request_order_book_snapshot(trading_pair)
        snapshot_timestamp: float = time.time()

        update_id = int(snapshot_timestamp * 1000)
        bids = []
        asks = []

        for bid in snapshot_response.get("bids", []):
            if isinstance(bid, dict):
                price = float(bid.get("price", 0))
                amount = float(bid.get("quantity", 0))
            else:
                price = float(bid[0])
                amount = float(bid[1])
            bids.append([price, amount])

        for ask in snapshot_response.get("asks", []):
            if isinstance(ask, dict):
                price = float(ask.get("price", 0))
                amount = float(ask.get("quantity", 0))
            else:
                price = float(ask[0])
                amount = float(ask[1])
            asks.append([price, amount])

        order_book_message_content = {
            "trading_pair": trading_pair,
            "update_id": update_id,
            "bids": bids,
            "asks": asks,
        }

        snapshot_msg = OrderBookMessage(
            OrderBookMessageType.SNAPSHOT,
            order_book_message_content,
            snapshot_timestamp)

        return snapshot_msg

    async def _request_order_book_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        """Request order book snapshot from REST API."""
        symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        path_url = CONSTANTS.ORDER_BOOK_PATH_URL.format(instrument=symbol)

        rest_assistant = await self._api_factory.get_rest_assistant()
        url = web_utils.public_rest_url(path_url, domain=self._domain)

        data = await rest_assistant.execute_request(
            url=url,
            method=RESTMethod.GET,
            throttler_limit_id=CONSTANTS.ORDER_BOOK_PATH_URL,
        )
        return data

    async def _connected_websocket_assistant(self) -> WSAssistant:
        """Connect to WebSocket using Centrifugo protocol."""
        # Cancel any existing ping task
        if self._ping_task is not None and not self._ping_task.done():
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            self._ping_task = None

        ws = await self._api_factory.get_ws_assistant()
        await ws.connect(
            ws_url=web_utils.wss_url(domain=self._domain),
            ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL + CONSTANTS.WS_PING_TIMEOUT)

        # Send Centrifugo connect message
        connect_payload = {
            "connect": {"name": "js"},
            "id": self._next_message_id()
        }
        connect_request: WSJSONRequest = WSJSONRequest(payload=connect_payload)
        await ws.send(connect_request)

        # Start the Centrifugo ping loop to keep connection alive
        self._ws_assistant = ws
        self._ping_task = asyncio.create_task(self._ping_loop(ws))

        return ws

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Subscribe to order book and trades channels using Centrifugo protocol.

        Channel patterns:
        - Order book: futures-perp:orderBook-{instrument}-0.1
        - Trades: futures-perp:recent-trade-{instrument}
        """
        try:
            # Subscribe to heartbeat channel (public, no auth required)
            heartbeat_payload = {
                "subscribe": {
                    "channel": "futures-perp:heartbeat",
                    "flag": 1
                },
                "id": self._next_message_id()
            }
            subscribe_heartbeat_request: WSJSONRequest = WSJSONRequest(payload=heartbeat_payload)
            await ws.send(subscribe_heartbeat_request)

            for trading_pair in self._trading_pairs:
                symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                # WebSocket channels use symbol without hyphen
                ws_symbol = symbol.replace("-", "")

                # Subscribe to order book updates: futures-perp:orderBook-{instrument}-0.1
                orderbook_channel = f"futures-perp:orderBook-{ws_symbol}-0.1"
                orderbook_payload = {
                    "subscribe": {
                        "channel": orderbook_channel,
                        "flag": 1
                    },
                    "id": self._next_message_id()
                }
                subscribe_orderbook_request: WSJSONRequest = WSJSONRequest(payload=orderbook_payload)
                await ws.send(subscribe_orderbook_request)

                # Subscribe to trades: futures-perp:recent-trade-{instrument}
                trade_channel = f"futures-perp:recent-trade-{ws_symbol}"
                trades_payload = {
                    "subscribe": {
                        "channel": trade_channel,
                        "flag": 1
                    },
                    "id": self._next_message_id()
                }
                subscribe_trades_request: WSJSONRequest = WSJSONRequest(payload=trades_payload)
                await ws.send(subscribe_trades_request)

            self.logger().info("Subscribed to public order book and trade channels...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception("Unexpected error occurred subscribing to order book data streams.")
            raise

    async def _on_order_stream_interruption(self, websocket_assistant: Optional[WSAssistant] = None):
        """Called when the order book stream gets interrupted."""
        # Cancel the ping task
        if self._ping_task is not None and not self._ping_task.done():
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            self._ping_task = None

        self._ws_assistant = None
        await super()._on_order_stream_interruption(websocket_assistant=websocket_assistant)

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        """Parse order book update message from Centrifugo push format."""
        # Handle Centrifugo push format
        if "push" in raw_message:
            push_data = raw_message.get("push", {})
            pub_data = push_data.get("pub", {})
            data = pub_data.get("data", {})
            instrument = data.get("instrument", "")

            try:
                trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(instrument)
            except KeyError:
                return

            orderbook = data.get("orderBook", {})
            timestamp = time.time()
            update_id = int(orderbook.get("t", timestamp * 1000))

            bids = []
            asks = []

            for bid in orderbook.get("bids", []):
                if isinstance(bid, dict):
                    price = float(bid.get("price", 0))
                    amount = float(bid.get("quantity", 0))
                else:
                    price = float(bid[0])
                    amount = float(bid[1])
                bids.append([price, amount])

            for ask in orderbook.get("asks", []):
                if isinstance(ask, dict):
                    price = float(ask.get("price", 0))
                    amount = float(ask.get("quantity", 0))
                else:
                    price = float(ask[0])
                    amount = float(ask[1])
                asks.append([price, amount])

            order_book_message_content = {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "bids": bids,
                "asks": asks,
            }

            diff_message = OrderBookMessage(
                OrderBookMessageType.DIFF,
                order_book_message_content,
                timestamp)

            message_queue.put_nowait(diff_message)

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        """Parse trade message from Centrifugo push format."""
        # Handle Centrifugo push format
        if "push" in raw_message:
            push_data = raw_message.get("push", {})
            pub_data = push_data.get("pub", {})
            data = pub_data.get("data", {})

            # Handle both single trade and list of trades
            trades = [data] if isinstance(data, dict) else data

            for trade in trades:
                # Trade structure: instrument, side, fillPrice, fillQuantity, executionId
                instrument = trade.get("instrument", "")

                try:
                    trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(instrument)
                except KeyError:
                    continue

                timestamp = time.time()
                trade_message_content = {
                    "trading_pair": trading_pair,
                    "trade_type": float(TradeType.BUY.value) if trade.get("side") == "BUY" else float(TradeType.SELL.value),
                    "trade_id": str(trade.get("executionId", trade.get("orderId", int(timestamp * 1000)))),
                    "update_id": int(timestamp * 1000),
                    "price": float(trade.get("fillPrice", 0)),
                    "amount": float(trade.get("fillQuantity", 0)),
                }

                trade_msg = OrderBookMessage(
                    OrderBookMessageType.TRADE,
                    trade_message_content,
                    timestamp)

                message_queue.put_nowait(trade_msg)

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        """Determine which channel the message originated from."""
        # Handle Centrifugo push format
        if "push" in event_message:
            push_data = event_message.get("push", {})
            channel = push_data.get("channel", "")
            if "orderBook" in channel:
                return self._diff_messages_queue_key
            elif "recent-trade" in channel:
                return self._trade_messages_queue_key
        return ""
