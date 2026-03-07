from decimal import Decimal
from typing import Any, Dict, Optional

from eth_account import Account
from eth_account.messages import encode_typed_data

from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest

PRICE_MULTIPLIER = Decimal("1000000000")

CHAIN_IDS_BY_DOMAIN = {
    "grvt_perpetual": 325,
    "grvt_perpetual_testnet": 326,
}

TIME_IN_FORCE_TO_SIGN_VALUE = {
    "GOOD_TILL_TIME": 1,
    "ALL_OR_NONE": 2,
    "IMMEDIATE_OR_CANCEL": 3,
    "FILL_OR_KILL": 4,
}

EIP712_ORDER_MESSAGE_TYPE = {
    "Order": [
        {"name": "subAccountID", "type": "uint64"},
        {"name": "isMarket", "type": "bool"},
        {"name": "timeInForce", "type": "uint8"},
        {"name": "postOnly", "type": "bool"},
        {"name": "reduceOnly", "type": "bool"},
        {"name": "legs", "type": "OrderLeg[]"},
        {"name": "nonce", "type": "uint32"},
        {"name": "expiration", "type": "int64"},
    ],
    "OrderLeg": [
        {"name": "assetID", "type": "uint256"},
        {"name": "contractSize", "type": "uint64"},
        {"name": "limitPrice", "type": "uint64"},
        {"name": "isBuyingContract", "type": "bool"},
    ],
}


