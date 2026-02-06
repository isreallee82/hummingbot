"""Evedex authentication module."""
from typing import Any, Dict, Optional

from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest


class EvedexAuth(AuthBase):
    """
    Auth class for Evedex exchange.
    Evedex uses simple API key authentication via X-API-Key header.
    """

    def __init__(self, api_key: str, time_provider: Optional[TimeSynchronizer] = None):
        self._api_key = api_key
        self._time_provider = time_provider

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Add authentication headers to REST request.
        """
        headers = request.headers or {}
        headers.update(self.header_for_authentication())
        request.headers = headers
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        """
        This method is intended to configure a websocket request to be authenticated.
        Evedex WS authentication is done via message payload after connection.
        """
        return request

    def header_for_authentication(self) -> Dict[str, Any]:
        """
        Returns headers for REST API authentication.
        """
        return {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json"
        }

    def generate_ws_auth_message(self) -> Dict[str, Any]:
        """
        Generate WebSocket authentication message.
        """
        return {
            "event": "auth",
            "payload": {
                "apiKey": self._api_key
            }
        }
