"""One-shot script: pull account + positions from Alpaca and write to state.json.

Run via sync-alpaca.bat. Safe to run at any time — read-only from Alpaca,
only writes to data/state.json.
"""
import sys
from pathlib import Path

# Make sure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.broker.alpaca_broker import AlpacaBroker

def main():
    print("Connecting to Alpaca paper account...")
    try:
        broker = AlpacaBroker(paper=True)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print("Fetching account + positions...")
    acct = broker.get_account()

    print(f"\n  Cash:          ${acct.cash:,.2f}")
    print(f"  Equity:        ${acct.equity:,.2f}")
    print(f"  Buying Power:  ${acct.buying_power:,.2f}")
    print(f"  Open Positions: {len(acct.positions)}")

    if acct.positions:
        print()
        for pos in acct.positions:
            unrl_pct = (pos.unrealized_pl / (pos.avg_entry * pos.quantity)) * 100 if pos.avg_entry and pos.quantity else 0
            print(f"    {pos.symbol:<6}  qty={pos.quantity:.0f}  entry=${pos.avg_entry:.2f}"
                  f"  mv=${pos.market_value:,.2f}  unrl={pos.unrealized_pl:+,.2f} ({unrl_pct:+.1f}%)"
                  f"  stop={pos.stop_loss}")

    print(f"\nstate.json updated at: {broker._state_path}")
    print("Dashboard will reflect these values on next refresh.")

if __name__ == "__main__":
    main()
