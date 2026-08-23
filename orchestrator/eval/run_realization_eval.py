"""Score the PRODUCTION realization judge against the hand-labeled gold set.

Imports the live `_SYSTEM_PROMPT` and `_build_user_prompt` from the orchestrator so the eval can
never drift from what ships — re-run it after any prompt change and the printed `prompt_sha` must
equal `realization_version()`'s hash. Reads realization_gold.jsonl (risk text + events + gold
labels all inline), runs the judge, and reports precision/recall plus every disagreement. Raw
verdicts are printed so labels can be revised and re-scored without re-calling the model.

Run under the orchestrator venv (so `filings_orchestrator` imports) with ANTHROPIC_API_KEY set:
    uv run --project <orchestrator> python run_realization_eval.py [--model NAME] [--gold PATH]
"""

import argparse
import hashlib
import json
import os
import sys

from anthropic import Anthropic

from filings_orchestrator.change_detection.realization import (
    _SYSTEM_PROMPT,
    DEFAULT_REALIZATION_MODEL,
    RealizationEvent,
    _build_user_prompt,
    evidence_is_grounded,
    quote_is_grounded,
)

TOOL = {
    "name": "submit_realization",
    "description": "Submit the risk-realization verdict. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_realized": {"type": "boolean"},
            "event_index": {"type": ["integer", "null"]},
            "quote": {"type": "string"},
            "evidence": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["is_realized", "event_index", "quote", "evidence", "confidence"],
    },
}


def _events(rec):
    # The gold set carries only the one-line summary, so it stands as the disclosure text the
    # judge quotes from — the same grounding mechanism the production path applies to 8-K bodies.
    return [
        RealizationEvent(e["date"], e["type"], e["item"], e["summary"], e["summary"])
        for e in rec["events"]
    ]


def user_prompt(rec):
    return _build_user_prompt(rec["risk_text"] or "", rec["chg"], _events(rec))


def judge(client, model, rec):
    msg = client.messages.create(
        model=model,
        max_tokens=300,
        temperature=0,
        system=_SYSTEM_PROMPT,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "submit_realization"},
        messages=[{"role": "user", "content": user_prompt(rec)}],
    )
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input
    return {
        "is_realized": False,
        "event_index": None,
        "quote": "",
        "evidence": "",
        "confidence": 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_REALIZATION_MODEL)
    ap.add_argument(
        "--gold", default=os.path.join(os.path.dirname(__file__), "realization_gold.jsonl")
    )
    args = ap.parse_args()

    gold = [json.loads(line) for line in open(args.gold) if line.strip()]
    client = Anthropic()

    tp = fp = fn = tn = 0
    ungrounded = 0
    ungrounded_evidence = 0
    verdicts, disagreements, fabrications, embellishments = [], [], [], []
    for rec in gold:
        v = judge(client, args.model, rec)
        n = len(rec["events"])
        idx = v.get("event_index")
        pred_realized = bool(v.get("is_realized")) and isinstance(idx, int) and 1 <= idx <= n
        # Fabrication check: a realized verdict's quote must be verbatim in the cited event's
        # disclosure. An ungrounded quote is exactly the "designated future CEO" failure class.
        if pred_realized and not quote_is_grounded(
            v.get("quote", ""), rec["events"][idx - 1]["summary"]
        ):
            ungrounded += 1
            fabrications.append(
                {
                    "id": rec["id"],
                    "company": rec["company"],
                    "pred_event": idx,
                    "quote": (v.get("quote") or "")[:200],
                    "summary": rec["events"][idx - 1]["summary"][:200],
                }
            )
        # Embellishment check: the sentence the page renders must assert only what the quote,
        # the risk factor, or the disclosure already say. This is the gate that catches an
        # invented title riding along on a correctly-verified quote.
        if pred_realized:
            ok, unsupported = evidence_is_grounded(
                v.get("evidence", ""),
                sources=[
                    v.get("quote", ""),
                    rec["risk_text"] or "",
                    rec["events"][idx - 1]["summary"],
                ],
            )
            if not ok:
                ungrounded_evidence += 1
                embellishments.append(
                    {
                        "id": rec["id"],
                        "company": rec["company"],
                        "gold": rec["gold_realized"],
                        "unsupported": unsupported,
                        "evidence": (v.get("evidence") or "")[:250],
                    }
                )
        correct_pos = rec["gold_realized"] and pred_realized and idx in rec["gold_events"]
        if correct_pos:
            tp += 1
        elif pred_realized:
            fp += 1
        if rec["gold_realized"] and not correct_pos:
            fn += 1
        if not rec["gold_realized"] and not pred_realized:
            tn += 1
        verdicts.append(
            {
                "id": rec["id"],
                "is_realized": v.get("is_realized"),
                "event_index": idx,
                "quote": v.get("quote", ""),
                "evidence": v.get("evidence", ""),
            }
        )
        if pred_realized != rec["gold_realized"] or (correct_pos is False and rec["gold_realized"]):
            disagreements.append(
                {
                    "id": rec["id"],
                    "company": rec["company"],
                    "gold": rec["gold_realized"],
                    "gold_events": rec["gold_events"],
                    "pred": pred_realized,
                    "pred_event": idx,
                    "evidence": (v.get("evidence") or "")[:200],
                }
            )

    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec_ = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec_ / (prec + rec_) if (prec + rec_) else 0.0
    print(
        json.dumps(
            {
                "model": args.model,
                "prompt_sha": hashlib.sha256(_SYSTEM_PROMPT.encode()).hexdigest()[:8],
                "n": len(gold),
                "TP": tp,
                "FP": fp,
                "FN": fn,
                "TN": tn,
                "precision": round(prec, 3),
                "recall": round(rec_, 3),
                "f1": round(f1, 3),
                "ungrounded_realized": ungrounded,
                "ungrounded_evidence": ungrounded_evidence,
                "fabrications": fabrications,
                "embellishments": embellishments,
                "disagreements": disagreements,
            },
            indent=2,
        )
    )
    print("RAW_VERDICTS=" + json.dumps(verdicts), file=sys.stderr)


if __name__ == "__main__":
    main()
