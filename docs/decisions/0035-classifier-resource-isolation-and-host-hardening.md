# 0035. Classifier resource isolation and host hardening

- **Status:** Accepted
- **Date:** 2026-06-22

## Context

On 2026-06-22 the production host wedged twice: the OS became unresponsive (SSH and the SSM agent froze) while EC2 still reported the instance "running" and the website kept serving. It was not a clean crash and it did not self-recover; only a forced stop/start brought it back. Diagnosis surfaced three distinct, compounding weaknesses, none of which was the simple "out of memory" story first assumed (the kernel log showed no OOM-killer, and the box had 2 GB of swap):

1. **No resource isolation on the classifier.** The ingest/reconciler ticks run as systemd one-shot services with no cgroup memory limit, so a single tick can consume all host RAM. With swap present, memory pressure manifested as swap/I/O thrash — a livelock — rather than a contained, fail-fast kill. The box ran for weeks on 8-K volume; adding 6-K classification was enough new load to expose this.

2. **Timers did not self-arm after a reboot.** The `OnUnitInactiveSec` timers (atom-feed, reclassify-orphans, alarm-drain, host-heartbeat) are primed only by an explicit `systemctl start` at *install* time. A plain reboot/stop-start leaves them loaded-but-unscheduled, so after every restart nothing polls the feed and — worse — the host heartbeat stops, blinding the CloudWatch dead-man's-switch and preventing it from ever clearing.

3. **Tailscale MagicDNS broke public DNS on reboot.** Bringing the node up with the default `accept-dns=true` rewrote `/etc/resolv.conf` to `100.100.100.100`, which does not resolve public names on this host. Every tick then failed to reach the SSM API endpoint / EDGAR / Anthropic and the SSM agent could not register — while the website (inbound, no DNS) kept serving, masking the failure.

We were also blind to the cause throughout: default EC2 CloudWatch metrics are hypervisor-level (CPU, disk, network, status checks); memory and swap are guest-internal and uncollected, and no CloudWatch agent is installed.

## Decision

We harden the host along three independent layers, defence-in-depth:

- **Isolation (primary guard):** the three Anthropic-classifying services run under a shared `filings-classify.slice` with `MemoryMax=2G` and `MemorySwapMax=0`. A runaway tick is OOMKilled inside its own cgroup — fail-fast — instead of swap-thrashing the host into a wedge. The cap is a safety *ceiling* sized above the legitimate working set (~150–400 MB observed), not a target; a shared slice bounds the *total* across concurrent ticks. This is the same primitive a Kubernetes pod memory limit uses.
- **Capacity (host reserve):** `instance_type` is raised from `t4g.small` (2 GB) to `t4g.medium` (4 GB), leaving ~1.7 GB reserve for the OS, Caddy, the Go server, and page cache that the cap draws against.
- **Self-arming + DNS correctness:** the interval timers gain `OnBootSec=` so they fire after every boot without a re-install; and the operator brings Tailscale up with `--accept-dns=false` (documented in `infra/README.md` and the user_data comment), so a reboot never re-breaks public DNS.

All of this lives in Terraform / the `filings-install-orchestrate-timer` SSM document so it survives redeploys and host replacement.

## Alternatives considered

### Resize only (no cgroup cap)

Rejected as a fix: more RAM raises the threshold but does not change the failure *mode* — a leak or a pathological filing would still thrash the larger box eventually, silently. The cgroup cap converts a silent whole-host wedge into a contained, logged, observable kill, regardless of instance size. With the cap in place the resize is arguably optional; we keep it as reserve and may downsize later once load is characterised.

### Per-service memory caps instead of a shared slice

Rejected: per-service caps *sum* under concurrency (atom-feed and reclassify-orphans can overlap), so two 2 GB caps could over-commit a 4 GB box. A shared slice bounds the aggregate.

### `vm.panic_on_oom` / watchdog reboot

Rejected: rebooting on memory pressure is a blunter, lossier response than killing the one offending cgroup, and does nothing to prevent recurrence.

### Bake `tailscale up` into user_data

Rejected: node authentication is interactive and operator-run by design (ADR 0014), so the flag is documented on the operator command rather than enforced in user_data.

## Consequences

- A classifier tick that balloons is killed cleanly and logged with the filing it was processing — turning an unexplained wedge into a measurable signal. The host stays up and the website keeps serving through a classifier failure.
- Reboots and stop/starts no longer silently halt ingestion or the heartbeat; the dead-man's-switch recovers on its own.
- We accept that a tick exceeding 2 GB is *killed* rather than allowed to finish — correct for a fail-fast posture, but it means a legitimately heavy operation must fit the budget or be batched (see the back-pressure follow-up).
- Remaining gaps, tracked as follow-ups: (a) **back-pressure** — bound the per-tick batch and the reclassify-orphans per-run set so a backlog drains as a stream, not a burst; (b) **resource observability** — emit memory/swap (and the slice's `MemoryPeak`) to CloudWatch with a swap alarm, so we are warned *before* a wedge and can finally confirm leak-vs-spike.
