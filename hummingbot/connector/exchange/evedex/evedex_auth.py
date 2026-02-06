"""Evedex authentication module."""
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Optional

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

from hummingbot.connector.exchange.evedex import evedex_constants as CONSTANTS
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest

# EvedEx Auth URLs
EVEDEX_AUTH_BASE_URL = "https://auth.evedex.com"
EVEDEX_NONCE_URL = f"{EVEDEX_AUTH_BASE_URL}/auth/nonce"
EVEDEX_SIGNUP_URL = f"{EVEDEX_AUTH_BASE_URL}/auth/user/sign-up"
EVEDEX_REFRESH_URL = f"{EVEDEX_AUTH_BASE_URL}/auth/refresh"

# EIP-712 Type Schemas
EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "salt", "type": "bytes32"},
    ],
    "New limit order": [
        {"name": "id", "type": "string"},
        {"name": "instrument", "type": "string"},
        {"name": "side", "type": "string"},
        {"name": "leverage", "type": "uint8"},
        {"name": "quantity", "type": "uint96"},
        {"name": "limitPrice", "type": "uint80"},
        {"name": "chainId", "type": "uint256"},
    ],
    "New market order": [
        {"name": "id", "type": "string"},
        {"name": "instrument", "type": "string"},
        {"name": "side", "type": "string"},
        {"name": "timeInForce", "type": "string"},
        {"name": "leverage", "type": "uint8"},
        {"name": "cashQuantity", "type": "uint96"},
        {"name": "chainId", "type": "uint256"},
    ],
    "New stop-limit order": [
        {"name": "id", "type": "string"},
        {"name": "instrument", "type": "string"},
        {"name": "side", "type": "string"},
        {"name": "leverage", "type": "uint8"},
        {"name": "quantity", "type": "uint96"},
        {"name": "limitPrice", "type": "uint80"},
        {"name": "stopPrice", "type": "uint80"},
        {"name": "chainId", "type": "uint256"},
    ],
    "Position close order": [
        {"name": "id", "type": "string"},
        {"name": "instrument", "type": "string"},
        {"name": "leverage", "type": "uint8"},
        {"name": "quantity", "type": "uint96"},
        {"name": "chainId", "type": "uint256"},
    ],
    "New take-profit/stop-loss": [
        {"name": "instrument", "type": "string"},
        {"name": "type", "type": "string"},
        {"name": "side", "type": "string"},
        {"name": "quantity", "type": "uint96"},
        {"name": "price", "type": "uint80"},
    ],
    "Replace limit order": [
        {"name": "orderId", "type": "string"},
        {"name": "quantity", "type": "uint96"},
        {"name": "limitPrice", "type": "uint80"},
    ],
    "Replace stop-limit order": [
        {"name": "orderId", "type": "string"},
        {"name": "quantity", "type": "uint96"},
        {"name": "limitPrice", "type": "uint80"},
        {"name": "stopPrice", "type": "uint80"},
    ],
    "Withdraw": [
        {"name": "recipient", "type": "address"},
        {"name": "amount", "type": "uint256"},
    ],
    "Oauth consent": [
        {"name": "oauthRequestId", "type": "string"},
    ],
}


def to_eth_number(value: Decimal) -> int:
    """
    Converts a decimal value to an integer using MATCHER_PRECISION.
    Formula: Round(floatValue * 10 ^ 8, HalfUp)
    """
    multiplier = Decimal(10 ** CONSTANTS.MATCHER_PRECISION)
    return int((value * multiplier).quantize(Decimal("1"), rounding="ROUND_HALF_UP"))


def create_siwe_message(
    address: str,
    uri: str,
    version: str,
    chain_id: int,
    nonce: str,
    issued_at: str,
) -> str:
    """
    Create a SIWE (Sign-In with Ethereum) message following EIP-4361.

    :param address: Ethereum wallet address
    :param uri: The URI of the service (e.g., "https://exchange.evedex.com")
    :param version: SIWE version (typically "1")
    :param chain_id: Blockchain chain ID
    :param nonce: Random nonce from the server
    :param issued_at: ISO 8601 timestamp
    :return: Formatted SIWE message string
    """
    return f"""{uri} wants you to sign in with your Ethereum account: {address}


URI: {uri}
Version: {version}
Chain ID: {chain_id}
Nonce: {nonce}
Issued At: {issued_at}"""


