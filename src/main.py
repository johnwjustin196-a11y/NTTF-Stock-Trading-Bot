"""Entry point.

Usage:
    python -m src.main                         # run scheduler forever
    python -m src.main --once pre_market       # run pre-market scan and exit
    python -m src.main --once decide           # run one decision cycle and exit
    python -m src.main --once audit            # run EOD audit and exit
    python -m src.main --once tune_weights     # re-run the signal-weight tuner
    python -m src.main --mode sim              # force sim mode for this run
"""
from __future__ import annotations

import argparse
import os
import sys

# Force UTF-8 console output so arrow/bullet chars in logs don't crash on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from .broker import get_broker
from .learning import run_eod_reflection
from .scheduler import run_forever
from .screener import build_shortlist
from .trading import run_decision_cycle
from .utils.config import load_config
from .utils.logger import get_logger
from .utils.market_time import now_eastern

log = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stock trading bot")
    parser.add_argument("--once", choices=["pre_market", "decide", "audit", "tune_weights"],
                        help="Run a single stage and exit")
    parser.add_argument("--mode", choices=["sim", "alpaca_paper", "alpaca_live"],
                        help="Override broker.mode from settings.yaml for this run")
    parser.add_argument("--label", default=None,
                        help="Cycle label for --once decide (e.g. 09:30)")
    args = parser.parse_args()

    if args.mode:
        # config is cached; set env override so future loads see the override.
        # Simpler: mutate the loaded dict directly.
        cfg = load_config()
        cfg["broker"]["mode"] = args.mode
        log.info(f"Overriding broker.mode -> {args.mode}")

    if args.once == "pre_market":
        # Also refresh the market regime so a manual pre_market run mirrors the
        # scheduled one.
        try:
            from .analysis import classify_market_regime
            r = classify_market_regime(force=True)
            log.info(f"Pre-market regime: {r['label'].upper()} — {r['reason'][:160]}")
        except Exception as e:
            log.warning(f"regime classification failed: {e}")
        build_shortlist()
        return 0

    if args.once == "decide":
        broker = get_broker()
        label = args.label or now_eastern().strftime("%H:%M")
        run_decision_cycle(broker, label)
        return 0

    if args.once == "audit":
        broker = get_broker()
        run_eod_reflection(broker)
        return 0

    if args.once == "tune_weights":
        from .learning.signal_weights import tune_signal_weights
        result = tune_signal_weights()
        log.info(f"weight tuner result: {result}")
        return 0

    # default: run scheduler forever
    _crash_tb = None
    _crash_reason = "Bot stopped (clean shutdown)"
    try:
        run_forever()
    except KeyboardInterrupt:
        _crash_reason = "Bot stopped by user (Ctrl+C or window closed)"
    except Exception as e:
        import traceback as _tb
        _crash_reason = f"Bot CRASHED: {type(e).__name__}: {e}"
        _crash_tb = _tb.format_exc()
        raise
    finally:
        from .utils.crash_report import send_crash_report
        send_crash_report(_crash_reason, _crash_tb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
