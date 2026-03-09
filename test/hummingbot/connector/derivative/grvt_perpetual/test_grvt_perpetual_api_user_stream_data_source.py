import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_api_user_stream_data_source import (
    GrvtPerpetualAPIUserStreamDataSource,
)
from hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_auth import GrvtPerpetualAuth


class GrvtPerpetualAPIUserStreamDataSourceTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        auth = GrvtPerpetualAuth(session_cookie="session=abc", account_id="123")
        self.api_factory = MagicMock()
        self.data_source = GrvtPerpetualAPIUserStreamDataSource(
            auth=auth,
            trading_pairs=["BTC-USDT"],
            connector=MagicMock(),
            api_factory=self.api_factory,
        )

    async def test_subscribe_channels(self):
        ws = AsyncMock()

        await self.data_source._subscribe_channels(ws)

        self.assertEqual(1, ws.send.call_count)

    async def test_process_event_message_filters_non_subscription(self):
        queue = asyncio.Queue()

        await self.data_source._process_event_message({"foo": "bar"}, queue)
        self.assertTrue(queue.empty())

        await self.data_source._process_event_message(
            {
                "method": "subscription",
                "params": {"stream": "v1.orders", "data": {"order_id": "1"}},
            },
            queue,
        )
        self.assertFalse(queue.empty())
