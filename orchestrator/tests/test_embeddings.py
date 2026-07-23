"""Tests for block embeddings (ADR 0042, PR 3): Voyage client, persistence, reconciler.

Hermetic — a tmp SQLite DB with migrations applied; Voyage HTTP is intercepted by
respx, and the reconciler is exercised with a deterministic fake embedder.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine, text

from filings_orchestrator.change_detection import RiskFactorBlock, VoyageEmbedder
from filings_orchestrator.cli.embed_blocks import embed_pass, main
from filings_orchestrator.persistence import apply_migrations, open_engine
from filings_orchestrator.persistence.repository import (
    UnembeddedBlock,
    insert_block_embeddings,
    insert_periodic_filing,
    select_unembedded_blocks,
)

MIGRATIONS_DIR = (Path(__file__).resolve().parent.parent / "db" / "migrations").resolve()
_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
_MODEL = "test-model"


class _FakeEmbedder:
    """Deterministic 3-dim embedder that records the batches it was called with."""

    model_id = _MODEL

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[float(len(t)), 1.0, 2.0] for t in texts]


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_engine(str(tmp_path / "filings.db"))
    apply_migrations(eng, migrations_dir=MIGRATIONS_DIR)
    return eng


def _seed_blocks(engine: Engine, n: int, accession: str = "0001-26-000010") -> None:
    blocks = [
        RiskFactorBlock(
            index=i, heading=f"H{i}", text=f"risk factor number {i}", block_hash=f"h{i}"
        )
        for i in range(n)
    ]
    insert_periodic_filing(
        engine,
        accession_number=accession,
        cik="0000000123",
        company_name="ACME CORP",
        form="10-K",
        filed_at="2026-03-15",
        period_of_report="2025-12-31",
        fiscal_year=2025,
        parsed=True,
        blocks=blocks,
        ingested_at="2026-03-15T12:00:00+00:00",
    )


def _embedding_count(engine: Engine, model_id: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM filing_block_embeddings WHERE model_id = :m"),
                {"m": model_id},
            ).scalar_one()
        )


# --- Voyage client ---


def test_voyage_embed_posts_documents_and_restores_order() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_VOYAGE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.3, 0.4], "index": 1},
                        {"embedding": [0.1, 0.2], "index": 0},
                    ],
                    "model": _MODEL,
                },
            )
        )
        embedder = VoyageEmbedder("key-abc", _MODEL)
        vectors = embedder.embed(["first", "second"])

    # Out-of-order API items are restored to input order by their index.
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == _MODEL
    assert body["input_type"] == "document"
    assert body["input"] == ["first", "second"]


def test_voyage_embed_empty_makes_no_request() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_VOYAGE_URL)
        assert VoyageEmbedder("key", _MODEL).embed([]) == []
        assert not route.called


# --- persistence ---


def test_select_unembedded_and_insert_round_trip(engine: Engine) -> None:
    _seed_blocks(engine, 2)
    pending = select_unembedded_blocks(engine, _MODEL, limit=10)
    assert len(pending) == 2
    assert all(isinstance(b, UnembeddedBlock) for b in pending)

    insert_block_embeddings(
        engine,
        model_id=_MODEL,
        items=[(pending[0], [1.0, 2.0, 3.0])],
        embedded_at="2026-03-15T12:00:00+00:00",
    )
    # One embedded -> one still pending; dim recorded.
    assert len(select_unembedded_blocks(engine, _MODEL, limit=10)) == 1
    with engine.begin() as conn:
        dim = conn.execute(text("SELECT dim FROM filing_block_embeddings")).scalar_one()
    assert dim == 3


def test_insert_block_embeddings_is_idempotent(engine: Engine) -> None:
    _seed_blocks(engine, 1)
    block = select_unembedded_blocks(engine, _MODEL, limit=1)[0]
    insert_block_embeddings(engine, model_id=_MODEL, items=[(block, [1.0])], embedded_at="t1")
    insert_block_embeddings(engine, model_id=_MODEL, items=[(block, [9.0, 9.0])], embedded_at="t2")
    assert _embedding_count(engine, _MODEL) == 1  # overwrote, did not duplicate
    with engine.begin() as conn:
        row = conn.execute(text("SELECT dim, embedding_json FROM filing_block_embeddings")).one()
    assert row[0] == 2 and json.loads(row[1]) == [9.0, 9.0]


# --- reconciler ---


def test_embed_pass_batches_and_is_resumable(engine: Engine) -> None:
    _seed_blocks(engine, 5)
    fake = _FakeEmbedder()
    counts = embed_pass(engine, fake, batch_size=2, limit=100)
    assert counts == {"embedded": 5, "batches": 3}  # 2 + 2 + 1
    assert [len(b) for b in fake.batches] == [2, 2, 1]
    assert _embedding_count(engine, _MODEL) == 5

    # Second pass has nothing to do — idempotent.
    again = embed_pass(engine, _FakeEmbedder(), batch_size=2, limit=100)
    assert again == {"embedded": 0, "batches": 0}


def test_embed_pass_respects_limit(engine: Engine) -> None:
    _seed_blocks(engine, 5)
    counts = embed_pass(engine, _FakeEmbedder(), batch_size=10, limit=3)
    assert counts["embedded"] == 3
    assert len(select_unembedded_blocks(engine, _MODEL, limit=10)) == 2


# --- CLI wiring ---


def test_main_missing_key_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FILINGS_DB_PATH", str(tmp_path / "filings.db"))
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["embed-blocks"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_main_embeds_via_voyage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "filings.db"
    engine = open_engine(str(db_path))
    apply_migrations(engine, migrations_dir=MIGRATIONS_DIR)
    _seed_blocks(engine, 2)

    monkeypatch.setenv("FILINGS_DB_PATH", str(db_path))
    monkeypatch.setenv("VOYAGE_API_KEY", "key-xyz")
    monkeypatch.setenv("VOYAGE_MODEL", _MODEL)
    monkeypatch.setattr(sys, "argv", ["embed-blocks"])

    with respx.mock(assert_all_called=True) as mock:
        mock.post(_VOYAGE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.1, 0.2], "index": 0},
                        {"embedding": [0.3, 0.4], "index": 1},
                    ],
                    "model": _MODEL,
                },
            )
        )
        main()

    assert _embedding_count(engine, _MODEL) == 2
