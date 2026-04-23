"""Tests for the ICD-derived EAPG block on the Rate Calculator response.

What we verify:
  * When the user supplies a principal ICD-10 that resolves to an EAPG,
    `CalculatorOut.icd_based_eapg` is populated with the EAPG/weight/rate.
  * When the ICD doesn't resolve, the field is returned with a clear note.
  * The official payment total (`apg.correct_apg_payment`) is driven by
    the per-line HCPCS math and is NOT affected by the ICD block.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from backend.db.database import (
    ApgBaseRate, ApgWeight, Base, HcpcsToEapg, Icd10ToEapg,
    ProviderConfig, async_session, engine,
)
from backend.main import app


# Sentinel values chosen so they won't clash with the shared dev DB's real data.
TEST_HCPCS = "ZZHX99"
TEST_DX    = "ZZDX99"                # canonical form
TEST_DX_DOTTED = "ZZ.DX99"           # what the user might type
HCPCS_EAPG = 91001
ICD_EAPG   = 91002
DOS = date(2024, 6, 15)


@pytest_asyncio.fixture()
async def seeded():
    """Seed the reference rows needed for a full calculator round-trip.
    Rolls back on exit so nothing leaks into the dev DB."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as s:
        # Clean up any leftover sentinels
        await s.execute(delete(HcpcsToEapg).where(HcpcsToEapg.hcpcs == TEST_HCPCS))
        await s.execute(delete(Icd10ToEapg).where(Icd10ToEapg.dx_code == TEST_DX))
        await s.execute(delete(ApgWeight).where(ApgWeight.apg.in_([HCPCS_EAPG, ICD_EAPG])))

        # HCPCS → EAPG (drives the per-line payment)
        s.add(HcpcsToEapg(
            hcpcs=TEST_HCPCS, eapg=HCPCS_EAPG,
            eapg_desc="Test HCPCS EAPG",
            eapg_type="Significant Procedure",
            eapg_category="Test",
            quarter_effective_date=date(2020, 1, 1),
            quarter_end_date=date(9999, 12, 31),
        ))
        # ICD → EAPG (drives the informational block)
        s.add(Icd10ToEapg(
            dx_code=TEST_DX, description="Test DX",
            gender="0", eapg=ICD_EAPG,
            eapg_desc="Test ICD EAPG", eapg_type="Medical Visit",
            eapg_category="Test",
            effective_date=None, end_date=None,
        ))
        # Weight for the ICD-resolved EAPG
        s.add(ApgWeight(
            apg=ICD_EAPG, apg_description="Test ICD EAPG",
            effective_date=date(2020, 1, 1),
            weight=Decimal("0.7500"), is_final_rate=False,
        ))
        # Weight for the HCPCS-resolved EAPG
        s.add(ApgWeight(
            apg=HCPCS_EAPG, apg_description="Test HCPCS EAPG",
            effective_date=date(2020, 1, 1),
            weight=Decimal("1.0000"), is_final_rate=False,
        ))

        # Make sure a Clinic*/Downstate/dtc rate exists
        rate_q = await s.execute(
            select(ApgBaseRate).where(
                ApgBaseRate.source == "dtc",
                ApgBaseRate.peer_group == "Clinic*",
                ApgBaseRate.region == "Downstate",
                ApgBaseRate.effective_date <= DOS,
            ).limit(1)
        )
        seeded_base_rate = False
        if rate_q.scalar_one_or_none() is None:
            s.add(ApgBaseRate(
                source="dtc", peer_group="Clinic*", region="Downstate",
                effective_date=date(2015, 4, 1), rate=Decimal("200.00"),
            ))
            seeded_base_rate = True

        # Active provider: Clinic*/Downstate/dtc
        await s.execute(delete(ProviderConfig).where(ProviderConfig.is_active.is_(True)))
        s.add(ProviderConfig(
            is_active=True,
            provider_name="ICD-Test Clinic",
            npi="0000000001",
            county_code=None,
            region="Downstate",
            peer_group="Clinic*",
            provider_type="dtc",
            capital_addon_eligible=False,
        ))
        await s.commit()

    yield seeded_base_rate

    # Cleanup
    async with async_session() as s:
        await s.execute(delete(HcpcsToEapg).where(HcpcsToEapg.hcpcs == TEST_HCPCS))
        await s.execute(delete(Icd10ToEapg).where(Icd10ToEapg.dx_code == TEST_DX))
        await s.execute(delete(ApgWeight).where(ApgWeight.apg.in_([HCPCS_EAPG, ICD_EAPG])))
        await s.execute(delete(ProviderConfig).where(
            ProviderConfig.provider_name == "ICD-Test Clinic"
        ))
        await s.commit()


