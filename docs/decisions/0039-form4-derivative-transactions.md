# 0039. Store Form 4 derivative (option) transactions

- **Status:** Accepted
- **Date:** 2026-06-26

## Context

ADR 0037 scoped the Form-4 ingest to the **non-derivative** table only (common-stock buys/sells), deferring the derivative table (options, warrants, convertibles) on the grounds that the headline signal is open-market purchases. In practice that was over-narrow: it *discarded* roughly half of every day's Form 4s (option-only filings) from storage entirely, even though the data is part of the same fetch and parse.

It also interacted badly with dedup (ADR 0038): option-only filings produced no rows anywhere, which is part of why the envelope anchor was needed.

## Decision

Parse and store the derivative table too, in a dedicated `insider_derivative_transactions` table — separate from `insider_transactions` because derivative lines carry fields the non-derivative table has no place for: the strike (`conversion_exercise_price`), the exercise and expiration dates, and the underlying security (title + share count). The parser gains a `DerivativeTransaction` model and a `Form4Filing.derivative_transactions` list; the envelope's `derivative_count` is now populated.

This follows the same **store-everything, score-narrow** rule the non-derivative table already uses: we persist all transaction codes (grants `A`, exercises `M`, dispositions, …) and let downstream scoring select what matters (open-market buys remain the headline signal).

## Alternatives considered

- **One table with an `is_derivative` flag + nullable derivative columns** — fewer tables, but a wide sparse row and a mix of two genuinely different record shapes. A dedicated table keeps each shape clean and queries unambiguous.
- **Keep deferring** — rejected: it discards data that is free to capture, has clear future signal value (exercise-and-hold, off-cycle or unusually large grants, exercise timing), and leaves the corpus incomplete for the backtest.

## Consequences

- The Form-4 corpus is now complete per filing — both tables captured — so the envelope's `derivative_count` and `non_derivative_count` fully describe a filing's contents.
- Scoring (next) treats derivative activity as a separate, lower-weight lens; the open-market non-derivative buy stays the hero signal. Nothing here changes that emphasis — only what is *stored*.
- `transaction_value` is computed as `shares × price_per_share` when both are present; for grants (price 0 / absent) it is 0 or null, as expected.
- Pre-existing option-only filings (filed before this change) are not retroactively stored; they fill in when their dates are re-scanned by the backfill.
