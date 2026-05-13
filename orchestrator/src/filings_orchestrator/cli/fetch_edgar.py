"""CLI: fetch recent 8-K filings for one ticker and print a summary.

Usage:
    uv run fetch-edgar AAPL
    uv run fetch-edgar AAPL --limit 5
"""

from __future__ import annotations

import argparse
import sys

from filings_orchestrator.config import MissingConfigError, load_config
from filings_orchestrator.edgar import EdgarClient, recent_8k_filings


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fetch-edgar",
        description="Fetch recent 8-K filings from SEC EDGAR for a ticker.",
    )
    parser.add_argument("ticker", help="Stock ticker, e.g., AAPL")
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of filings to return (default: 10)",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except MissingConfigError as e:
        sys.exit(
            f"{e}\nCopy orchestrator/.env.example to orchestrator/.env and fill in real values."
        )

    with EdgarClient(user_agent=config.edgar_user_agent) as client:
        filings = recent_8k_filings(args.ticker, client, limit=args.limit)

    if not filings:
        print(f"No 8-K filings found for {args.ticker}.")
        return

    company = filings[0].company_name
    print(f"{company} ({args.ticker.upper()}) — {len(filings)} recent 8-K filing(s):")
    print()
    for f in filings:
        items_repr = ", ".join(item.number for item in f.items) if f.items else "—"
        print(f"  {f.filing_date.isoformat()}  Items: {items_repr}")
        print(f"    {f.primary_document_url}")


if __name__ == "__main__":
    main()
