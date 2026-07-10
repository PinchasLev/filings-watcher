# 0041. Alarm on daily-index cursor staleness

- **Status:** Accepted
- **Date:** 2026-07-10

## Context

The 2026-07-10 PDF-OOM incident (ADR 0040) ran ~2 days as a silent outage. The
daily-index reconciler OOM-killed on a poison filing every run, so its cursor sat
stuck days in the past while the operator had no signal. None of the existing
alarms covered it:

- The **"daily index not published"** alarm (scan_daily_index) fires only when
  *today's* index is missing at the end of the evening cluster. During the
  incident today's index published fine — the cursor was stuck on an *old* date it
  could not advance past. Wrong question.
- The **ClassifierOOMKill** metric/alarm (ADR 0035) is cause-specific — it depends
  on the OOM→metric→alarm→Discord path working. In this incident it did not page
  (a separate latent bug, tracked in the incident memo).
- The **atom** freshness clock on `/ops` is display-only, and the atom path
  self-heals anyway (a stuck filing ages out of EDGAR's rolling window), so it
  never looked stuck for long.

What was missing is a check on the *outcome* — "is ingest actually keeping up?" —
that fires regardless of the cause.

## Decision

Add a **cursor-staleness dead-man's switch**: a small check that reads only the
`ingest_cursor` high-water date and the calendar, and alarms when the reconciler
has fallen too far behind. It is deliberately cause-agnostic — it pages the same
way for an OOM, a hang, a cost-cap wedge, or a timer that never armed.

- **New CLI `check-ingest-freshness`** reads the cursor, counts business-day lag to
  today, and on a stall queues one outbox alert (ADR 0031) with a per-cause dedup
  key (`ingest_cursor_stale`) — so a standing stall pages once per drainer repeat
  window and goes silent when the cursor catches up. It touches no EDGAR/Anthropic
  credential and does no fetch/parse/classify, so it cannot itself OOM or hang the
  way a tick can: it is the watcher that stays alive when a classifier tick is the
  thing that died. It runs on its own light self-arming timer (`OnUnitInactiveSec=1h`
  + `OnBootSec`), outside the classifier resource slice.

- **Staleness is measured in business days, not wall-clock.** The cursor
  legitimately sits at "yesterday" between evening ticks and at "last Friday" all
  weekend; a wall-clock age would cry wolf every weekend and holiday — the exact
  false-positive class we have been bitten by before. Lag is the count of
  fully-elapsed business days strictly between the cursor date and today. The
  threshold (default 3, env-overridable) carries enough slack that the weekday-only
  calendar's holiday over-count cannot trip it (the largest routine US market
  closure is two consecutive weekdays), while a genuine multi-day stall — the
  incident was ~8 business days — clears it comfortably.

- **A second freshness clock on `/ops`** renders the cursor's high-water date and a
  coarse days-behind, beside the existing atom clock, so the reconciler's health is
  visible at a glance and not only via the page.

- The weekday-only business-day logic (`is_business_day`) is extracted to a shared
  `business_days` module, now used by both the reconciler and this check.

## Consequences

- A silent multi-day ingest stall now pages, whatever the cause — the general net
  the incident showed we lacked. This is the primary durable fix; diagnosing why
  the cause-specific OOM alarm did not fire is a separate, secondary follow-up.
- Detection lags a real stall by up to ~3 business days by design — the price of
  zero holiday false positives with a weekday-only calendar. Tightening the
  threshold toward 2 for faster detection wants a holiday-aware calendar first
  (the tracked `is_business_day` follow-up); the threshold is env-overridable so an
  extraordinary closure can be absorbed without a deploy.
- An unset cursor (fresh install, before the first tick) does **not** alarm — we
  cannot distinguish "never ran" from "just installed", and a wholly dead system is
  caught by the host/atom signals.
- Installing the new timer needs the infra step re-run (terraform apply on the SSM
  document, then `aws ssm send-command` to execute it), like any timer change.