class GrvtPerpetualAuth(AuthBase):
    """
    GRVT auth wrapper.

    Order signing uses GRVT EIP-712 payload for /v1/create_order.
    """

    def __init__(
        self,
        session_cookie: str = "",
        account_id: str = "",
        api_key: str = "",
        sub_account_id: str = "",
        evm_private_key: str = "",
        stark_public_key: str = "",
        stark_signature: str = "",
        domain: str = "grvt_perpetual",
    ):
        self._session_cookie = session_cookie.strip()
        self._account_id = account_id.strip()
        self._api_key = api_key.strip()
        self._sub_account_id = sub_account_id.strip()
        self._evm_private_key = evm_private_key.strip()
        self._stark_public_key = stark_public_key.strip()
        self._stark_signature = stark_signature.strip()
        self._domain = domain

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def sub_account_id(self) -> str:
        return self._sub_account_id

    @property
    def has_session(self) -> bool:
        return bool(self._session_cookie and self._account_id)

    def set_session_context(self, session_cookie: str, account_id: str):
        self._session_cookie = (session_cookie or "").strip()
        self._account_id = (account_id or "").strip()

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        if not self.has_session:
            raise ValueError(
                "GRVT authenticated request requires session context. "
                "Use API key login first or provide `session_cookie` + `account_id`."
            )
        headers = dict(request.headers or {})
        headers.update(self.auth_headers())
        request.headers = headers
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        return request

    def auth_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self._account_id:
            headers["X-Grvt-Account-Id"] = self._account_id
        if self._session_cookie:
            headers["Cookie"] = self._session_cookie
        return headers

    def ws_headers(self) -> Dict[str, str]:
        return self.auth_headers()

    def _get_domain_data(self) -> Dict[str, Any]:
        return {
            "name": "GRVT Exchange",
            "version": "0",
            "chainId": CHAIN_IDS_BY_DOMAIN.get(self._domain, CHAIN_IDS_BY_DOMAIN["grvt_perpetual"]),
        }

    @staticmethod
    def _normalize_asset_id(instrument_data: Dict[str, Any]) -> int:
        raw_asset_id = instrument_data.get("instrument_hash")
        if raw_asset_id is None:
            raise ValueError(
                "Missing instrument hash in GRVT instrument metadata. "
                "Expected `instrument_hash` (hex)."
            )
        asset_id_str = str(raw_asset_id).strip()
        if asset_id_str.lower().startswith("0x"):
            return int(asset_id_str, 16)
        return int(asset_id_str)

    @staticmethod
    def _normalize_base_decimals(instrument_data: Dict[str, Any]) -> int:
        base_decimals = instrument_data.get("base_decimals")
        if base_decimals is None:
            raise ValueError(
                "Missing `base_decimals` in GRVT instrument metadata required for EIP-712 order signing."
            )
        return int(base_decimals)

    def _build_signable_order_message_data(
        self,
        order: Dict[str, Any],
        instrument_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        time_in_force = str(order.get("time_in_force", "GOOD_TILL_TIME")).upper()
        if time_in_force not in TIME_IN_FORCE_TO_SIGN_VALUE:
            raise ValueError(
                f"Unsupported GRVT time_in_force '{time_in_force}'. "
                f"Supported values: {list(TIME_IN_FORCE_TO_SIGN_VALUE.keys())}."
            )

        signature = order.get("signature") or {}
        if "nonce" not in signature or "expiration" not in signature:
            raise ValueError(
                "GRVT order signature envelope requires `nonce` and `expiration` before signing."
            )

        legs = order.get("legs") or []
        if len(legs) == 0:
            raise ValueError("GRVT order must contain at least one leg.")
        # Hummingbot currently uses single-leg perpetual orders.
        leg = legs[0]

        asset_id = self._normalize_asset_id(instrument_data)
        base_decimals = self._normalize_base_decimals(instrument_data)
        contract_size = int(Decimal(str(leg.get("size"))) * (Decimal(10) ** base_decimals))
        limit_price = int(Decimal(str(leg.get("limit_price", "0"))) * PRICE_MULTIPLIER)

        signable_legs = [{
            "assetID": asset_id,
            "contractSize": contract_size,
            "limitPrice": limit_price,
            "isBuyingContract": bool(leg.get("is_buying_asset")),
        }]

        return {
            "subAccountID": int(order["sub_account_id"]),
            "isMarket": bool(order.get("is_market", False)),
            "timeInForce": TIME_IN_FORCE_TO_SIGN_VALUE[time_in_force],
            "postOnly": bool(order.get("post_only", False)),
            "reduceOnly": bool(order.get("reduce_only", False)),
            "legs": signable_legs,
            "nonce": int(signature["nonce"]),
            "expiration": int(signature["expiration"]),
        }

    def sign_order_payload(
        self,
        order: Dict[str, Any],
        instrument_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not self._evm_private_key:
            raise ValueError(
                "GRVT order signing requires secret/private key. "
                "Set `grvt_perpetual_evm_private_key`."
            )

        message_data = self._build_signable_order_message_data(order=order, instrument_data=instrument_data)
        signable_message = encode_typed_data(self._get_domain_data(), EIP712_ORDER_MESSAGE_TYPE, message_data)
        signed = Account.sign_message(signable_message, self._evm_private_key)
        signer = Account.from_key(self._evm_private_key).address
        v_value = int(signed.v)
        if v_value in (0, 1):
            v_value += 27

        return {
            "signer": signer,
            "r": f"0x{signed.r:064x}",
            "s": f"0x{signed.s:064x}",
            "v": v_value,
            "expiration": str(message_data["expiration"]),
            "nonce": int(message_data["nonce"]),
        }

    def build_create_order_payload(
        self,
        order: Dict[str, Any],
        instrument_data: Dict[str, Any],
        provided_signature: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        metadata = order.get("metadata") or {}
        order_payload: Dict[str, Any] = {
            "sub_account_id": order.get("sub_account_id"),
            "is_market": order.get("is_market", False),
            "time_in_force": order.get("time_in_force", "GOOD_TILL_TIME"),
            "post_only": order.get("post_only", False),
            "reduce_only": order.get("reduce_only", False),
            "legs": list(order.get("legs") or []),
            "signature": dict(order.get("signature") or {}),
            "metadata": {
                "client_order_id": metadata.get("client_order_id"),
            },
        }
        if provided_signature is not None:
            signature = provided_signature
        else:
            signature = self.sign_order_payload(order=order_payload, instrument_data=instrument_data)
        order_payload["signature"] = signature

        return {
            "order": order_payload,
        }