class EvedexAuth(AuthBase):
    """
    Auth class required by Evedex API.

    Supports two authentication methods:
    1. API Key: Simple header-based authentication using X-API-Key
    2. SIWE + JWT: Sign-In with Ethereum (EIP-4361) for JWT token authentication

    For order signing, uses EIP-712 typed data signatures.

    The private_key parameter is your Ethereum wallet's private key - the same one
    used when calling ethers.getSigner().signMessage() in JavaScript.
    """

    def __init__(self, api_key: str, time_provider: Optional[TimeSynchronizer] = None, private_key: str = ""):
        """
        Initialize EvedEx authentication.

        :param api_key: API key for header authentication (or wallet address for SIWE)
        :param time_provider: Time synchronizer for timestamp generation
        :param private_key: Ethereum wallet private key (hex string, with or without 0x prefix)
                           Required for SIWE authentication and EIP-712 order signing
        """
        self._api_key: str = api_key
        self._private_key: str = private_key
        self._time_provider: Optional[TimeSynchronizer] = time_provider
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._access_token_expiry: float = 0
        self._token_fetcher: Optional[Callable[[], Any]] = None
        self._nonce_fetcher: Optional[Callable[[], Any]] = None
        self._siwe_auth_fetcher: Optional[Callable[[Dict], Any]] = None
        self._wallet: Optional[Account] = None

        # Initialize wallet if private key is provided
        if private_key:
            if not private_key.startswith("0x"):
                private_key = "0x" + private_key
            self._wallet = Account.from_key(private_key)

    def set_token_fetcher(self, fetcher: Callable[[], Any]):
        """
        Set the token fetcher function that will be called to get access tokens.
        This is typically set by the connector to use its API methods.
        """
        self._token_fetcher = fetcher

    def set_nonce_fetcher(self, fetcher: Callable[[], Any]):
        """
        Set the nonce fetcher function for SIWE authentication.
        """
        self._nonce_fetcher = fetcher

    def set_siwe_auth_fetcher(self, fetcher: Callable[[Dict], Any]):
        """
        Set the SIWE authentication fetcher function.
        """
        self._siwe_auth_fetcher = fetcher

    def create_siwe_signature(self, nonce: str, chain_id: int = 1) -> Dict[str, str]:
        """
        Create a SIWE message and sign it with the wallet.

        :param nonce: Nonce from the server
        :param chain_id: Blockchain chain ID (default: 1 for Ethereum mainnet)
        :return: Dictionary with wallet, message, nonce, and signature
        """
        if not self._wallet:
            raise ValueError("Private key not configured for SIWE authentication")

        address = self._wallet.address
        uri = CONSTANTS.REST_URL
        version = "1"
        issued_at = datetime.now(timezone.utc).isoformat()

        siwe_message = create_siwe_message(
            address=address,
            uri=uri,
            version=version,
            chain_id=chain_id,
            nonce=nonce,
            issued_at=issued_at,
        )

        # Sign the message using EIP-191 personal sign
        message_hash = encode_defunct(text=siwe_message)
        signed_message = self._wallet.sign_message(message_hash)
        signature = signed_message.signature.hex()

        if not signature.startswith("0x"):
            signature = "0x" + signature

        return {
            "wallet": address,
            "message": siwe_message,
            "nonce": nonce,
            "signature": signature,
        }

    async def authenticate_with_siwe(self) -> Dict[str, Any]:
        """
        Perform SIWE authentication flow:
        1. Get nonce from server
        2. Create and sign SIWE message
        3. Submit to sign-up endpoint
        4. Return JWT tokens

        :return: Dictionary containing 'token' (access token) and 'refreshToken'
        """
        if not self._nonce_fetcher or not self._siwe_auth_fetcher:
            raise ValueError("SIWE fetchers not configured")

        nonce_data = await self._nonce_fetcher()
        nonce = nonce_data.get("nonce", "")

        siwe_data = self.create_siwe_signature(nonce)

        auth_response = await self._siwe_auth_fetcher(siwe_data)

        self._access_token = auth_response.get("token", "")
        self._refresh_token = auth_response.get("refreshToken", "")
        self._access_token_expiry = time.time() + 900

        return auth_response

    async def get_access_token(self) -> str:
        """
        Get or refresh the access token for WebSocket authentication.
        The access token is a JWT obtained from /api/dx-feed/auth endpoint.
        Token expires typically in 15 minutes (900 seconds).
        """
        current_time = time.time()

        if self._access_token is None or current_time >= (self._access_token_expiry - 60):
            if self._token_fetcher is not None:
                token_data = await self._token_fetcher()
                self._access_token = token_data.get("token", "")
                self._access_token_expiry = token_data.get("expireAt", current_time + 900)
            else:
                self._access_token = ""

        return self._access_token or ""

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Adds the API key to the request header for authenticated interactions.
        :param request: the request to be configured for authenticated interaction
        """
        headers: Dict[str, str] = {}
        if request.headers is not None:
            headers.update(request.headers)
        headers.update(self.header_for_authentication())
        request.headers = headers
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        """
        This method is intended to configure a websocket request to be authenticated.
        """
        return request

    def header_for_authentication(self) -> Dict[str, str]:
        return {"X-API-Key": self._api_key}

    @property
    def wallet_address(self) -> Optional[str]:
        """Returns the wallet address if a private key was provided."""
        if self._wallet:
            return self._wallet.address
        return None

    def _get_domain_data(self, chain_id: int) -> Dict[str, Any]:
        """
        Get the EIP-712 domain data for EvedEx.

        :param chain_id: The blockchain chain ID
        :return: Domain data dictionary
        """
        return {
            "name": CONSTANTS.EVEDEX_DOMAIN_NAME,
            "version": CONSTANTS.EVEDEX_DOMAIN_VERSION,
            "chainId": int(chain_id),
            "salt": CONSTANTS.EVEDEX_DOMAIN_SALT,
        }

    def sign_limit_order(
        self,
        order_id: str,
        instrument: str,
        side: str,
        leverage: int,
        quantity: Decimal,
        limit_price: Decimal,
        chain_id: int = CONSTANTS.CHAIN_ID,
    ) -> str:
        """
        Sign a limit order using EIP-712.

        :param order_id: Unique order ID
        :param instrument: Trading instrument (e.g., "XRPUSD")
        :param side: Order side ("BUY" or "SELL")
        :param leverage: Leverage multiplier
        :param quantity: Order quantity
        :param limit_price: Limit price
        :param chain_id: Blockchain chain ID (default: 161803)
        :return: Hex signature string
        """
        if not self._wallet:
            raise ValueError("Private key not configured for signing")

        message = {
            "id": order_id,
            "instrument": instrument,
            "side": side,
            "leverage": leverage,
            "quantity": to_eth_number(quantity),
            "limitPrice": to_eth_number(limit_price),
            "chainId": int(chain_id),
        }

        typed_data = {
            "types": {
                "EIP712Domain": EIP712_TYPES["EIP712Domain"],
                "New limit order": EIP712_TYPES["New limit order"],
            },
            "primaryType": "New limit order",
            "domain": self._get_domain_data(chain_id),
            "message": message,
        }

        signable_message = encode_typed_data(full_message=typed_data)
        signed = self._wallet.sign_message(signable_message)
        signature = signed.signature.hex()
        return signature if signature.startswith("0x") else f"0x{signature}"

    def sign_market_order(
        self,
        order_id: str,
        instrument: str,
        side: str,
        time_in_force: str,
        leverage: int,
        cash_quantity: Decimal,
        chain_id: int = CONSTANTS.CHAIN_ID,
    ) -> str:
        """
        Sign a market order using EIP-712.

        :param order_id: Unique order ID
        :param instrument: Trading instrument (e.g., "XRPUSD")
        :param side: Order side ("BUY" or "SELL")
        :param time_in_force: Time in force (e.g., "IOC")
        :param leverage: Leverage multiplier
        :param cash_quantity: Cash quantity for the order
        :param chain_id: Blockchain chain ID (default: 161803)
        :return: Hex signature string
        """
        if not self._wallet:
            raise ValueError("Private key not configured for signing")

        message = {
            "id": order_id,
            "instrument": instrument,
            "side": side,
            "timeInForce": time_in_force,
            "leverage": leverage,
            "cashQuantity": to_eth_number(cash_quantity),
            "chainId": int(chain_id),
        }

        typed_data = {
            "types": {
                "EIP712Domain": EIP712_TYPES["EIP712Domain"],
                "New market order": EIP712_TYPES["New market order"],
            },
            "primaryType": "New market order",
            "domain": self._get_domain_data(chain_id),
            "message": message,
        }

        signable_message = encode_typed_data(full_message=typed_data)
        signed = self._wallet.sign_message(signable_message)
        signature = signed.signature.hex()
        return signature if signature.startswith("0x") else f"0x{signature}"

    def sign_position_close(
        self,
        order_id: str,
        instrument: str,
        leverage: int,
        quantity: Decimal,
        chain_id: int = CONSTANTS.CHAIN_ID,
    ) -> str:
        """
        Sign a position close order using EIP-712.

        :param order_id: Unique order ID
        :param instrument: Trading instrument (e.g., "XRPUSD")
        :param leverage: Leverage multiplier
        :param quantity: Quantity to close
        :param chain_id: Blockchain chain ID (default: 161803)
        :return: Hex signature string
        """
        if not self._wallet:
            raise ValueError("Private key not configured for signing")

        message = {
            "id": order_id,
            "instrument": instrument,
            "leverage": leverage,
            "quantity": to_eth_number(quantity),
            "chainId": int(chain_id),
        }

        typed_data = {
            "types": {
                "EIP712Domain": EIP712_TYPES["EIP712Domain"],
                "Position close order": EIP712_TYPES["Position close order"],
            },
            "primaryType": "Position close order",
            "domain": self._get_domain_data(chain_id),
            "message": message,
        }

        signable_message = encode_typed_data(full_message=typed_data)
        signed = self._wallet.sign_message(signable_message)
        signature = signed.signature.hex()
        return signature if signature.startswith("0x") else f"0x{signature}"

    def sign_stop_limit_order(
        self,
        order_id: str,
        instrument: str,
        side: str,
        leverage: int,
        quantity: Decimal,
        limit_price: Decimal,
        stop_price: Decimal,
        chain_id: int = CONSTANTS.CHAIN_ID,
    ) -> str:
        """
        Sign a stop-limit order using EIP-712.

        :param order_id: Unique order ID
        :param instrument: Trading instrument (e.g., "XRPUSD")
        :param side: Order side ("BUY" or "SELL")
        :param leverage: Leverage multiplier
        :param quantity: Order quantity
        :param limit_price: Limit price
        :param stop_price: Stop trigger price
        :param chain_id: Blockchain chain ID (default: 161803)
        :return: Hex signature string
        """
        if not self._wallet:
            raise ValueError("Private key not configured for signing")

        message = {
            "id": order_id,
            "instrument": instrument,
            "side": side,
            "leverage": leverage,
            "quantity": to_eth_number(quantity),
            "limitPrice": to_eth_number(limit_price),
            "stopPrice": to_eth_number(stop_price),
            "chainId": int(chain_id),
        }

        typed_data = {
            "types": {
                "EIP712Domain": EIP712_TYPES["EIP712Domain"],
                "New stop-limit order": EIP712_TYPES["New stop-limit order"],
            },
            "primaryType": "New stop-limit order",
            "domain": self._get_domain_data(chain_id),
            "message": message,
        }

        signable_message = encode_typed_data(full_message=typed_data)
        signed = self._wallet.sign_message(signable_message)
        signature = signed.signature.hex()
        return signature if signature.startswith("0x") else f"0x{signature}"

    def sign_replace_limit_order(
        self,
        order_id: str,
        quantity: Decimal,
        limit_price: Decimal,
        chain_id: int = CONSTANTS.CHAIN_ID,
    ) -> str:
        """
        Sign a replace limit order request using EIP-712.

        :param order_id: Order ID to replace
        :param quantity: New quantity
        :param limit_price: New limit price
        :param chain_id: Blockchain chain ID (default: 161803)
        :return: Hex signature string
        """
        if not self._wallet:
            raise ValueError("Private key not configured for signing")

        message = {
            "orderId": order_id,
            "quantity": to_eth_number(quantity),
            "limitPrice": to_eth_number(limit_price),
        }

        typed_data = {
            "types": {
                "EIP712Domain": EIP712_TYPES["EIP712Domain"],
                "Replace limit order": EIP712_TYPES["Replace limit order"],
            },
            "primaryType": "Replace limit order",
            "domain": self._get_domain_data(chain_id),
            "message": message,
        }

        signable_message = encode_typed_data(full_message=typed_data)
        signed = self._wallet.sign_message(signable_message)
        signature = signed.signature.hex()
        return signature if signature.startswith("0x") else f"0x{signature}"
