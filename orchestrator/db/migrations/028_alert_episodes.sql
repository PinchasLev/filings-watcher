-- An alert key that keeps firing is one condition, not many: the drainer pages it once, then once
-- per repeat window as a reminder. What the reminder could not say was how long the condition had
-- lasted or how often it had tripped, and when the condition stopped firing nothing said so — the
-- reminders simply stopped, which reads the same as the alerting itself having died. A month-long
-- credit lapse in October paged the same headline 31 times, each reading exactly like the first.
--
-- The drainer now reconstructs each key's EPISODE from the rows already here (first seen,
-- occurrences, last seen) and renders it into every page, so any single message says whether it
-- is new or ongoing. That needs no new column, but it reads the outbox by key in time order on
-- every page, hence the index.
--
-- `announce_clear` opts a key into an explicit "cleared" message once it has been quiet for the
-- repeat window. It is opt-in rather than universal because every historical per-entity key (one
-- per dead-lettered filing — 15,173 of them from that lapse) is, by that definition, quiet, and
-- would otherwise announce its own clearing on the first pass after deploy. It suits conditions
-- that are re-checked more often than the window; a producer that only checks daily would read
-- as cleared between checks.
ALTER TABLE alerts_outbox ADD COLUMN announce_clear INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_alerts_outbox_dedup_created
    ON alerts_outbox (dedup_key, created_at);

-- A failed Discord POST stored the transport's error text, and httpx quotes the URL it was
-- posting to — the webhook URL, token included — which the read-only ops console then displays.
-- The drainer now redacts it before storing; this strips it from the rows already written. The
-- rest of the message after the URL is only httpx's pointer to the HTTP status reference.
UPDATE alerts_outbox
   SET last_error = substr(last_error, 1, instr(last_error, 'https://discord.com/api/webhooks/') - 1)
                    || '<discord webhook URL redacted>'
 WHERE instr(last_error, 'https://discord.com/api/webhooks/') > 0;

UPDATE alerts_outbox
   SET last_error = substr(last_error, 1, instr(last_error, 'https://discordapp.com/api/webhooks/') - 1)
                    || '<discord webhook URL redacted>'
 WHERE instr(last_error, 'https://discordapp.com/api/webhooks/') > 0;
