from decimal import Decimal
from typing import Dict, List, Optional, Union, TypeVar

from pydantic.v1 import BaseModel

from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType

ExecutorConfigType = TypeVar("ExecutorConfigType", bound=ExecutorConfigBase)

class ExecutorInfo(BaseModel):
    id: str
    timestamp: float
    type: str
    close_timestamp: Optional[float]
    close_type: Optional[CloseType]
    status: RunnableStatus
    config: ExecutorConfigType
    net_pnl_pct: Decimal
    net_pnl_quote: Decimal
    cum_fees_quote: Decimal
    filled_amount_quote: Decimal
    is_active: bool
    is_trading: bool
    custom_info: Dict  # TODO: Define the custom info type for each executor
    controller_id: Optional[str] = None

    @property
    def is_done(self):
        return self.status == RunnableStatus.TERMINATED

    @property
    def side(self) -> Optional[TradeType]:
        return self.custom_info.get("side", None)

    @property
    def trading_pair(self) -> Optional[str]:
        return self.config.trading_pair

    @property
    def connector_name(self) -> Optional[str]:
        return self.config.connector_name

    def to_dict(self):
        base_dict = self.dict()
        base_dict["side"] = self.side
        return base_dict


class PerformanceReport(BaseModel):
    realized_pnl_quote: Decimal = Decimal("0")
    unrealized_pnl_quote: Decimal = Decimal("0")
    unrealized_pnl_pct: Decimal = Decimal("0")
    realized_pnl_pct: Decimal = Decimal("0")
    global_pnl_quote: Decimal = Decimal("0")
    global_pnl_pct: Decimal = Decimal("0")
    volume_traded: Decimal = Decimal("0")
    open_order_volume: Decimal = Decimal("0")
    inventory_imbalance: Decimal = Decimal("0")
    positions_summary: List = []
    close_type_counts: Dict[CloseType, int] = {}
