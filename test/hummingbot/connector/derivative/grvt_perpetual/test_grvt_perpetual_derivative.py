import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

from bidict import bidict

from hummingbot.connector.derivative.grvt_perpetual.grvt_perpetual_derivative import GrvtPerpetualDerivative
from hummingbot.connector.derivative.position import Position
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState


class GrvtPerpetualDerivativeTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.fixtures_dir = Path(__file__).resolve().parents[5] / "grvt" / "fixtures"
        self.exchange = GrvtPerpetualDerivative(
            grvt_perpetual_sub_account_id="527106154284817",
            grvt_perpetual_evm_private_key="f7934647276a6e1fa0af3f4467b4b8ddaf45d25a7368fa1a295eef49a446819d",  # noqa: mock
            trading_pairs=["BTC-USDT"],
            trading_required=False,
        )
        self.exchange._set_trading_pair_symbol_map(bidict({"BTC_USDT_Perp": "BTC-USDT"}))
        self.exchange._instrument_info_map["BTC_USDT_Perp"] = {
            "instrument_hash": "0x030501",
            "base_decimals": 9,
        }

    def _load_fixture(self, filename: str):
        with open(self.fixtures_dir / filename, "r") as f:
            return json.load(f)

    async def test_format_trading_rules(self):
        instruments = self._load_fixture("instruments.json")
        rules = await self.exchange._format_trading_rules(instruments)

        self.assertGreaterEqual(len(rules), 1)
        self.assertEqual("BTC-USDT", rules[0].trading_pair)
        self.assertEqual(Decimal("0.1"), rules[0].min_price_increment)

    async def test_format_trading_rules_with_zero_increments_skips_rule(self):
        exchange_info = {
            "result": [{
                "instrument": "BTC_USDT_Perp",
                "kind": "PERPETUAL",
                "base": "BTC",
                "quote": "USDT",
                "status": "active",
                "base_decimals": 9,
                "quote_decimals": 9,
                "min_size": "0",
                "tick_size": "0",
            }]
        }

        rules = await self.exchange._format_trading_rules(exchange_info)
        self.assertEqual([], rules)

    async def test_format_trading_rules_skips_non_canonical_fields(self):
        exchange_info = {
            "result": [{
                "instrument": "xrp-usdt",
                "kind": "PERPETUAL",
                "base_currency": "XRP",
                "quote_currency": "USDT",
                "status": "active",
                "price_tick_size": "0.0001",
                "qty_step_size": "10",
                "min_qty": "10",
            }]
        }

        rules = await self.exchange._format_trading_rules(exchange_info)
        self.assertEqual([], rules)

    async def test_place_order_builds_payload(self):
        response = self._load_fixture("create_order_response.json")
        self.exchange._api_post = AsyncMock(return_value=response)
        expected_exchange_client_order_id = self.exchange._exchange_client_order_id("HBOT-1")

        order_id, timestamp = await self.exchange._place_order(
            order_id="HBOT-1",
            trading_pair="BTC-USDT",
            amount=Decimal("0.01"),
            trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal("60000"),
            signature_nonce=123456,
            signature_expiration_ns=1730800479321350000,
        )

        self.assertEqual("987654", order_id)
        self.assertEqual(1710000000.0, timestamp)

        sent_payload = self.exchange._api_post.call_args.kwargs["data"]
        self.assertEqual("BTC_USDT_Perp", sent_payload["order"]["legs"][0]["instrument"])
        self.assertNotIn("client_order_id", sent_payload["order"])
        self.assertEqual(expected_exchange_client_order_id, sent_payload["order"]["metadata"]["client_order_id"])
        self.assertIn("r", sent_payload["order"]["signature"])
        self.assertIn("s", sent_payload["order"]["signature"])
        self.assertIn("v", sent_payload["order"]["signature"])
        self.assertNotIn("chain_id", sent_payload["order"]["signature"])

    async def test_place_order_raises_actionable_error_on_client_order_id_error(self):
        self.exchange._api_post = AsyncMock(
            side_effect=[
                IOError(
                    "Error executing request POST https://trades.grvt.io/full/v1/create_order. "
                    'HTTP status is 400. Error: {"code":2011,"message":"Client Order ID should be supplied when '
                    'creating an order","status":400}'
                ),
            ]
        )

        with self.assertRaises(IOError) as context:
            await self.exchange._place_order(
                order_id="HBOT-RETRY-1",
                trading_pair="BTC-USDT",
                amount=Decimal("0.01"),
                trade_type=TradeType.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal("60000"),
                signature_nonce=123456,
                signature_expiration_ns=1730800479321350000,
            )

        self.assertEqual(1, self.exchange._api_post.call_count)
        self.assertIn("code 2011", str(context.exception))

    async def test_place_order_raises_actionable_error_on_authz_error(self):
        self.exchange._api_post = AsyncMock(
            side_effect=[
                IOError(
                    "Error executing request POST https://trades.grvt.io/full/v1/create_order. "
                    'HTTP status is 403. Error: {"code":1001,"message":"You are not authorized to access this '
                    'functionality","status":403}'
                ),
            ]
        )

        with self.assertRaises(IOError) as context:
            await self.exchange._place_order(
                order_id="HBOT-RETRY-2",
                trading_pair="BTC-USDT",
                amount=Decimal("0.01"),
                trade_type=TradeType.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal("60000"),
                signature_nonce=123456,
                signature_expiration_ns=1730800479321350000,
            )

        self.assertEqual(1, self.exchange._api_post.call_count)
        first_call = self.exchange._api_post.call_args_list[0].kwargs
        self.assertEqual("/v1/create_order", first_call["path_url"])
        self.assertEqual("527106154284817", first_call["data"]["order"]["sub_account_id"])
        self.assertIn("authorization failed", str(context.exception).lower())

    async def test_request_order_status_mapping(self):
        self.exchange._api_post = AsyncMock(return_value=self._load_fixture("order_status.json"))
        tracked_order = InFlightOrder(
            client_order_id="HBOT-1",
            exchange_order_id="987654",
            trading_pair="BTC-USDT",
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("0.01"),
            price=Decimal("60000"),
            creation_timestamp=0,
        )

        update = await self.exchange._request_order_status(tracked_order)

        self.assertEqual(OrderState.PARTIALLY_FILLED, update.new_state)
        self.assertEqual("987654", update.exchange_order_id)
        sent_payload = self.exchange._api_post.call_args.kwargs["data"]
        self.assertEqual("527106154284817", sent_payload["sub_account_id"])
        self.assertEqual(
            self.exchange._exchange_client_order_id("HBOT-1"),
            sent_payload["client_order_id"],
        )

    async def test_update_balances(self):
        self.exchange._api_post = AsyncMock(return_value=self._load_fixture("account_summary.json"))

        await self.exchange._update_balances()

        self.assertEqual(Decimal("9000"), self.exchange.available_balances["USDT"])
        self.assertEqual(Decimal("10000"), self.exchange.get_balance("USDT"))
        self.assertEqual("527106154284817", self.exchange._api_post.call_args.kwargs["data"]["sub_account_id"])

    async def test_update_positions(self):
        self.exchange._api_post = AsyncMock(return_value=self._load_fixture("positions.json"))

        await self.exchange._update_positions()

        self.assertEqual(1, len(self.exchange.account_positions))
        position = list(self.exchange.account_positions.values())[0]
        self.assertEqual(Decimal("0.01"), position.amount)
        self.assertEqual("527106154284817", self.exchange._api_post.call_args.kwargs["data"]["sub_account_id"])

    def test_in_flight_asset_balances_locks_quote_collateral_for_open_buy_and_sell(self):
        buy_order = InFlightOrder(
            client_order_id="buy-1",
            exchange_order_id="1",
            trading_pair="BTC-USDT",
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("1"),
            price=Decimal("10"),
            creation_timestamp=0,
            leverage=1,
            position=PositionAction.OPEN,
        )
        sell_order = InFlightOrder(
            client_order_id="sell-1",
            exchange_order_id="2",
            trading_pair="BTC-USDT",
            order_type=OrderType.LIMIT,
            trade_type=TradeType.SELL,
            amount=Decimal("1"),
            price=Decimal("10"),
            creation_timestamp=0,
            leverage=1,
            position=PositionAction.OPEN,
        )

        balances = self.exchange.in_flight_asset_balances(
            {buy_order.client_order_id: buy_order, sell_order.client_order_id: sell_order}
        )

        self.assertNotIn("BTC", balances)
        self.assertEqual(Decimal("20.0040"), balances["USDT"])

    def test_in_flight_asset_balances_does_not_lock_collateral_for_close_order(self):
        close_order = InFlightOrder(
            client_order_id="close-1",
            exchange_order_id="3",
            trading_pair="BTC-USDT",
            order_type=OrderType.LIMIT,
            trade_type=TradeType.SELL,
            amount=Decimal("1"),
            price=Decimal("10"),
            creation_timestamp=0,
            leverage=1,
            position=PositionAction.CLOSE,
        )

        balances = self.exchange.in_flight_asset_balances({close_order.client_order_id: close_order})

        self.assertEqual({}, balances)

    async def test_update_positions_short_amount_is_signed(self):
        self.exchange._api_post = AsyncMock(return_value={
            "success": True,
            "result": [
                {
                    "event_time": "1710000060000000000",
                    "sub_account_id": "527106154284817",
                    "instrument": "BTC_USDT_Perp",
                    "size": "-0.01",
                    "notional": "-600",
                    "entry_price": "59990",
                    "mark_price": "60010",
                    "quote_index_price": "1",
                    "unrealized_pnl": "-0.2",
                    "leverage": "5",
                }
            ],
        })

        await self.exchange._update_positions()

        self.assertEqual(1, len(self.exchange.account_positions))
        position = list(self.exchange.account_positions.values())[0]
        self.assertEqual(PositionSide.SHORT, position.position_side)
        self.assertEqual(Decimal("-0.01"), position.amount)

    async def test_update_positions_short_notional_with_abs_size_is_signed(self):
        self.exchange._api_post = AsyncMock(return_value={
            "success": True,
            "result": [
                {
                    "event_time": "1710000060000000000",
                    "sub_account_id": "527106154284817",
                    "instrument": "BTC_USDT_Perp",
                    "size": "0.01",
                    "notional": "-600",
                    "entry_price": "59990",
                    "mark_price": "60010",
                    "quote_index_price": "1",
                    "unrealized_pnl": "-0.2",
                    "leverage": "5",
                }
            ],
        })

        await self.exchange._update_positions()

        self.assertEqual(1, len(self.exchange.account_positions))
        position = list(self.exchange.account_positions.values())[0]
        self.assertEqual(PositionSide.SHORT, position.position_side)
        self.assertEqual(Decimal("-0.01"), position.amount)

    async def test_process_position_event_removes_opposite_side_for_oneway(self):
        self.exchange._perpetual_trading.set_position(
            self.exchange._perpetual_trading.position_key("BTC-USDT", PositionSide.LONG),
            position=Position(
                trading_pair="BTC-USDT",
                position_side=PositionSide.LONG,
                unrealized_pnl=Decimal("0"),
                entry_price=Decimal("60000"),
                amount=Decimal("0.01"),
                leverage=Decimal("1"),
            ),
        )
        position_event = self._load_fixture("user_position_event.json")
        position_event["params"]["data"]["feed"]["size"] = "-0.02"
        position_event["params"]["data"]["feed"]["notional"] = "-1200"

        await self.exchange._process_position_event(position_event)

        self.assertEqual(1, len(self.exchange.account_positions))
        position = list(self.exchange.account_positions.values())[0]
        self.assertEqual(PositionSide.SHORT, position.position_side)
        self.assertEqual(Decimal("-0.02"), position.amount)

    async def test_process_position_event_short_notional_with_abs_size_is_signed(self):
        position_event = self._load_fixture("user_position_event.json")
        position_event["params"]["data"]["feed"]["size"] = "0.02"
        position_event["params"]["data"]["feed"]["notional"] = "-1200"

        await self.exchange._process_position_event(position_event)

        self.assertEqual(1, len(self.exchange.account_positions))
        position = list(self.exchange.account_positions.values())[0]
        self.assertEqual(PositionSide.SHORT, position.position_side)
        self.assertEqual(Decimal("-0.02"), position.amount)

    async def test_fetch_last_fee_payment(self):
        self.exchange._api_post = AsyncMock(return_value=self._load_fixture("funding_payment_history.json"))

        timestamp, rate, amount = await self.exchange._fetch_last_fee_payment("BTC-USDT")

        self.assertEqual(1710003600.0, timestamp)
        self.assertEqual(Decimal("-1"), rate)
        self.assertEqual(Decimal("-0.12"), amount)
        self.assertEqual("527106154284817", self.exchange._api_post.call_args.kwargs["data"]["sub_account_id"])

    async def test_user_stream_order_and_trade_event_processing(self):
        self.exchange.start_tracking_order(
            order_id="HBOT-1",
            exchange_order_id="987654",
            trading_pair="BTC-USDT",
            trade_type=TradeType.BUY,
            amount=Decimal("0.01"),
            price=Decimal("60000"),
            order_type=OrderType.LIMIT,
            position_action=PositionAction.OPEN,
        )

        order_event = self._load_fixture("user_order_event.json")
        order_event["params"]["data"]["feed"]["metadata"]["client_order_id"] = self.exchange._exchange_client_order_id("HBOT-1")
        self.exchange._process_order_event(order_event)
        tracked = self.exchange.in_flight_orders["HBOT-1"]
        self.assertEqual(OrderState.OPEN, tracked.current_state)

        trade_event = self._load_fixture("user_fill_event.json")
        trade_event["params"]["data"]["feed"]["client_order_id"] = self.exchange._exchange_client_order_id("HBOT-1")
        self.exchange._process_trade_event(trade_event)
        self.assertGreaterEqual(len(tracked.order_fills), 1)

    async def test_set_trading_pair_leverage_success(self):
        self.exchange._api_post = AsyncMock(return_value={"success": True})

        success, msg = await self.exchange._set_trading_pair_leverage("BTC-USDT", 3)

        self.assertTrue(success)
        self.assertEqual("", msg)
        self.assertEqual(3, self.exchange._perpetual_trading.get_leverage("BTC-USDT"))