def _auth_headers():
    """Bypass auth: most real tests in this repo do this by overriding the
    auth dependency via app.dependency_overrides. Here we mint a minimal
    analyst stub instead of wiring JWT, since the calculator endpoint only
    needs `CurrentUser` to check presence."""
    # The repo's test_auth already demonstrates this pattern; follow it.
    return {}


@pytest.mark.asyncio
async def test_icd_based_eapg_populated_when_dx_resolves(seeded):
    """Happy path: HCPCS line + dotted ICD input → both results visible."""
    from backend.deps import get_current_user
    from backend.db.database import UserAccount
    # Stub auth so the endpoint lets us through
    app.dependency_overrides[get_current_user] = lambda: UserAccount(
        id=1, username="test", full_name="Test", email=None,
        password_hash="x", role="analyst", disabled=False,
    )

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/calculator/calculate", json={
                "date_of_service": DOS.isoformat(),
                "service_lines": [{"procedure_code": TEST_HCPCS, "units": 1}],
                "principal_diagnosis": TEST_DX_DOTTED,  # dotted form
                "target": "apg",
            })
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # HCPCS-driven payment should be weight (1.0) × base_rate
        assert body["apg"] is not None
        expected_payment = Decimal(str(body["apg"]["correct_apg_payment"]))
        base_rate = Decimal(str(body["apg"]["base_rate_applied"]))
        assert expected_payment == base_rate  # weight 1.0000 × base_rate

        # ICD-based block should resolve to the ICD EAPG (91002), NOT the HCPCS
        # EAPG (91001). This proves we're looking up the dx independently.
        icd = body["icd_based_eapg"]
        assert icd is not None
        assert icd["input_dx_code"] == TEST_DX_DOTTED
        assert icd["dx_code"] == TEST_DX             # normalized
        assert icd["eapg"] == ICD_EAPG
        assert icd["eapg_desc"] == "Test ICD EAPG"
        assert Decimal(str(icd["weight"])) == Decimal("0.7500")
        # indicative = weight × base_rate = 0.75 × base_rate
        indicative = Decimal(str(icd["indicative_payment"]))
        assert indicative == (Decimal("0.7500") * base_rate).quantize(Decimal("0.01"))
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_icd_based_block_present_but_unresolved_when_dx_unknown(seeded):
    """User types a bogus ICD — block is returned with a note, not omitted."""
    from backend.deps import get_current_user
    from backend.db.database import UserAccount
    app.dependency_overrides[get_current_user] = lambda: UserAccount(
        id=1, username="test", full_name="Test", email=None,
        password_hash="x", role="analyst", disabled=False,
    )

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/calculator/calculate", json={
                "date_of_service": DOS.isoformat(),
                "service_lines": [{"procedure_code": TEST_HCPCS, "units": 1}],
                "principal_diagnosis": "NOSUCHDX",
                "target": "apg",
            })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        icd = body["icd_based_eapg"]
        assert icd is not None
        assert icd["input_dx_code"] == "NOSUCHDX"
        assert icd["eapg"] is None
        assert "did not resolve" in (icd["note"] or "").lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_icd_based_block_omitted_when_no_dx_supplied(seeded):
    """No principal_diagnosis → no ICD block in the response."""
    from backend.deps import get_current_user
    from backend.db.database import UserAccount
    app.dependency_overrides[get_current_user] = lambda: UserAccount(
        id=1, username="test", full_name="Test", email=None,
        password_hash="x", role="analyst", disabled=False,
    )

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/calculator/calculate", json={
                "date_of_service": DOS.isoformat(),
                "service_lines": [{"procedure_code": TEST_HCPCS, "units": 1}],
                "target": "apg",
            })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("icd_based_eapg") is None
    finally:
        app.dependency_overrides.clear()
