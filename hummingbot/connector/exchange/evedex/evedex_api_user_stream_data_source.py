"""Evedex API user stream data source module."""
import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from hummingbot.connector.exchange.evedex.evedex_exchange import EvedexExchange

from hummingbot.connector.exchange.evedex import evedex_constants as CONSTANTS, evedex_web_utils as web_utils
from hummingbot.connector.exchange.evedex.evedex_auth import EvedexAuth
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger


class EvedexAPIUserStreamDataSource(UserStreamTrackerDataSource):
    """
    User stream data source for Evedex exchange using Centrifugo protocol.

    Channel patterns:
    - Orders: futures-perp:order-{userExchangeId}
    - Account: futures-perp:user-{userExchangeId}
    - Order Fills: futures-perp:orderFilled-{userExchangeId}
    """

    HEARTBEAT_TIME_INTERVAL = 25.0  # Centrifugo ping interval
    PING_TIMEOUT = 10.0  # How long to wait for pong response

    _logger: Optional[HummingbotLogger] = None
    _message_id: int = 0

    def __init__(
            self,
            auth: EvedexAuth,
            trading_pairs: List[str],
            connector: "EvedexExchange",
            api_factory: WebAssistantsFactory,
            domain: str = CONSTANTS.DEFAULT_DOMAIN):
        super().__init__()
        self._auth = auth
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._trading_pairs = trading_pairs
        self._user_exchange_id: Optional[str] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._ws_assistant: Optional[WSAssistant] = None
        self._access_token: Optional[str] = None

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
                await asyncio.sleep(self.HEARTBEAT_TIME_INTERVAL)
                ping_payload = {"ping": {}}
                ping_request: WSJSONRequest = WSJSONRequest(payload=ping_payload)
                await websocket_assistant.send(ping_request)
                self.logger().debug("Sent Centrifugo ping (user stream)")
        except asyncio.CancelledError:
            self.logger().debug("User stream ping loop cancelled")
            raise
        except Exception as e:
            self.logger().warning(f"User stream ping loop error: {e}")

    async def _get_access_token(self) -> str:
        """Get access token for WebSocket authentication."""
        if self._access_token is None:
            token_data = await self._connector._api_get(
                path_url=CONSTANTS.DX_FEED_AUTH_PATH_URL,
                is_auth_required=True
            )
            self._access_token = token_data.get("token", "")
        return self._access_token

    async def _get_user_exchange_id(self) -> str:
        """
        Get the userExchangeId required for Centrifuge channel subscriptions.
        """
        if self._user_exchange_id is None:
            user_info = await self._connector._api_get(
                path_url=CONSTANTS.USER_ME_PATH_URL,
                is_auth_required=True
            )
            self._user_exchange_id = str(user_info.get("exchangeId", ""))
        return self._user_exchange_id

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
            ping_timeout=self.HEARTBEAT_TIME_INTERVAL + self.PING_TIMEOUT)

        # Send Centrifugo connect message (no token - auth is per-subscription)
        connect_payload = {
            "connect": {"name": "js"},
            "id": self._next_message_id()
        }
        connect_request: WSJSONRequest = WSJSONRequest(payload=connect_payload)
        await ws.send(connect_request)

        # Centrifugo server sends pings; respond with pong in message handler.
        self._ws_assistant = ws

        self.logger().info("Successfully connected to user stream")
        return ws

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        """
        Subscribe to user-specific channels using Centrifugo protocol.
        """
        try:
            # Get userExchangeId and access token for channel subscriptions
            user_exchange_id = await self._get_user_exchange_id()
            access_token = await self._get_access_token()

            # Subscribe to orders channel: futures-perp:order-{userExchangeId}
            orders_payload = {
                "subscribe": {
                    "channel": f"futures-perp:order-{user_exchange_id}",
                    "data": {"accessToken": access_token},
                    "recoverable": True,
                    "flag": 1,
                    "recover": True
                },
                "id": self._next_message_id()
            }
            subscribe_orders_request: WSJSONRequest = WSJSONRequest(payload=orders_payload)
            await websocket_assistant.send(subscribe_orders_request)

            # Subscribe to account channel: futures-perp:user-{userExchangeId}
            account_payload = {
                "subscribe": {
                    "channel": f"futures-perp:user-{user_exchange_id}",
                    "data": {"accessToken": access_token},
                    "recoverable": True,
                    "flag": 1,
                    "recover": True
                },
                "id": self._next_message_id()
            }
            subscribe_account_request: WSJSONRequest = WSJSONRequest(payload=account_payload)
            await websocket_assistant.send(subscribe_account_request)

            # Subscribe to order fills channel: futures-perp:orderFilled-{userExchangeId}
            order_fills_payload = {
                "subscribe": {
                    "channel": f"futures-perp:orderFilled-{user_exchange_id}",
                    "data": {"accessToken": access_token},
                    "recoverable": True,
                    "flag": 1,
                    "recover": True
                },
                "id": self._next_message_id()
            }
            subscribe_order_fills_request: WSJSONRequest = WSJSONRequest(payload=order_fills_payload)
            await websocket_assistant.send(subscribe_order_fills_request)

            self.logger().info(f"Subscribed to private user stream channels for user {user_exchange_id}...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception(
                "Unexpected error occurred subscribing to user streams...")
            raise

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant, queue: asyncio.Queue):
        async for ws_response in websocket_assistant.iter_messages():
            data = ws_response.data
            # Centrifugo sends ping commands and expects pong replies.
            if data == {}:
                await websocket_assistant.send(WSJSONRequest(payload={}))
                continue
            if isinstance(data, dict) and "ping" in data:
                self.logger().debug("Received Centrifugo ping on user stream; sending pong.")
                await websocket_assistant.send(WSJSONRequest(payload={"pong": {}}))
                continue
            await self._process_event_message(event_message=data, queue=queue)

    async def _on_user_stream_interruption(self, websocket_assistant: Optional[WSAssistant]):
        """Called when the user stream gets interrupted."""
        # Cancel the ping task
        if self._ping_task is not None and not self._ping_task.done():
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            self._ping_task = None

        self._ws_assistant = None
        self._access_token = None  # Reset token on reconnect
        await super()._on_user_stream_interruption(websocket_assistant=websocket_assistant)

    async def _process_event_message(self, event_message: Dict[str, Any], queue: asyncio.Queue):
        """Process event message from websocket."""
        # Handle empty pong responses from Centrifugo ping
        if not event_message or event_message == {}:
            return

        # Handle Centrifugo push format
        if "push" in event_message:
            queue.put_nowait(event_message)
