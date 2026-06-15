# 0031. Alerting via a transactional outbox, emitted from anywhere, drained to Discord

- **Status:** Proposed
- **Date:** 2026-06-15

## Context

The observability foundation is now in place — OTel spans, the cost surface
([ADR 0029](0029-near-realtime-8k-ingest-via-atom-feed.md)), the `/ops`
dashboard — but it is all *pull*: it answers questions only when the operator
opens a page. Several conditions need to *push*: a dead-lettered classification
([ADR 0030](0030-pipeline-completeness-via-reconcilers.md) abandons a poison
record and emits `classification_abandoned`), a daily-index publication that
never lands on a business day, the spend cap tripping, the orphan backlog
failing to drain. Today nothing reaches the operator; the OTel-Collector outage
on 2026-06-05 was caught only by chance, which is the cost of a foundation that
is never surfaced.

ADR 0030's revisit triggers anticipated this exactly: trigger #1 — *"a stage
begins emitting an external effect that must fire once (alerts, webhooks)... reach
for Temporal-style durable execution or a transactional outbox. The alarms work
is the likely first trigger."* This ADR is that trigger firing. It must answer
two questions: **what is the durable, fire-once mechanism for an alert**, and
**how does an alert get raised in the first place** — without bolting a
centralized health-monitor onto a system whose whole ethos
([ADR 0030](0030-pipeline-completeness-via-reconcilers.md)) is hand-rolled
patterns on SQLite and systemd timers, not adopted infrastructure.

The stakes are modest in blast radius (a solo operator, one EC2 host) but the
design choice compounds: alert-raising call sites will spread through both the Go
service and the Python orchestrator, so the *emit* ergonomics and the *delivery*
substrate are decisions we will be living with at many call sites, in two
languages.

## Decision

**Any component, anywhere, in either language, raises an alert by writing one row
to an `alerts_outbox` table in the shared SQLite database — in the same
transaction as the work that triggered it. A single standalone `alarm-drain` CLI,
fired by a systemd timer, is the only Discord-aware component: it drains
undelivered rows, POSTs each to the severity-appropriate Discord channel, and
marks it delivered. Liveness of the host itself — which nothing on the host can
report — is owned by a separate external dead-man's-switch.**

- **Emit from the source, not from an overseer.** The component that knows about
  the trouble reports it, via a thin `emit(severity, title, fields)` helper that
  is nothing but an `INSERT` into `alerts_outbox`. It exists in both the Go
  service and the Python orchestrator; both already hold the same SQLite database
  open, so emission carries no new dependency at the call site. There is no
  central poller re-deriving system health from the outside.

- **The outbox is a transactional outbox.** Because the alert row is written in
  the same DB transaction as the state change that warrants it, the alert and the
  fact it reports commit together or not at all. There is no "the work happened
  but the alert was lost" gap, and no "alerted but the work rolled back" gap. The
  DB — already the one durable substrate every component touches — is the queue.

- **Delivery is a separate, single-purpose utility.** `alarm-drain` is a lean CLI
  with one job: read undelivered rows, deliver, mark delivered, retry with a
  bounded attempt count on failure. It shares only unavoidable plumbing (the
  engine, config/secrets, migrations) with the other CLIs and has zero coupling
  to classify/reduce. Undelivered rows simply remain undelivered until they
  succeed — durability and "drain the backlog" fall out of the table, with no
  broker. A dedup watermark prevents a standing condition from re-paging every
  tick.

- **Detection is mostly inline; absence is the one exception.** Discrete events
  ("I just abandoned this classification") `emit()` at their call site. The one
  class that has *no* call site is **absence** — "the daily index never
  published today" — because nothing executes at the moment of non-occurrence.
  That class, and slow-moving trend alarms, are raised by thin timer-driven
  checks that are themselves just more `emit()` callers, not a central health
  brain.

- **Two Discord channels = severity routing.** `#alerts` (needs human action) and
  `#info` (situational awareness) are two incoming-webhook URLs; severity on the
  outbox row selects which. Webhook URLs live in SSM Parameter Store as
  SecureStrings, like every other secret.

- **Transport is swappable behind a `Notifier` seam.** `alarm-drain` renders a
  transport-neutral notification to a provider via a `Notifier` interface;
  `DiscordNotifier` is the first implementation. Discord's payload shape never
  leaks into call sites (they only ever touch `emit()`), so a later move to Slack
  is one new implementation plus a different webhook URL.

- **Host liveness is external, by necessity.** No internal mechanism — outbox,
  drainer, or a bus — can report "the host is dead or unreachable"; that is
  self-monitoring's irreducible blind spot. A separate CloudWatch path (an EC2
  status-check alarm plus a heartbeat the box refreshes each drain tick, alarming
  on staleness → SNS→email) owns liveness. This, not internal durability, is the
  real answer to "what if the alerter is down."

## Alternatives considered

### SNS → SQS → consumer (a real message bus) now

Rejected as premature, adopted as the *upgrade path*. A bus buys decoupling,
durable buffering, fan-out, and native retry/DLQ — but at one host with one
operator and a shared SQLite database, those properties are already supplied by
a table the system holds open, and the bus adds an SNS topic, an SQS queue, IAM,
and an always-on consumer to maintain. Critically, the bus would also require the
AWS SDK and IAM at *every* `emit()` call site in two languages, which is friction
at exactly the spots we want frictionless. The outbox is designed so the bus is a
drop-in later substitution: the day producers go multi-host or fan-out to
multiple teams is wanted, `alarm-drain` points at SQS instead of the table and no
`emit()` call site changes. See "When to revisit" below.

