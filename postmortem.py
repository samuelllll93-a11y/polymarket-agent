"""
Nightly self-improvement loop for the Polymarket trading bot.

Runs after market close each day to:
- Review all trades taken (wins, losses, edge cases)
- Compare predicted probabilities against actual Polymarket prices
- Identify systematic errors per agent (weather, BTC, politics, sports)
- Generate a structured postmortem report saved to reports/
- Send a summary via Telegram
- Update rolling accuracy metrics used by agents for self-calibration
"""

import asyncio
from pathlib import Path
from datetime import date


REPORTS_DIR = Path(__file__).parent / "reports"


def load_daily_trades(trade_date: date) -> list:
    """Load all trades logged for the given date from the local cache."""
    raise NotImplementedError


def analyze_agent_performance(trades: list) -> dict:
    """Compute win rate, average edge, and calibration error per agent."""
    raise NotImplementedError


def generate_report(analysis: dict, trade_date: date) -> Path:
    """Write a structured markdown postmortem report to the reports/ directory."""
    raise NotImplementedError


async def run_postmortem(trade_date: date = None):
    """
    Execute the full nightly postmortem pipeline.

    Args:
        trade_date: Date to analyze. Defaults to today.
    """
    raise NotImplementedError


if __name__ == "__main__":
    asyncio.run(run_postmortem())
