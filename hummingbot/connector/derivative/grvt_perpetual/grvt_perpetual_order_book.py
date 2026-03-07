from typing import Any, Dict, Optional

from hummingbot.connector.derivative.grvt_perpetual import grvt_perpetual_utils as utils
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType


class GrvtPerpetualOrderBook(OrderBook):
    @staticmethod
    def _normalize_price_levels(levels: Any) -> list:
        normalized_levels = []
        for level in levels or []:
            if not isinstance(level, dict):
                continue
            price = level.get("price")
            amount = level.get("size")
            if price is None or amount is None:
                continue
            normalized_levels.append([str(price), str(amount)])
        return normalized_levels

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _extract_ws_stream_data(msg: Dict[str, Any]) -> Dict[str, Any]:
        params = msg.get("params", {})
        if not isinstance(params, dict):
            return {}
        data = params.get("data")
        if not isinstance(data, dict):
            return {}
        return data

    @classmethod
    def _extract_ws_feed_data(cls, msg: Dict[str, Any]) -> Dict[str, Any]:
        stream_data = cls._extract_ws_stream_data(msg)
        feed = stream_data.get("feed")
        if not isinstance(feed, dict):
            return {}
        return feed

    @classmethod
    def snapshot_message_from_exchange(
        cls,
        msg: Dict[str, Any],
        timestamp: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OrderBookMessage:
        data = msg.get("result", msg)
        instrument = data.get("instrument")
        trading_pair = metadata.get("trading_pair") if metadata else utils.instrument_to_trading_pair(instrument)
        update_id = cls._safe_int(data.get("event_time"))
        event_ts = utils.convert_timestamp_to_seconds(data.get("event_time"))
        ts = event_ts or timestamp

        return OrderBookMessage(
            OrderBookMessageType.SNAPSHOT,
            {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "bids": cls._normalize_price_levels(data.get("bids", [])),
                "asks": cls._normalize_price_levels(data.get("asks", [])),
            },
            timestamp=ts,
        )

    @classmethod
    def snapshot_message_from_ws(
        cls,
        msg: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OrderBookMessage:
        stream_data = cls._extract_ws_stream_data(msg)
        data = cls._extract_ws_feed_data(msg)
        instrument = data.get("instrument")
        trading_pair = metadata.get("trading_pair") if metadata else utils.instrument_to_trading_pair(instrument)
        ts = utils.convert_timestamp_to_seconds(data.get("event_time"))
        update_id = cls._safe_int(stream_data.get("sequence_number"), cls._safe_int(data.get("event_time")))

        return OrderBookMessage(
            OrderBookMessageType.SNAPSHOT,
            {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "bids": cls._normalize_price_levels(data.get("bids", [])),
                "asks": cls._normalize_price_levels(data.get("asks", [])),
            },
            timestamp=ts,
        )

    @classmethod
    def diff_message_from_exchange(
        cls,
        msg: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OrderBookMessage:
        stream_data = cls._extract_ws_stream_data(msg)
        data = cls._extract_ws_feed_data(msg)
        instrument = data.get("instrument")
        trading_pair = metadata.get("trading_pair") if metadata else utils.instrument_to_trading_pair(instrument)
        ts = utils.convert_timestamp_to_seconds(data.get("event_time"))
        update_id = cls._safe_int(stream_data.get("sequence_number"), cls._safe_int(data.get("event_time")))
        first_update_id = cls._safe_int(stream_data.get("prev_sequence_number"), update_id)

        return OrderBookMessage(
            OrderBookMessageType.DIFF,
            {
                "trading_pair": trading_pair,
                "first_update_id": first_update_id,
                "update_id": update_id,
                "bids": cls._normalize_price_levels(data.get("bids", [])),
                "asks": cls._normalize_price_levels(data.get("asks", [])),
            },
            timestamp=ts,
        )

    @classmethod
    def trade_message_from_exchange(
        cls,
        msg: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OrderBookMessage:
        stream_data = cls._extract_ws_stream_data(msg)
        data = cls._extract_ws_feed_data(msg)
        instrument = data.get("instrument")
        trading_pair = metadata.get("trading_pair") if metadata else utils.instrument_to_trading_pair(instrument)

        ts = utils.convert_timestamp_to_seconds(data.get("event_time"))
        is_taker_buyer = bool(data.get("is_taker_buyer"))
        trade_id = data.get("trade_id")
        sequence_number = cls._safe_int(stream_data.get("sequence_number"))
        update_id = sequence_number or cls._safe_int(trade_id, cls._safe_int(data.get("event_time")))

        return OrderBookMessage(
            OrderBookMessageType.TRADE,
            {
                "trading_pair": trading_pair,
                "trade_type": float(TradeType.BUY.value) if is_taker_buyer else float(TradeType.SELL.value),
                "trade_id": trade_id or update_id,
                "update_id": update_id,
                "price": data.get("price"),
                "amount": data.get("size"),
            },
            timestamp=ts,
        )
