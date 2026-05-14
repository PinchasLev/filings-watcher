"""CLI: classify the Nth recent 8-K for a ticker.

Usage:
    uv run classify-filing AAPL 0
    uv run classify-filing TSLA 4 --json
"""

from __future__ import annotations

import argparse
import os
import sys

from filings_orchestrator.classify import classify_filing
from filings_orchestrator.config import MissingConfigError, load_config
from filings_orchestrator.edgar import EdgarClient, fetch_filing_document, recent_8k_filings


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="classify-filing",
        description="Fetch one recent 8-K for a ticker and classify each Item via Claude.",
    )
    parser.add_argument("ticker", help="Stock ticker, e.g., AAPL")
    parser.add_argument(
        "index",
        type=int,
        help="Index of the filing in the recent-list (0 = most recent)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent filings to list when resolving the index (default: 10)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human summary",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except MissingConfigError as e:
        sys.exit(
            f"{e}\nCopy orchestrator/.env.example to orchestrator/.env and fill in real values."
        )

    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    os.environ["LANGSMITH_API_KEY"] = config.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = config.langsmith_project
    os.environ["LANGSMITH_TRACING"] = "true" if config.langsmith_tracing else "false"

    with EdgarClient(user_agent=config.edgar_user_agent) as client:
        filings = recent_8k_filings(args.ticker, client, limit=args.limit)
        if not 0 <= args.index < len(filings):
            sys.exit(
                f"index {args.index} out of range; "
                f"found {len(filings)} recent filing(s) for {args.ticker}"
            )
        document = fetch_filing_document(filings[args.index], client)

    result = classify_filing(document)

    if args.json:
        print(result.model_dump_json(indent=2))
        return

    print(f"{result.company_name} — 8-K filed {result.filing_date}")
    print(f"Accession: {result.accession_number}  Model: {result.model}")
    print()

    if result.items:
        for ic in result.items:
            c = ic.classification
            title_suffix = f": {ic.item_title}" if ic.item_title else ""
            material_marker = "MATERIAL" if c.is_material else "non-material"
            print(f"  Item {ic.item_number}{title_suffix}")
            print(f"    {c.event_type.value}   confidence={c.confidence:.2f}   {material_marker}")
            print(f"    {c.reasoning}")
            print()
    elif result.whole_filing is not None:
        c = result.whole_filing
        material_marker = "MATERIAL" if c.is_material else "non-material"
        print("  (no Item sections extracted; classified the whole filing body)")
        print(f"  {c.event_type.value}   confidence={c.confidence:.2f}   {material_marker}")
        print(f"  {c.reasoning}")
    else:
        print("  (no classifications produced)")


if __name__ == "__main__":
    main()
