"""Tests for the common-mode theme catalog stage (calibration, PR1): the reduce (extract
the catalog), its content-addressed versioning, storage, and the build pass.

The LLM is a fake model returning a scripted tool-call catalog, so no network or Anthropic
config is needed. The DB is a tmp SQLite with migrations applied.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, text

from filings_orchestrator.change_detection import (
    BlockChange,
    CatalogTheme,
    DiffResult,
    DisclosureCatalog,
    MaterialityVerdict,
    RiskChangeCategory,
    RiskChangeDirection,
    RiskFactorBlock,
    canonical_themes,
    catalog_content_hash,
    catalog_extractor_version,
    catalog_version,
    extract_catalog,
)
from filings_orchestrator.cli.build_catalog import build_catalog_pass
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    insert_change_verdict,
    insert_disclosure_catalog,
    insert_filing_diff,
    insert_periodic_filing,
    latest_catalog_version,
    load_catalog_themes,
    load_material_change_digest,
    select_changes_needing_verdict,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
_MODEL = "test-embed-model"
_SECTION = "risk_factors"
_JUDGE_V = "test-judge-v1"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_engine(str(tmp_path / "filings.db"))
    apply_migrations(eng, migrations_dir=MIGRATIONS_DIR)
    return eng


# --- extract + versioning ---


class _CatalogResponse:
    def __init__(self, args: dict[str, Any]) -> None:
        self.tool_calls = [{"name": "submit_catalog", "args": args, "id": "t"}]
        self.usage_metadata: dict[str, Any] = {}


class _FakeCatalogModel:
    """Returns the same catalog on every invoke; records the last prompt."""

    def __init__(self, result: DisclosureCatalog) -> None:
        self._result = result
        self.last_user: str | None = None

    def invoke(self, messages: list[Any]) -> _CatalogResponse:
        self.last_user = messages[-1].content
        return _CatalogResponse(self._result.model_dump(mode="json"))


class _NoToolModel:
    def invoke(self, messages: list[Any]) -> Any:
        return SimpleNamespace(tool_calls=[], usage_metadata={})


def _catalog(*themes: tuple[str, str, int]) -> DisclosureCatalog:
    return DisclosureCatalog(
        themes=[CatalogTheme(theme_slug=s, archetype=a, prevalence=p) for s, a, p in themes]
    )


def test_extract_parses_catalog_and_sees_digest() -> None:
    model = _FakeCatalogModel(_catalog(("tariffs_trade_policy", "Tariffs raise costs.", 40)))
    result = extract_catalog(model, digest="ACME | tariffs | new tariff exposure", model_name="m")
    assert [t.theme_slug for t in result.themes] == ["tariffs_trade_policy"]
    assert model.last_user is not None and "ACME | tariffs" in model.last_user


def test_extract_raises_without_tool_call() -> None:
    with pytest.raises(RuntimeError):
        extract_catalog(_NoToolModel(), digest="x", model_name="m")


def test_theme_slug_and_prevalence_coerced() -> None:
    theme = CatalogTheme(theme_slug="Tariffs / Trade Policy", archetype="a", prevalence="40")  # type: ignore[arg-type]
    assert theme.theme_slug == "tariffs_trade_policy"
    assert theme.prevalence == 40
    # non-numeric prevalence degrades to 0 rather than failing the reduce
    assert CatalogTheme(theme_slug="x", archetype="a", prevalence="lots").prevalence == 0  # type: ignore[arg-type]


def test_malformed_themes_field_coerces_to_empty() -> None:
    assert DisclosureCatalog(themes="not a list").themes == []  # type: ignore[arg-type]


def test_canonical_themes_dedupes_and_sorts() -> None:
    canon = canonical_themes(
        [
            CatalogTheme(theme_slug="rates", archetype="first", prevalence=1),
            CatalogTheme(theme_slug="tariffs", archetype="t", prevalence=2),
            CatalogTheme(theme_slug="rates", archetype="dup dropped", prevalence=9),
            CatalogTheme(theme_slug="", archetype="empty dropped", prevalence=1),
        ]
    )
    assert [t.theme_slug for t in canon] == ["rates", "tariffs"]  # sorted, deduped, empties gone
    assert canon[0].archetype == "first"  # first-wins on duplicate slug


def test_extractor_version_stable_and_names_model() -> None:
    v = catalog_extractor_version("claude-x")
    assert v == catalog_extractor_version("claude-x")
    assert v.startswith("claude-x+catalog-")


def test_catalog_version_tracks_themes_not_prevalence() -> None:
    # A real content change (archetype) yields a new version.
    a = _catalog(("tariffs", "a", 1)).themes
    b = _catalog(("tariffs", "b", 1)).themes
    assert catalog_version(a, "m") != catalog_version(b, "m")
    # Prevalence is soft metadata, NOT identity: same slug+archetype, different count -> SAME
    # version (so a cosmetic count wobble does not churn the catalog / re-classify downstream).
    p1 = _catalog(("tariffs", "a", 1)).themes
    p2 = _catalog(("tariffs", "a", 99)).themes
    assert catalog_version(p1, "m") == catalog_version(p2, "m")
    # order-independent: same content in a different order is the SAME version
    reordered = _catalog(("rates", "r", 1), ("tariffs", "t", 1)).themes
    forward = _catalog(("tariffs", "t", 1), ("rates", "r", 1)).themes
    assert catalog_version(reordered, "m") == catalog_version(forward, "m")
    assert catalog_content_hash(reordered) == catalog_content_hash(forward)


def test_extract_seeds_current_catalog_for_verbatim_carry_forward() -> None:
    model = _FakeCatalogModel(_catalog(("tariffs_trade_policy", "Tariffs raise costs.", 40)))
    extract_catalog(
        model,
        digest="ACME | tariffs | x",
        model_name="m",
        current_themes=[("debt_leverage_refinancing", "Higher rates raise refinancing risk.")],
    )
    assert model.last_user is not None
    assert "EXISTING CATALOG" in model.last_user
    assert "debt_leverage_refinancing" in model.last_user


# --- persistence: digest, cut, idempotency, latest ---


def _seed_company(
    engine: Engine,
    *,
    cik: str,
    company: str,
    prior: str,
    current: str,
    verdicts: list[tuple[RiskChangeCategory, str, str, bool]],
) -> None:
    """Seed one company's filing pair with a diff and one verdict per entry
    (category, direction, explanation, is_material), judging its pending changes."""
    n = len(verdicts)
    for accession, period, fy in ((prior, "2024-12-31", 2024), (current, "2025-12-31", 2025)):
        insert_periodic_filing(
            engine,
            accession_number=accession,
            cik=cik,
            company_name=company,
            form="10-K",
            filed_at="2026-01-01",
            period_of_report=period,
            fiscal_year=fy,
            parsed=True,
            blocks=[
                RiskFactorBlock(
                    index=i, heading="H", text=f"{accession} {i}", block_hash=f"{cik}{accession}{i}"
                )
                for i in range(n)
            ],
            ingested_at="t",
        )
    insert_filing_diff(
        engine,
        accession_number=current,
        prior_accession_number=prior,
        section=_SECTION,
        model_id=_MODEL,
        result=DiffResult(
            changes=[BlockChange("changed", i, i, 0.8) for i in range(n)],
            added=0,
            changed=n,
            carried=0,
            dropped=0,
        ),
        computed_at="t",
    )
    pending = select_changes_needing_verdict(engine, _JUDGE_V, limit=100)
    mine = [c for c in pending if c.accession_number == current]
    for change, (category, direction, explanation, material) in zip(mine, verdicts, strict=True):
        insert_change_verdict(
            engine,
            change=change,
            judge_version=_JUDGE_V,
            verdict=MaterialityVerdict(
                is_material=material,
                confidence=0.9,
                category=category,
                direction=RiskChangeDirection(direction),
                explanation=explanation,
            ),
            needs_review=False,
            judged_at="t",
        )


def test_digest_includes_only_material_changes_with_company(engine: Engine) -> None:
    _seed_company(
        engine,
        cik="1",
        company="ACME",
        prior="acme-p",
        current="acme-c",
        verdicts=[
            (RiskChangeCategory.RESTRUCTURING_WORKFORCE, "worse", "layoffs announced", True),
            (RiskChangeCategory.LIQUIDITY_GOING_CONCERN, "neutral", "reworded boilerplate", False),
        ],
    )
    digest = load_material_change_digest(engine, judge_version=_JUDGE_V)
    assert len(digest) == 1  # the immaterial one is excluded
    assert digest[0].company == "ACME"
    assert digest[0].explanation == "layoffs announced"


def test_digest_orders_by_company(engine: Engine) -> None:
    _seed_company(
        engine,
        cik="2",
        company="ZETA",
        prior="zeta-p",
        current="zeta-c",
        verdicts=[(RiskChangeCategory.RESTRUCTURING_WORKFORCE, "worse", "z change", True)],
    )
    _seed_company(
        engine,
        cik="1",
        company="ALPHA",
        prior="alpha-p",
        current="alpha-c",
        verdicts=[(RiskChangeCategory.RESTRUCTURING_WORKFORCE, "worse", "a change", True)],
    )
    digest = load_material_change_digest(engine, judge_version=_JUDGE_V)
    assert [r.company for r in digest] == ["ALPHA", "ZETA"]


def test_insert_catalog_round_trip_and_idempotent(engine: Engine) -> None:
    themes = [
        ("tariffs_trade_policy", "Tariffs raise costs.", 40),
        ("rate_refinancing", "Rates hurt.", 25),
    ]
    version = "m+catalog-abcd1234-deadbeef"
    for _ in range(2):  # second call must be a no-op (same version)
        insert_disclosure_catalog(
            engine,
            catalog_version=version,
            model_id="m",
            corpus_label="all",
            content_hash="deadbeef",
            themes=themes,
            cut_at="t",
        )
    with engine.begin() as conn:
        vcount = conn.execute(text("SELECT COUNT(*) FROM disclosure_catalog_versions")).scalar()
        tcount = conn.execute(text("SELECT COUNT(*) FROM disclosure_catalog_themes")).scalar()
    assert vcount == 1 and tcount == 2  # no duplicate rows on re-run
    read = load_catalog_themes(engine, version)
    assert [t.theme_slug for t in read] == ["rate_refinancing", "tariffs_trade_policy"]
    assert read[1].prevalence == 40


def test_latest_catalog_version_returns_most_recent(engine: Engine) -> None:
    assert latest_catalog_version(engine) is None
    insert_disclosure_catalog(
        engine,
        catalog_version="v1",
        model_id="m",
        corpus_label="all",
        content_hash="h1",
        themes=[("a", "a", 1)],
        cut_at="2026-01-01T00:00:00+00:00",
    )
    insert_disclosure_catalog(
        engine,
        catalog_version="v2",
        model_id="m",
        corpus_label="all",
        content_hash="h2",
        themes=[("b", "b", 1)],
        cut_at="2026-02-01T00:00:00+00:00",
    )
    assert latest_catalog_version(engine) == "v2"


# --- build pass ---


def test_build_pass_cuts_a_catalog_from_material_changes(engine: Engine) -> None:
    _seed_company(
        engine,
        cik="1",
        company="ACME",
        prior="acme-p",
        current="acme-c",
        verdicts=[(RiskChangeCategory.RESTRUCTURING_WORKFORCE, "worse", "tariff cost", True)],
    )
    model = _FakeCatalogModel(_catalog(("tariffs_trade_policy", "Tariffs raise costs.", 1)))
    counts = build_catalog_pass(
        engine, model, model_name="m", judge_ver=_JUDGE_V, corpus_label="all"
    )
    assert counts["cut"] is True and counts["themes"] == 1 and counts["material_changes"] == 1
    version = latest_catalog_version(engine)
    assert version is not None and version == counts["catalog_version"]
    assert [t.theme_slug for t in load_catalog_themes(engine, version)] == ["tariffs_trade_policy"]


def test_build_pass_no_material_changes_is_a_noop(engine: Engine) -> None:
    model = _FakeCatalogModel(_catalog(("x", "x", 1)))
    counts = build_catalog_pass(
        engine, model, model_name="m", judge_ver=_JUDGE_V, corpus_label="all"
    )
    assert counts["cut"] is False and counts["material_changes"] == 0
    assert latest_catalog_version(engine) is None


def test_build_pass_empty_catalog_is_not_cut(engine: Engine) -> None:
    _seed_company(
        engine,
        cik="1",
        company="ACME",
        prior="acme-p",
        current="acme-c",
        verdicts=[(RiskChangeCategory.RESTRUCTURING_WORKFORCE, "worse", "something", True)],
    )
    model = _FakeCatalogModel(DisclosureCatalog(themes=[]))  # model returned nothing usable
    counts = build_catalog_pass(
        engine, model, model_name="m", judge_ver=_JUDGE_V, corpus_label="all"
    )
    assert counts["cut"] is False and counts["themes"] == 0
    assert latest_catalog_version(engine) is None
