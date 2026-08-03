# Realization judge eval

Offline scoreboard for the risk-**materialization** judge (`change_detection/realization.py`). A
flagged risk "materializes" when the adverse **consequence** it warned of is disclosed in a
subsequent 8-K/6-K — a breach, a key executive actually departing, a downgrade/default, an
announced impairment, a lawsuit — **not** when the company merely does more of the risky thing
(issues debt, amends a facility, reshuffles a role). This harness measures how well the shipped
judge holds that line.

## Files

- `realization_gold.jsonl` — 50 real risk→events pairs pulled from production (6 companies we
  studied by hand + a 12-company breadth sample), each hand-labeled `gold_realized` + `gold_events`
  (which event index would count as the realization) against the consequence bar. Risk text and
  event summaries are inline, so the file is self-contained.
- `run_realization_eval.py` — imports the **live** `_SYSTEM_PROMPT` / `_build_user_prompt` (so the
  eval can never drift from what ships), runs the judge over the gold set, and prints
  precision/recall/F1, every disagreement, and the raw verdicts (edit a label and re-score for free
  without re-calling the model).
- `build_gold.py` — one-time script recording how the labels were authored and why (its rationale
  strings are the audit trail; it reads a `/tmp/pairs.json` dump that is no longer needed — the
  labeled data now lives in the jsonl).

## Result driving this change

`claude-sonnet-4-6` + the consequence prompt (`realization-49edc181`): **precision 1.0, recall
1.0** across all 50. Haiku on the identical prompt scores 0.50 / 1.0 — it speculates that an
earnings release "would reveal" a risk and reads an exec's move to a non-executive role as a
departure — so the judge runs on Sonnet. The prior bar (a risk realized by any concrete activity in
its domain) mis-tagged salient events wholesale (one merger "realizing" many risks; a CEO
appointment "realizing" a labor risk); the consequence bar removes that class.

## Re-run

```sh
uv run --project orchestrator python orchestrator/eval/run_realization_eval.py
```

Needs `ANTHROPIC_API_KEY`. Drift check: the printed `prompt_sha` must equal the hash inside
`realization_version()` — if you change the production prompt, re-run and re-check the number.
