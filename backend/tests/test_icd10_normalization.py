"""Tests for ICD-10 dx_code normalization across storage + lookup paths.

The regression this guards against: user types `I10.0` into the Rate
Calculator, the crosswalk has `I100` (dot-free, per Solventum's canonical
form), so the ICD-10 → EAPG fallback silently misses. The fix is to
normalize at every boundary via `normalize_dx_code`.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from backend.db.database import Base, Icd10ToEapg, async_session, engine
from backend.engines.apg_engine import APGEngine, normalize_dx_code


# ---------------------------------------------------------------------------
# normalize_dx_code — pure function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("I10",        "I10"),
        ("I10.0",      "I100"),
        ("i10.0",      "I100"),
        ("  I10.0  ",  "I100"),
        ("I48.91",     "I4891"),
        ("A00.0",      "A000"),
        ("Z99.89",     "Z9989"),
        # Already canonical — no change
        ("A000",       "A000"),
        # Empty / whitespace / None
        (None,         None),
        ("",           None),
        ("   ",        None),
        # Dots-only garbage normalizes to empty → None
        ("...",        None),
    ],
)
def test_normalize_dx_code(raw, expected):
    assert normalize_dx_code(raw) == expected


# ---------------------------------------------------------------------------
# Engine lookup — stored canonical form + user-typed dotted form both hit
# ---------------------------------------------------------------------------


TEST_DX = "ZZZ99"     # sentinel that won't clash with real Solventum data


@pytest_asyncio.fixture()
async def seeded_icd():
    """Seed one ICD row in the canonical (dot-free) form and clean up after."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as s:
        # Remove any prior sentinel row from a failed previous run
        await s.execute(delete(Icd10ToEapg).where(Icd10ToEapg.dx_code == TEST_DX))
        s.add(Icd10ToEapg(
            dx_code=TEST_DX, description="Synthetic test DX",
            gender="0", eapg=99999, eapg_desc="Test EAPG",
            eapg_type="Medical Visit", eapg_category="Test",
            eapg_service_line=None,
            effective_date=None, end_date=None,
        ))
        await s.commit()

    async with async_session() as s:
        yield s

    async with async_session() as s:
        await s.execute(delete(Icd10ToEapg).where(Icd10ToEapg.dx_code == TEST_DX))
        await s.commit()


@pytest.mark.asyncio
async def test_lookup_matches_canonical_form(seeded_icd):
    apg = APGEngine()
    row = await apg.lookup_icd10_eapg(seeded_icd, "ZZZ99", date(2024, 6, 1))
    assert row is not None and row.eapg == 99999


@pytest.mark.asyncio
async def test_lookup_matches_dotted_user_input(seeded_icd):
    """User typing 'zzz.99' into the Rate Calculator should still hit the
    canonical row stored as 'ZZZ99'."""
    apg = APGEngine()
    row = await apg.lookup_icd10_eapg(seeded_icd, "zzz.99", date(2024, 6, 1))
    assert row is not None and row.eapg == 99999


@pytest.mark.asyncio
async def test_lookup_matches_whitespace_and_case(seeded_icd):
    apg = APGEngine()
    row = await apg.lookup_icd10_eapg(seeded_icd, "  zZz99  ", date(2024, 6, 1))
    assert row is not None and row.eapg == 99999


@pytest.mark.asyncio
async def test_lookup_empty_returns_none(seeded_icd):
    apg = APGEngine()
    assert await apg.lookup_icd10_eapg(seeded_icd, "", date(2024, 6, 1)) is None
    assert await apg.lookup_icd10_eapg(seeded_icd, None, date(2024, 6, 1)) is None
