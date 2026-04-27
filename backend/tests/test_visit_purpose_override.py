"""Tests for the visit-purpose ICD override.

Per Solventum v3.18 EAPG methodology, all standalone E/M codes (99202–
99215, etc.) map to type=Incidental in the HCPCS crosswalk — they are
placeholders. The actual EAPG for an outpatient medical visit comes from
the principal diagnosis. The engine substitutes the dx-derived EAPG when
the HCPCS resolves to an Incidental placeholder AND a principal_diagnosis
is present.

Without this fix, every standalone E/M visit packages to $0.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from backend.db.database import (
    ApgBaseRate, ApgWeight, Base, HcpcsToEapg, Icd10ToEapg,
    ProviderConfig, async_session, engine,
)
from backend.engines.apg_engine import APGEngine
from backend.models.schemas import (
    EapgType, FileType, ParsedClaim, Region, ServiceLine,
)


# Sentinels chosen so they can't collide with real Solventum data.
EM_CODE = "ZZEM01"
DX_CODE = "ZZDX01"
PLACEHOLDER_EAPG = 90901   # the "Incidental placeholder" EAPG for the E/M
DX_DRIVEN_EAPG = 90902     # real medical-visit EAPG resolved via dx
DOS = date(2024, 6, 15)


@pytest_asyncio.fixture()
async def seeded_session():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as s:
        # Clean any leftover sentinels
        await s.execute(delete(HcpcsToEapg).where(HcpcsToEapg.hcpcs == EM_CODE))
        await s.execute(delete(Icd10ToEapg).where(Icd10ToEapg.dx_code == DX_CODE))
        await s.execute(delete(ApgWeight).where(
            ApgWeight.apg.in_([PLACEHOLDER_EAPG, DX_DRIVEN_EAPG])
        ))

        # E/M HCPCS → Incidental placeholder EAPG (mirrors how Solventum
        # publishes 99213 → 491 / type=Incidental)
        s.add(HcpcsToEapg(
            hcpcs=EM_CODE, eapg=PLACEHOLDER_EAPG,
            eapg_desc="Incidental placeholder",
            eapg_type="Incidental", eapg_category="EM",
            quarter_effective_date=date(2020, 1, 1),
            quarter_end_date=date(9999, 12, 31),
        ))
        # ICD → real medical-visit EAPG
        s.add(Icd10ToEapg(
            dx_code=DX_CODE, description="Test DX",
            gender="0", eapg=DX_DRIVEN_EAPG,
            eapg_desc="Real Medical Visit EAPG",
            eapg_type="Medical Visit",
            eapg_category="MV",
            effective_date=None, end_date=None,
        ))
        # Weight 0 for the placeholder (matches NYS DOH reality)
        s.add(ApgWeight(
            apg=PLACEHOLDER_EAPG, apg_description="Placeholder",
            effective_date=date(2020, 1, 1),
            weight=Decimal("0"), is_final_rate=False,
        ))
        # Real weight for the dx-driven EAPG
        s.add(ApgWeight(
            apg=DX_DRIVEN_EAPG, apg_description="Real EAPG",
            effective_date=date(2020, 1, 1),
            weight=Decimal("0.5000"), is_final_rate=False,
        ))
        # Make sure a Clinic*/Downstate base rate exists
        existing = await s.execute(
            select(ApgBaseRate).where(
                ApgBaseRate.source == "dtc",
                ApgBaseRate.peer_group == "Clinic*",
                ApgBaseRate.region == "Downstate",
                ApgBaseRate.effective_date <= DOS,
            ).limit(1)
        )
        seeded_base = False
        if existing.scalar_one_or_none() is None:
            s.add(ApgBaseRate(
                source="dtc", peer_group="Clinic*", region="Downstate",
                effective_date=date(2015, 4, 1), rate=Decimal("100.00"),
            ))
            seeded_base = True
        await s.commit()

    async with async_session() as s:
        yield s

    async with async_session() as s:
        await s.execute(delete(HcpcsToEapg).where(HcpcsToEapg.hcpcs == EM_CODE))
        await s.execute(delete(Icd10ToEapg).where(Icd10ToEapg.dx_code == DX_CODE))
        await s.execute(delete(ApgWeight).where(
            ApgWeight.apg.in_([PLACEHOLDER_EAPG, DX_DRIVEN_EAPG])
        ))
        if seeded_base:
            await s.execute(delete(ApgBaseRate).where(
                ApgBaseRate.source == "dtc",
                ApgBaseRate.peer_group == "Clinic*",
                ApgBaseRate.region == "Downstate",
                ApgBaseRate.effective_date == date(2015, 4, 1),
                ApgBaseRate.rate == Decimal("100.00"),
            ))
        await s.commit()


def _provider() -> ProviderConfig:
    return ProviderConfig(
        is_active=True,
        provider_name="Visit-Purpose Test Clinic",
        npi="0000000099",
        county_code=None,
        region="Downstate",
        peer_group="Clinic*",
        provider_type="dtc",
        capital_addon_eligible=False,
    )


def _claim(*, principal_dx: str | None) -> ParsedClaim:
    return ParsedClaim(
        file_type=FileType.ERA_835I,
        claim_id=f"VP-TEST-{principal_dx or 'NONE'}",
        date_of_service=DOS,
        paid_amount=Decimal("0"),
        principal_diagnosis=principal_dx,
        service_lines=[
            ServiceLine(
                line_seq=1, procedure_code=EM_CODE, modifiers=[],
                units=1, paid_amount=Decimal("0"),
                allowed_amount=Decimal("0"),
            )
        ],
    )


@pytest.mark.asyncio
async def test_em_with_dx_uses_visit_purpose_eapg(seeded_session):
    """E/M placeholder + matching dx → engine swaps in the dx-driven EAPG
    and the line pays at the real weight, not 0."""
    engine_ = APGEngine()
    result = await engine_.calculate(
        seeded_session, _claim(principal_dx=DX_CODE), _provider(),
    )
    ld = result.line_details[0]
    assert ld.eapg == DX_DRIVEN_EAPG, (
        f"Expected dx-driven EAPG {DX_DRIVEN_EAPG}, got {ld.eapg}"
    )
    assert ld.eapg_type == EapgType.MEDICAL_VISIT
    assert ld.weight == Decimal("0.5000")
    expected = (Decimal("0.5000") * result.base_rate_applied).quantize(Decimal("0.01"))
    assert ld.expected_payment == expected
    assert ld.expected_payment > 0
    assert any(
        "Visit-purpose adjustment" in n for n in ld.notes
    ), f"Expected a visit-purpose note, got {ld.notes}"


@pytest.mark.asyncio
async def test_em_without_dx_remains_packaged(seeded_session):
    """No principal dx → the override doesn't fire; the line packages to $0
    as the placeholder EAPG would dictate. (This is the pre-fix baseline.)"""
    engine_ = APGEngine()
    result = await engine_.calculate(
        seeded_session, _claim(principal_dx=None), _provider(),
    )
    ld = result.line_details[0]
    assert ld.eapg == PLACEHOLDER_EAPG
    assert ld.eapg_type == EapgType.INCIDENTAL
    assert ld.expected_payment == Decimal("0.00")
    assert ld.packaged is True


@pytest.mark.asyncio
async def test_em_with_unknown_dx_remains_packaged(seeded_session):
    """User supplies a dx the system can't resolve → no override; line stays
    packaged. The dx is reported in the notes via the existing ICD fallback
    path (which DOESN'T fire here either since we already have an EAPG)."""
    engine_ = APGEngine()
    result = await engine_.calculate(
        seeded_session, _claim(principal_dx="ZZZNOTFOUND"), _provider(),
    )
    ld = result.line_details[0]
    assert ld.eapg == PLACEHOLDER_EAPG     # still the placeholder
    assert ld.expected_payment == Decimal("0.00")
    assert ld.packaged is True