### A centralized health scanner ("oversight system")

Rejected. A poller that re-derives all system health from state on a timer is more
coupled and less cohesive than emit-from-source: it must know how to *infer* every
condition that the producing code already knows directly, and it concentrates a
"health brain" the operator explicitly did not want. The model keeps a small
timer-driven component only for **absence/trend** alarms, which genuinely have no
call site — and even that is framed as another `emit()` caller, not an arbiter.

### Point-of-event POST with no outbox

Rejected. Firing the webhook inline at the call site is the simplest thing that
could work for discrete events, but it makes every producer Discord-aware (in two
languages), scatters retry and dedup across the codebase, loses the alert if
Discord or the network is briefly down, and breaks the transactional guarantee
(the POST cannot enlist in the DB transaction). The outbox keeps call sites
trivial and centralizes delivery, retry, and dedup in one utility.

### Internal durability instead of an external liveness check

Rejected as a category error. Investing in SNS/SQS durability to answer "what if
the alerter is down" does not address the dominant failure — the *host* being
down — which no on-host mechanism can report regardless of how durable it is. The
correct and far cheaper answer is one external dead-man's-switch; internal
durability is then a separate, smaller concern that the outbox already covers.

### Slack instead of Discord

Rejected for now, kept trivially reachable. Slack's free tier would in fact cover
this at no cost, and is the more "professional" framing; Discord is chosen as
free-forever with no tier limits, for a solo operator building a
channel-monitoring habit. Because both are incoming-webhook transports behind the
same `Notifier` seam, the choice is reversible by swapping one implementation and
a URL.

## When to revisit (escalate the substrate)

The outbox is the right size only while emission is single-host. Replace the
table substrate with SNS/SQS (or an equivalent broker) when any of these appears,
and record which:

1. **Producers go multi-host or ephemeral.** More than one machine, or
   autoscaled/short-lived workers, emitting alerts — a shared local table is no
   longer the common substrate, and a network bus is.
2. **Fan-out to multiple independent consumers/teams** is wanted (page one team,
   archive to another sink, mirror to a status page) — SNS fan-out earns its
   keep over a single drainer.
3. **Delivery throughput or retry semantics outgrow a timer-drained table** —
   high alert volume, or a need for managed visibility-timeout/DLQ behavior you
   would otherwise re-implement.

Until then the `alerts_outbox` table plus the `alarm-drain` timer *are* the bus,
at the right size — the same "hand-rolled until a trigger fires" stance as
[ADR 0030](0030-pipeline-completeness-via-reconcilers.md).

## What this amends

- **ADR 0030.** Resolves its revisit-trigger #1 (exactly-once external effects).
  0030 named the two options — durable execution (Temporal) or a transactional
  outbox — and this ADR selects the outbox, hand-rolled on SQLite, consistent
  with 0030's deferral of adopted orchestration. `classification_abandoned`,
  introduced by 0030 as "alarm-eligible," becomes a concrete `emit()` call site.
- **ADR 0029 / the `/ops` surface.** Adds the push complement to the existing
  pull-only cost/freshness dashboard; the two read the same underlying state.

## Consequences

- **Easier:** raising an alert is a one-line `emit()` from anywhere in either
  language, with no transport knowledge and no new dependency at the call site.
- **Easier:** alerts are exactly as durable and consistent as the work they
  report — they ride the same transaction — without a broker.
- **Easier:** delivery, retry, dedup, and the transport choice live in exactly
  one swappable place; Discord→Slack is a one-file change.
- **Easier:** the host-down blind spot is covered explicitly and cheaply, rather
  than papered over with internal durability that could never see it.
- **Harder:** a schema addition (`alerts_outbox`) and the discipline that emitting
  an alert is a deliberate, reviewed act — an outbox makes alerts cheap to add,
  so alert-fatigue management (what is `#alerts` vs `#info`, dedup windows)
  becomes an ongoing curation task.
- **Accepted:** delivery latency is bounded by the drain interval (a few minutes),
  not instant — acceptable for these signals, none of which are sub-minute
  critical.
- **Accepted:** the substrate is hand-rolled and will migrate to SNS/SQS once a
  trigger above fires; the migration is bounded by design (only `alarm-drain`'s
  source changes), which is the price of not standing up a bus today.

## Deferred

- **The exact alarm catalogue and routing** — which conditions emit, at which
  severity, and the precise business-day / staleness thresholds — lands with the
  implementing PRs, informed by what the `/ops` dashboard shows is worth paging on.
- **The dedup/watermark mechanism** (per-condition keys, re-alert windows) — settled
  with the `alarm-drain` PR; the outbox + `emit()` foundation PR does not require it.
- **The dead-man's-switch wiring** (CloudWatch status-check alarm + heartbeat
  metric + SNS→email) — a separate, terraform-only slice, independent of the
  on-host outbox work.
- **Placement of the absence/trend emitters** — whether the daily-index-missing and
  backlog checks are their own small CLI or attach to the relevant existing tick —
  decided when that slice lands.
