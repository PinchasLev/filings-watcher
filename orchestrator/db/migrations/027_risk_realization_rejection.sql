-- A realized verdict passes two code gates before it is surfaced: the quote must be a verbatim
-- span of the realizing 8-K, and the evidence sentence must assert nothing the sources do not
-- support. A verdict failing either is downgraded to not-realized and, until now, its quote and
-- evidence were dropped on the floor — a rejection was indistinguishable in the data from the
-- judge simply finding nothing.
--
-- That blindness has a measured cost. In the first run after the gate counters landed, the judge
-- claimed a materialization twelve times; the quote gate rejected ten of them. Whether those were
-- fabrications the gate correctly caught or sound citations it wrongly refused is not answerable
-- from anything we stored. These columns keep the rejected claim so the question can be settled
-- by looking rather than by re-running the judge and hoping it repeats itself.
--
-- Nothing here reaches the site: the service attaches a materialization only when is_realized is
-- non-zero, and a rejected verdict is stored with is_realized = 0.
ALTER TABLE risk_realizations ADD COLUMN rejected_by TEXT;
ALTER TABLE risk_realizations ADD COLUMN rejected_detail TEXT NOT NULL DEFAULT '';
