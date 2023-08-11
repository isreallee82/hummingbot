import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.client.settings import AllConnectorSettings, ConnectorSetting
from hummingbot.logger import HummingbotLogger

from .async_utils import safe_ensure_future

"""
        Add a new config variable to conf_client.yml: fetch_pairs_from_all_exchanges: False.

        Change the trading pair fetcher so that 
            if fetch_pairs_from_all_exchanges  is False, 
                only fetches pairs from exchanges where the user has added an API key, && the exchanges in paper_trade_exchanges in the conf_client.yml
            elseif True, 
                the fetcher operates as it does now and fetches pairs from all exchange connectors
                
        After a user added API key with connect, 
            fetch pairs from that new exchange
"""
class TradingPairFetcher:
    _sf_shared_instance: "TradingPairFetcher" = None
    _tpf_logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._tpf_logger is None:
            cls._tpf_logger = logging.getLogger(__name__)
        return cls._tpf_logger

    @classmethod
    def get_instance(cls, client_config_map: Optional["ClientConfigAdapter"] = None) -> "TradingPairFetcher":
        if cls._sf_shared_instance is None:
            client_config_map = client_config_map or cls._get_client_config_map()
            cls._sf_shared_instance = TradingPairFetcher(client_config_map)
        return cls._sf_shared_instance

    def __init__(self, client_config_map: ClientConfigAdapter):
        self.ready = False
        self.trading_pairs: Dict[str, Any] = {}
        self._fetch_pairs_from_all_exchanges = client_config_map.fetch_pairs_from_all_exchanges
        # print(f"{self._fetch_pairs_from_all_exchanges}")
        self.paper_trades_in_conf = client_config_map.paper_trade.paper_trade_exchanges
        self._fetch_task = safe_ensure_future(self.fetch_all(client_config_map))

    def _fetch_pairs_from_connector_setting(
            self,
            connector_setting: ConnectorSetting,
            connector_name: Optional[str] = None):
        connector_name = connector_name or connector_setting.name
        connector = connector_setting.non_trading_connector_instance_with_default_configuration()
        safe_ensure_future(self.call_fetch_pairs(connector.all_trading_pairs(), connector_name))

    def _fetch_from_filtered(
            self,
            connector_setting: ConnectorSetting,
            conn_setting: AllConnectorSettings.get_connector_settings(),
            conn_name: Optional[str] = None,
            connector_name: Optional[str] = None,
            filtered_data_list: Optional[Dict[str, Any]] = None):
        conn_name = conn_name or conn_setting.name
        connector_name = connector_name or connector_setting.name
        connector = connector_setting.non_trading_connector_instance_with_default_configuration()
        filtered_data = filter(lambda s: "api_keys" in s, conn_setting.config_keys)
        filtered_data_list = list(filtered_data)
        # print(f"{filtered_data_list}")
        safe_ensure_future(self.call_fetch_pairs(connector.all_trading_pairs(), filtered_data_list.conn_name))

    async def fetch_all(self, client_config_map: ClientConfigAdapter):
        connector_settings = self._all_connector_settings()
        for conn_setting in connector_settings.values():
             # XXX(martin_kou): Some connectors, e.g. uniswap v3, aren't completed yet. Ignore if you can't find the
             # data source module for them.
            try:
                if self._fetch_pairs_from_all_exchanges:
                    if 'api_keys' in conn_setting.config_keys and conn_setting.base_name().endswith("paper_trade"):
                        self._fetch_from_filtered(
                            connector_setting=connector_settings[conn_setting.parent_name],
                            connector_name=conn_setting.name
                        )
                        # print(f"_fetch_from_filtered")
                        # print(f"{conn_setting}")
                else:
                    if conn_setting.base_name().endswith("paper_trade"):
                        self._fetch_pairs_from_connector_setting(
                            connector_setting=connector_settings[conn_setting.parent_name],
                            connector_name=conn_setting.name
                        )
                        # print(f"fetch_pairs_from_connector_setting")
                        # print(f"{conn_setting}")
                    else:
                        self._fetch_pairs_from_connector_setting(connector_setting=conn_setting)
            except ModuleNotFoundError:
                continue
            except Exception:
                self.logger().exception(f"An error occurred when fetching trading pairs for {conn_setting.name}. "
                                        "Please check the logs")
        self.ready = True

    async def call_fetch_pairs(self, fetch_fn: Callable[[], Awaitable[List[str]]], exchange_name: str):
        try:
            pairs = await fetch_fn
            self.trading_pairs[exchange_name] = pairs
            # print(f"{pairs}")
        except Exception:
            self.logger().error(f"Connector {exchange_name} failed to retrieve its trading pairs. "
                                f"Trading pairs autocompletion won't work.", exc_info=True)
            # In case of error just assign empty list, this is st. the bot won't stop working
            self.trading_pairs[exchange_name] = []

    def _all_connector_settings(self) -> Dict[str, ConnectorSetting]:
        # Method created to enabling patching in unit tests
        return AllConnectorSettings.get_connector_settings()

    @staticmethod
    def _get_client_config_map() -> "ClientConfigAdapter":
        from hummingbot.client.hummingbot_application import HummingbotApplication

        client_config_map = HummingbotApplication.main_application().client_config_map
        return client_config_map
