import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from hummingbot.connector.derivative.grvt_perpetual import (
    grvt_perpetual_constants as CONSTANTS,
    grvt_perpetual_utils as utils,
    grvt_perpetual_web_utils as web_utils,
)
from hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_order_book import GrvtPerpetualOrderBook
from hummingbot.core.data_type.funding_info import FundingInfo, FundingInfoUpdate
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.perpetual_api_order_book_data_source import PerpetualAPIOrderBookDataSource
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_derivative import GrvtPerpetualDerivative


class GrvtPerpetualAPIOrderBookDataSource(PerpetualAPIOrderBookDataSource):

    _logger: Optional[HummingbotLogger] = None

    def __init__(
        self,
        trading_pairs: List[str],
        connector: "GrvtPerpetualDerivative",
        api_factory: WebAssistantsFactory,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ):
        super().__init__(trading_pairs)
        self._connector = connector
        self._trade_messages_queue_key = CONSTANTS.WS_PUBLIC_TRADES_STREAM
        self._diff_messages_queue_key = CONSTANTS.WS_PUBLIC_ORDERBOOK_DIFF_STREAM
        self._snapshot_messages_queue_key = CONSTANTS.WS_PUBLIC_ORDERBOOK_SNAPSHOT_STREAM
        self._funding_info_messages_queue_key = CONSTANTS.WS_PUBLIC_TICKERS_STREAM
        self._domain = domain
        self._api_factory = api_factory

    async def get_last_traded_prices(
        self,
        trading_pairs: List[str],
        domain: Optional[str] = None,
    ) -> Dict[str, float]:
        return await self._connector.get_last_traded_prices(trading_pairs=trading_pairs)

    async def get_funding_info(self, trading_pair: str) -> FundingInfo:
        params = {"instrument": self._connector.exchange_symbol_associated_to_pair(trading_pair)}
        data = await self._connector._api_get(
            path_url=CONSTANTS.TICKER_PATH_URL,
            params=params,
            throttler_limit_id=CONSTANTS.TICKER_PATH_URL,
        )
        payload = utils.extract_result(data)

        return FundingInfo(
            trading_pair=trading_pair,
            index_price=utils.safe_decimal(payload.get("index_price")),
            mark_price=utils.safe_decimal(payload.get("mark_price")),
            next_funding_utc_timestamp=0.0,
            rate=utils.safe_decimal(payload.get("funding_rate_8h_curr")),
        )

    async def _request_order_book_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        instrument = self._connector.exchange_symbol_associated_to_pair(trading_pair)
        last_error: Optional[Exception] = None

        for depth in CONSTANTS.ORDER_BOOK_SNAPSHOT_DEPTHS:
            params = {"instrument": instrument, "depth": depth}
            try:
                return await self._connector._api_get(
                    path_url=CONSTANTS.SNAPSHOT_PATH_URL,
                    params=params,
                    limit_id=CONSTANTS.SNAPSHOT_PATH_URL,
                )
            except IOError as request_error:
                last_error = request_error
                error_text = str(request_error).lower()
                is_depth_error = "depth is invalid" in error_text or '"code":3031' in error_text
                if not is_depth_error:
                    raise
                self.logger().warning(
                    f"GRVT order book snapshot rejected depth={depth} for {trading_pair}. Trying next depth."
                )

        if last_error is not None:
            raise last_error
        raise IOError(f"Failed to fetch GRVT order book snapshot for {trading_pair}: no depth candidates available.")

    async def _connected_websocket_assistant(self) -> WSAssistant:
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        await ws.connect(
            ws_url=web_utils.public_wss_url(domain=self._domain),
            ping_timeout=CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL,
        )
        return ws

    async def _subscribe_channels(self, ws: WSAssistant):
        try:
            for trading_pair in self._trading_pairs:
                await self.subscribe_to_trading_pair(trading_pair)
            self.logger().info("Subscribed to GRVT perpetual public channels")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error("Unexpected error while subscribing to public channels", exc_info=True)
            raise

    async def subscribe_to_trading_pair(self, trading_pair: str) -> bool:
        if self._ws_assistant is None:
            return False

        instrument = self._connector.exchange_symbol_associated_to_pair(trading_pair)
        streams = [
            CONSTANTS.WS_PUBLIC_ORDERBOOK_SNAPSHOT_STREAM,
            CONSTANTS.WS_PUBLIC_ORDERBOOK_DIFF_STREAM,
            CONSTANTS.WS_PUBLIC_TRADES_STREAM,
            CONSTANTS.WS_PUBLIC_TICKERS_STREAM,
        ]

        for stream in streams:
            payload = {
                "id": str(uuid.uuid4()),
                "jsonrpc": "2.0",
                "method": "subscribe",
                "params": {
                    "streams": [stream],
                    "selector": {
                        stream: [instrument],
                    },
                },
            }
            await self._ws_assistant.send(WSJSONRequest(payload=payload))

        return True

    async def unsubscribe_from_trading_pair(self, trading_pair: str) -> bool:
        if self._ws_assistant is None:
            return False

        instrument = self._connector.exchange_symbol_associated_to_pair(trading_pair)
        streams = [
            CONSTANTS.WS_PUBLIC_ORDERBOOK_SNAPSHOT_STREAM,
            CONSTANTS.WS_PUBLIC_ORDERBOOK_DIFF_STREAM,
            CONSTANTS.WS_PUBLIC_TRADES_STREAM,
            CONSTANTS.WS_PUBLIC_TICKERS_STREAM,
        ]

        for stream in streams:
            payload = {
                "id": str(uuid.uuid4()),
                "jsonrpc": "2.0",
                "method": "unsubscribe",
                "params": {
                    "streams": [stream],
                    "selector": {
                        stream: [instrument],
                    },
                },
            }
            await self._ws_assistant.send(WSJSONRequest(payload=payload))

        return True

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        stream = event_message.get("params", {}).get("stream", "")

        if stream == CONSTANTS.WS_PUBLIC_ORDERBOOK_DIFF_STREAM:
            return self._diff_messages_queue_key
        if stream == CONSTANTS.WS_PUBLIC_ORDERBOOK_SNAPSHOT_STREAM:
            return self._snapshot_messages_queue_key
        if stream == CONSTANTS.WS_PUBLIC_TRADES_STREAM:
            return self._trade_messages_queue_key
        if stream in {CONSTANTS.WS_PUBLIC_TICKERS_STREAM, CONSTANTS.WS_PUBLIC_FUNDING_STREAM}:
            return self._funding_info_messages_queue_key
        return ""

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot = await self._request_order_book_snapshot(trading_pair)
        snapshot_timestamp = time.time()
        return GrvtPerpetualOrderBook.snapshot_message_from_exchange(
            snapshot,
            snapshot_timestamp,
            metadata={"trading_pair": trading_pair},
        )

    async def _parse_order_book_snapshot_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        message_queue.put_nowait(GrvtPerpetualOrderBook.snapshot_message_from_ws(raw_message))

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        message_queue.put_nowait(GrvtPerpetualOrderBook.diff_message_from_exchange(raw_message))

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        message_queue.put_nowait(GrvtPerpetualOrderBook.trade_message_from_exchange(raw_message))

    async def _parse_funding_info_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        stream_data: Dict[str, Any] = raw_message.get("params", {}).get("data", {})
        if not isinstance(stream_data, dict):
            return
        data: Dict[str, Any] = stream_data.get("feed", {})
        if not isinstance(data, dict):
            return
        instrument = data.get("instrument")
        if instrument is None:
            return

        trading_pair = self._connector.trading_pair_associated_to_exchange_symbol(instrument)
        funding_update = FundingInfoUpdate(
            trading_pair=trading_pair,
            index_price=utils.safe_decimal(data.get("index_price")),
            mark_price=utils.safe_decimal(data.get("mark_price")),
            next_funding_utc_timestamp=0,
            rate=utils.safe_decimal(data.get("funding_rate_8h_curr")),
        )
        message_queue.put_nowait(funding_update)
