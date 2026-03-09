from unittest import TestCase

from hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_order_book import GrvtPerpetualOrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessageType


class GrvtPerpetualOrderBookTests(TestCase):

    def test_snapshot_message_from_exchange_parses_object_price_levels(self):
        snapshot_message = GrvtPerpetualOrderBook.snapshot_message_from_exchange(
            msg={
                "result": {
                    "event_time": "1710000000000000000",
                    "instrument": "BTC_USDT_Perp",
                    "bids": [
                        {"price": "60000.0", "size": "1.25", "num_orders": 2},
                        {"price": "60000.5", "size": "0.5", "num_orders": 1},
                    ],
                    "asks": [
                        {"price": "60001.0", "size": "0.75", "num_orders": 2},
                        {"price": "60001.5", "size": "0.40", "num_orders": 1},
                    ],
                }
            },
            timestamp=1710000000.0,
            metadata={"trading_pair": "BTC-USDT"},
        )

        self.assertEqual(OrderBookMessageType.SNAPSHOT, snapshot_message.type)
        self.assertEqual("BTC-USDT", snapshot_message.trading_pair)
        self.assertEqual(1710000000000000000, snapshot_message.update_id)
        self.assertEqual(2, len(snapshot_message.bids))
        self.assertEqual(60000.0, snapshot_message.bids[0].price)
        self.assertEqual(1.25, snapshot_message.bids[0].amount)
        self.assertEqual(60000.5, snapshot_message.bids[1].price)
        self.assertEqual(0.5, snapshot_message.bids[1].amount)
        self.assertEqual(2, len(snapshot_message.asks))
        self.assertEqual(60001.0, snapshot_message.asks[0].price)
        self.assertEqual(0.75, snapshot_message.asks[0].amount)
        self.assertEqual(60001.5, snapshot_message.asks[1].price)
        self.assertEqual(0.4, snapshot_message.asks[1].amount)

    def test_diff_message_from_exchange_parses_feed_price_levels(self):
        diff_message = GrvtPerpetualOrderBook.diff_message_from_exchange(
            msg={
                "params": {
                    "data": {
                        "sequence_number": "123457",
                        "prev_sequence_number": "123456",
                        "feed": {
                            "event_time": "1710000000100000000",
                            "instrument": "BTC_USDT_Perp",
                            "bids": [{"price": "60000.0", "size": "0.80", "num_orders": 1}],
                            "asks": [{"price": "60000.5", "size": "0", "num_orders": 1}],
                        },
                    }
                }
            },
            metadata={"trading_pair": "BTC-USDT"},
        )

        self.assertEqual(OrderBookMessageType.DIFF, diff_message.type)
        self.assertEqual("BTC-USDT", diff_message.trading_pair)
        self.assertEqual(123457, diff_message.update_id)
        self.assertEqual(123456, diff_message.first_update_id)
        self.assertEqual(1, len(diff_message.bids))
        self.assertEqual(60000.0, diff_message.bids[0].price)
        self.assertEqual(0.8, diff_message.bids[0].amount)
        self.assertEqual(1, len(diff_message.asks))
        self.assertEqual(60000.5, diff_message.asks[0].price)
        self.assertEqual(0.0, diff_message.asks[0].amount)
