# Architecture Decision Records

Short, dated, numbered documents capturing substantive engineering decisions on this project. The format follows the [Nygard ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions). Each ADR records the context that forced a decision, the decision itself, the alternatives considered, and the consequences accepted.

## Why ADRs

- **Decisions outlive their context.** Months later it's easy to forget *why* a choice was made — the ADR is the durable answer.
- **Alternatives matter as much as the choice.** Recording what was rejected and why prevents relitigation.
- **Portfolio signal.** Structural reasoning about tradeoffs reads as senior engineering to anyone browsing the repo.

## Conventions

- Numbered sequentially: `0001-`, `0002-`, etc. — zero-padded for sort order
- One decision per file
- Status: `Proposed` → `Accepted` → optionally `Superseded by NNNN` later
- Dates are absolute (e.g., `2026-05-12`), not relative
- Short — most ADRs are 1-2 screens of markdown; longer suggests the decision needs decomposition

## When to write an ADR

Write one when the decision:

- Has substantive alternatives that were rejected
- Will be expensive to reverse later
- Future-you (or a future contributor) will need to understand *why* to maintain or extend the code

Skip ADRs for: routine choices with no real tradeoff, conventions already documented elsewhere (see [CONTRIBUTING.md](../../CONTRIBUTING.md)), or anything that's just preference.

## Adding a new ADR

1. Copy [template.md](template.md) to `NNNN-short-slug.md` using the next available number
2. Fill in context, decision, alternatives, consequences
3. Status starts as `Proposed`; flip to `Accepted` once the decision lands (often the same PR)
4. Reference the ADR from the relevant code or doc when useful (e.g., a code comment can cite `docs/decisions/0007-...md` for the why)

## Superseding

When a decision is reversed, do **not** delete the old ADR. Instead:

1. Write a new ADR explaining the new direction and why the old one is no longer right
2. Update the old ADR's status to `Superseded by NNNN`
3. The historical reasoning remains visible — that's the point
