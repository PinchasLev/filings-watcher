"""EDGAR 8-K Atom feed — near-real-time ingest source (ADR 0029).

The Atom feed at:

    https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom&count=N

is a snapshot of the N most recent 8-K submissions, refreshed within minutes
of each submission. ADR 0029 chose this over higher-frequency polling of the
daily-index file because the daily-index file does not exist intraday — the
latency floor of any daily-index-based path is EDGAR's once-per-day
publication, not the polling cadence.

Items are not extracted here. The body parser in `fetch_filing_document`
remains the authoritative source for Item numbers across both ingest paths
(see ADR 0029's note on Atom's metadata sufficiency vs ADR 0006).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from pydantic import BaseModel

from filings_orchestrator.edgar.client import EdgarClient

_ATOM_FEED_URL_TEMPLATE = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type={form}&output=atom&count={count}"
)

_ATOM_NS = "http://www.w3.org/2005/Atom"

# Title format: "<form> - <company> (<cik>) (<role>)"
# e.g. "8-K - Hillman Solutions Corp. (0001822492) (Filer)"
# Anchored on the trailing 10-digit-CIK paren and role paren so company
# names containing hyphens or commas parse correctly.
_TITLE_RE = re.compile(r"^(?P<form>\S+) - (?P<company>.+) \((?P<cik>\d{10})\) \(\w+\)\s*$")

# Accession URN format: urn:tag:sec.gov,2008:accession-number=NNNNNNNNNN-NN-NNNNNN
_ACCESSION_FROM_ID_RE = re.compile(r"accession-number=(\d{10}-\d{2}-\d{6})\s*$")


class AtomEntry(BaseModel):
    """One entry from the EDGAR `getcurrent` Atom feed.

    Field shape matches what the shared filing resolver needs. `updated_at`
    preserves the full ISO 8601 filing timestamp (timezone-offset included)
    distinct from any date-only derivation, so future event emission can
    carry sub-day filing-time granularity without re-parsing the feed.
    """

    cik: str  # 10-digit zero-padded
    company_name: str
    form: str
    accession_number: str
    updated_at: str  # ISO 8601 with offset, e.g. "2026-06-05T09:05:09-04:00"


def atom_feed_url(form: str = "8-K", count: int = 100) -> str:
    """Compose the Atom feed URL for the given form and entry count.

    `count` is the maximum entries returned (EDGAR's default is 40). ADR 0029
    pairs count=100 with a 30s poll cadence; that gives comfortable headroom
    against the rate at which entries roll off the snapshot tail at observed
    8-K volume.
    """
    return _ATOM_FEED_URL_TEMPLATE.format(form=form, count=count)


def fetch_atom_feed(client: EdgarClient, form: str = "8-K", count: int = 100) -> str:
    """Fetch the raw Atom XML for the given form."""
    return client.get_text(atom_feed_url(form=form, count=count))


def parse_atom_feed(xml_text: str) -> list[AtomEntry]:
    """Parse the EDGAR `getcurrent` Atom XML into typed entries.

    Per-entry: accession from the `<id>` URN, CIK + company from the title
    (which uses a consistent `<form> - <company> (<10-digit-CIK>) (<role>)`
    shape), form from `<category term=...>` (falling back to the title's
    first token), and filing timestamp verbatim from `<updated>`.

    Entries that don't match the expected shape (missing required field,
    unparseable title, missing accession URN) are skipped silently — one
    malformed entry must not poison the whole tick. The parser does not
    enforce a form-type filter; the URL filters server-side.
    """
    # httpx already decoded the body using the declared ISO-8859-1 encoding,
    # so the in-memory str's bytes no longer match the declaration. Strip
    # the declaration to avoid a re-decoding mismatch in the XML parser.
    body = xml_text.lstrip()
    if body.startswith("<?xml"):
        body = body[body.index("?>") + 2 :].lstrip()
    root = ET.fromstring(body)

    out: list[AtomEntry] = []
    for entry_el in root.findall(f"{{{_ATOM_NS}}}entry"):
        parsed = _parse_entry(entry_el)
        if parsed is not None:
            out.append(parsed)
    return out


def filter_form(entries: list[AtomEntry], form: str) -> list[AtomEntry]:
    """Return entries whose `form` field equals `form` exactly.

    Exact match: `8-K/A` amendments are a separate form and must not be
    picked up by a plain `8-K` filter (mirrors `daily_index.filter_form`).
    """
    return [e for e in entries if e.form == form]


def _parse_entry(entry_el: ET.Element) -> AtomEntry | None:
    title_el = entry_el.find(f"{{{_ATOM_NS}}}title")
    id_el = entry_el.find(f"{{{_ATOM_NS}}}id")
    updated_el = entry_el.find(f"{{{_ATOM_NS}}}updated")
    category_el = entry_el.find(f"{{{_ATOM_NS}}}category")

    if title_el is None or id_el is None or updated_el is None:
        return None
    if title_el.text is None or id_el.text is None or updated_el.text is None:
        return None

    title_match = _TITLE_RE.match(title_el.text.strip())
    if title_match is None:
        return None

    accession_match = _ACCESSION_FROM_ID_RE.search(id_el.text.strip())
    if accession_match is None:
        return None

    if category_el is not None and (term := category_el.get("term")):
        form = term
    else:
        form = title_match.group("form")

    return AtomEntry(
        cik=title_match.group("cik"),
        company_name=title_match.group("company").strip(),
        form=form,
        accession_number=accession_match.group(1),
        updated_at=updated_el.text.strip(),
    )
