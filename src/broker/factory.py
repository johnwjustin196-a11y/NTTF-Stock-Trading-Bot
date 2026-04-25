"""Broker selection based on config mode."""
from __future__ import annotations

from ..utils.config import load_config
from ..utils.logger import get_logger
from .base import Broker

log = get_logger(__name__)


def get_broker() -> Broker:
    cfg = load_config()
    mode = cfg["broker"]["mode"]
    live_guard = cfg["trading"]["live_mode"]

    if mode == "sim":
        from .sim import SimBroker
        log.info("Using SimBroker (offline simulation)")
        return SimBroker()

    if mode == "alpaca_paper":
        from .alpaca_broker import AlpacaBroker
        log.info("Using AlpacaBroker in PAPER mode")
        return AlpacaBroker(paper=True)

    if mode == "alpaca_live":
        if not live_guard:
            raise RuntimeError(
                "broker.mode=alpaca_live but trading.live_mode=false. "
                "Flip trading.live_mode to true in settings.yaml to confirm."
            )
        from .alpaca_broker import AlpacaBroker
        log.warning("Using AlpacaBroker in LIVE mode — real money at risk")
        return AlpacaBroker(paper=False)

    raise ValueError(f"Unknown broker.mode: {mode}")
