"""CLI: fetch recent 8-K filings for one ticker, optionally show one body.

Usage:
    uv run fetch-edgar AAPL                  # list the 10 most recent 8-Ks
    uv run fetch-edgar AAPL --limit 5
    uv run fetch-edgar AAPL --detail 0       # also fetch and print the body
                                             # of the first listed filing
"""

from __future__ import annotations

import argparse
import sys

from filings_orchestrator.config import MissingConfigError, load_config
from filings_orchestrator.edgar import EdgarClient, fetch_filing_document, recent_8k_filings

_PREVIEW_CHARS = 4000


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
        help="Maximum number of filings to list (default: 10)",
    )
    parser.add_argument(
        "--detail",
        type=int,
        default=None,
        metavar="INDEX",
        help="Also fetch and print the body of the filing at this list index",
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
        for idx, f in enumerate(filings):
            items_repr = ", ".join(item.number for item in f.items) if f.items else "—"
            print(f"  [{idx}] {f.filing_date.isoformat()}  Items: {items_repr}")
            print(f"      {f.primary_document_url}")

        if args.detail is None:
            return

        if not 0 <= args.detail < len(filings):
            sys.exit(
                f"--detail {args.detail} is out of range; valid indexes are 0..{len(filings) - 1}"
            )

        target = filings[args.detail]
        print()
        print(f"=== Fetching body of filing [{args.detail}] {target.filing_date.isoformat()} ===")
        document = fetch_filing_document(target, client)

    print(
        f"Body size: {document.raw_size_bytes:,} bytes raw HTML, {len(document.text):,} chars text"
    )
    if document.items:
        print(f"Detected {len(document.items)} Item section(s) in the body:")
        for item in document.items:
            preview = item.text[:300].replace("\n", " ")
            print(f"  Item {item.number}: {(item.title or '').strip()[:80]}")
            print(f"    {preview}{'...' if len(item.text) > 300 else ''}")
    else:
        print("(no Item section headings detected; falling back to full-body preview)")
        print()
        print(document.text[:_PREVIEW_CHARS])
        if len(document.text) > _PREVIEW_CHARS:
            print(f"... [{len(document.text) - _PREVIEW_CHARS} more chars]")


if __name__ == "__main__":
    main()
