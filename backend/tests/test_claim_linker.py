"""Integration test for 837 ↔ 835 claim linking and APG enrichment.

Scenario:
  1. Upload an 835I where the 835's HCPCS codes alone resolve to Incidental EAPGs
     (so correct_apg_payment = 0).
  2. Upload a matching 837P that carries diagnosis codes for those same claims.
  3. Verify that after upload:
     - Both records now have linked_claim_id_fk pointing at each other.
     - The 835's principal_diagnosis is populated from the 837.
     - The APG engine was re-run with the enriched DX (may or may not change
       the payment depending on whether the DX maps to a payable EAPG — but the
       structure must be correct).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from backend.db.database import (
    HcpcsToEapg,
    ParsedClaim as ORMClaim,
    ProviderConfig,
    ProviderCounty,
    async_session,
    Base,
    engine as db_engine,
)
from backend.engines.claim_linker import link_and_enrich
from backend.main import _persist_parsed_claim, _run_apg_and_persist
from backend.parsers.edi_835i import parse_835i
from backend.parsers.edi_837 import parse_837

FIX_835I = Path(__file__).parent / "fixtures" / "sample_835i.edi"
FIX_837P = Path(__file__).parent / "fixtures" / "sample_837p.edi"


@pytest_asyncio.fixture()
async def session():
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as s:
        cnt = await s.execute(select(func.count()).select_from(HcpcsToEapg))
        if cnt.scalar_one() == 0:
            pytest.skip("Reference data not loaded. Run init_db first.")
        # Clean any prior claims from an earlier test run
        await s.execute(ORMClaim.__table__.delete())
        await s.commit()
        yield s


@pytest_asyncio.fixture()
async def provider(session):
    q = await session.execute(
        select(ProviderCounty).where(ProviderCounty.region == "Downstate").limit(1)
    )
    county = q.scalar_one()
    cfg = ProviderConfig(
        is_active=True, provider_name="Enrichment Test Clinic",
        county_code=county.county_code, region=county.region,
        peer_group="Clinic*", provider_type="dtc", capital_addon_eligible=False,
    )
    session.add(cfg)
    await session.flush()
    yield cfg


@pytest.mark.asyncio
async def test_837_enriches_matching_835_claim(session, provider):
    # --- Step 1: upload 835I (CLM0001, CLM0002, CLM0003) ---
    p_835 = parse_835i(FIX_835I)
    era_pks = []
    all_ids = []
    for dto in p_835.claims:
        orm = await _persist_parsed_claim(session, dto, p_835, batch_id="test-era")
        await _run_apg_and_persist(session, dto, orm, provider)
        era_pks.append(orm.id)
        all_ids.append(dto.claim_id)
    await session.commit()

    # Before 837: principal_diagnosis should be None on all 835 claims
    q = await session.execute(select(ORMClaim).where(ORMClaim.id.in_(era_pks)))
    before = {c.claim_id: c for c in q.scalars().all()}
    for c in before.values():
        assert c.principal_diagnosis is None
        assert c.linked_claim_id_fk is None

    # --- Step 2: upload 837P with CLM0001, CLM0002 ---
    p_837 = parse_837(FIX_837P)
    sub_pks = []
    sub_ids = []
    for dto in p_837.claims:
        orm = await _persist_parsed_claim(session, dto, p_837, batch_id="test-sub")
        sub_pks.append(orm.id)
        sub_ids.append(dto.claim_id)
    await session.commit()

    # --- Step 3: run enrichment ---
    summary = await link_and_enrich(session, sub_ids, provider)
    await session.commit()

    # Two claim IDs overlap (CLM0001 + CLM0002); CLM0003 only on 835
    assert summary["linked_pairs"] == 2
    assert summary["apg_recalcs"] >= 1

    # --- Step 4: verify linkage + dx enrichment on 835 side ---
    q = await session.execute(select(ORMClaim).where(ORMClaim.id.in_(era_pks)))
    after = {c.claim_id: c for c in q.scalars().all()}

    # CLM0001 and CLM0002 were in both files; CLM0003 was 835-only
    assert after["CLM0001"].linked_claim_id_fk is not None
    assert after["CLM0002"].linked_claim_id_fk is not None
    assert after["CLM0003"].linked_claim_id_fk is None

    # 837P fixture has ABK:E119 on CLM0001 → enriched onto 835
    assert after["CLM0001"].principal_diagnosis == "E119"
    # Other diagnoses merged
    assert "I10" in (after["CLM0001"].other_diagnoses or [])

    # CLM0002 has ABK:L82.1 on 837 → enriched
    assert after["CLM0002"].principal_diagnosis == "L82.1"


@pytest.mark.asyncio
async def test_link_is_idempotent(session, provider):
    """Running enrichment a second time should not re-link or double-enrich."""
    p_835 = parse_835i(FIX_835I)
    for dto in p_835.claims:
        orm = await _persist_parsed_claim(session, dto, p_835, batch_id="test-era2")
        await _run_apg_and_persist(session, dto, orm, provider)
    await session.commit()

    p_837 = parse_837(FIX_837P)
    for dto in p_837.claims:
        await _persist_parsed_claim(session, dto, p_837, batch_id="test-sub2")
    await session.commit()

    # First pass
    first = await link_and_enrich(session, ["CLM0001", "CLM0002"], provider)
    await session.commit()
    # Second pass (no new DX to merge, dx is already set)
    second = await link_and_enrich(session, ["CLM0001", "CLM0002"], provider)
    await session.commit()

    assert first["linked_pairs"] == 2
    assert second["linked_pairs"] == 2  # always links (cheap op)
    # apg_recalcs should be 0 on the second pass because nothing was newly enriched
    assert second["apg_recalcs"] == 0
