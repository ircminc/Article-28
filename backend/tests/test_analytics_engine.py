"""Analytics engine integration tests.

These tests seed a clean database with a handful of claims (835I, 835P),
attach APG results and CAS adjustments, then exercise each method on
AnalyticsEngine. We assert shape + directional correctness rather than exact
penny values — the engine is thin wrapping SQL, so the risk is in the SQL
expressions / filter plumbing, not arithmetic precision.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.db.database import (
    ApgResult as ORMApgResult,
    Base,
    ClaimAdjustment as ORMAdjustment,
    ParsedClaim as ORMClaim,
    ParsedServiceLine as ORMLine,
    async_session,
    engine as db_engine,
)
from backend.engines.analytics_engine import AnalyticsEngine


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def fresh_session():
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as s:
        # Wipe anything prior tests left behind
        await s.execute(delete(ORMApgResult))
        await s.execute(delete(ORMAdjustment))
        await s.execute(delete(ORMLine))
        await s.execute(delete(ORMClaim))
        await s.commit()
        yield s


def _make_claim(**kwargs):
    defaults = dict(
        file_id="fx",
        file_type="835I",
        payer_name="NYS MEDICAID",
        provider_npi="1234567890",
        claim_id="CLMTEST",
        patient_name="Doe, Jane",
        date_of_service=date(2023, 6, 15),
        claim_status="1",
        billed_amount=Decimal("1000.00"),
        allowed_amount=Decimal("700.00"),
        paid_amount=Decimal("500.00"),
        patient_responsibility=Decimal("50.00"),
        claim_filing_indicator="MC",
        principal_diagnosis=None,
        other_diagnoses=[],
    )
    defaults.update(kwargs)
    return ORMClaim(**defaults)


@pytest_asyncio.fixture()
async def seeded(fresh_session):
    s = fresh_session

    # Three 835I claims — one paid above, one underpaid, one denied
    c1 = _make_claim(claim_id="ERA-A", billed_amount=Decimal("500"), paid_amount=Decimal("400"),
                     payer_name="NYS MEDICAID", date_of_service=date(2023, 3, 10))
    c2 = _make_claim(claim_id="ERA-B", billed_amount=Decimal("1500"), paid_amount=Decimal("900"),
                     payer_name="NYS MEDICAID", date_of_service=date(2023, 5, 20))
    c3 = _make_claim(claim_id="ERA-C", billed_amount=Decimal("750"), paid_amount=Decimal("0"),
                     patient_responsibility=Decimal("750"), claim_status="4",
                     payer_name="NYS MEDICAID", date_of_service=date(2023, 7, 1))

    # One 835P (commercial, included in denial/payer aggregates; no APG)
    c4 = _make_claim(
        claim_id="PROF-A", file_type="835P",
        billed_amount=Decimal("300"), paid_amount=Decimal("200"),
        payer_name="AETNA COMMERCIAL", claim_filing_indicator="CI",
        date_of_service=date(2023, 4, 5),
    )

    s.add_all([c1, c2, c3, c4])
    await s.flush()

    # APG results for the 3 835I claims
    s.add(ORMApgResult(
        claim_id_fk=c1.id,
        correct_apg_payment=Decimal("420.00"),
        actual_paid=Decimal("400.00"),
        variance=Decimal("20.00"),            # underpaid $20
        compression_pct=Decimal("4.7619"),
        underpaid=True, overpaid=False,
        base_rate_applied=Decimal("169.02"),
        peer_group="Clinic*", region="Downstate",
        discounting_applied=False, u6_applied=False, capital_applied=False,
        line_details=[
            {"line_seq": 1, "procedure_code": "99213", "eapg": 491,
             "eapg_desc": "MEDICAL VISIT INDICATOR", "eapg_type": "Medical Visit",
             "weight": "2.4850", "expected_payment": "420.00",
             "actual_paid": "400.00", "variance": "20.00"},
        ],
    ))
    s.add(ORMApgResult(
        claim_id_fk=c2.id,
        correct_apg_payment=Decimal("1350.00"),
        actual_paid=Decimal("900.00"),
        variance=Decimal("450.00"),           # underpaid $450
        compression_pct=Decimal("33.3333"),
        underpaid=True, overpaid=False,
        base_rate_applied=Decimal("169.02"),
        peer_group="Clinic*", region="Downstate",
        discounting_applied=True, u6_applied=False, capital_applied=False,
        line_details=[
            {"line_seq": 1, "procedure_code": "11042", "eapg": 321,
             "eapg_desc": "SKIN DEBRIDEMENT", "eapg_type": "Significant Procedure",
             "weight": "7.9887", "expected_payment": "1350.00",
             "actual_paid": "900.00", "variance": "450.00"},
        ],
    ))
    s.add(ORMApgResult(
        claim_id_fk=c3.id,
        correct_apg_payment=Decimal("0.00"),
        actual_paid=Decimal("0.00"),
        variance=Decimal("0.00"),
        compression_pct=Decimal("0.0000"),
        underpaid=False, overpaid=False,
        base_rate_applied=Decimal("169.02"),
        peer_group="Clinic*", region="Downstate",
        discounting_applied=False, u6_applied=False, capital_applied=False,
        line_details=[],
    ))

    # A couple of CAS adjustments (one on c3 for denial, one on c2 for contractual)
    s.add(ORMAdjustment(claim_id_fk=c3.id, group_code="CO", reason_code="50",
                        amount=Decimal("750.00")))
    s.add(ORMAdjustment(claim_id_fk=c2.id, group_code="CO", reason_code="97",
                        amount=Decimal("450.00")))
    s.add(ORMAdjustment(claim_id_fk=c4.id, group_code="CO", reason_code="45",
                        amount=Decimal("100.00")))

    await s.commit()
    yield s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_totals_and_denial_rate(seeded):
    eng = AnalyticsEngine()
    out = await eng.summary(seeded)

    # 4 claims total (3 ERA + 1 PROF)
    assert out["total_claims"] == 4
    # Billed sum = 500 + 1500 + 750 + 300 = 3050
    assert Decimal(out["total_billed"]) == Decimal("3050.00")
    # Paid sum = 400 + 900 + 0 + 200 = 1500
    assert Decimal(out["total_paid"]) == Decimal("1500.00")
    # One claim denied out of 4 → 25%
    assert out["total_denied"] == 1
    assert Decimal(out["denial_rate_pct"]) == Decimal("25.0000")
    # APG claims = 3, underpayment total = 20 + 450 = 470
    assert out["apg_claims"] == 3
    assert Decimal(out["apg_underpayment_total"]) == Decimal("470.00")


@pytest.mark.asyncio
async def test_summary_filter_by_payer(seeded):
    """Filtering by payer restricts totals to that payer."""
    eng = AnalyticsEngine()
    out = await eng.summary(seeded, payer_name="AETNA COMMERCIAL")
    assert out["total_claims"] == 1
    assert Decimal(out["total_billed"]) == Decimal("300.00")


@pytest.mark.asyncio
async def test_summary_filter_by_date_range(seeded):
    """Date range filters claims to the specified window."""
    eng = AnalyticsEngine()
    out = await eng.summary(
        seeded,
        date_from=date(2023, 5, 1),
        date_to=date(2023, 12, 31),
    )
    # Claims in range: ERA-B (May), ERA-C (July) — 2 claims
    assert out["total_claims"] == 2


@pytest.mark.asyncio
async def test_compression_by_eapg_ranks_by_variance(seeded):
    eng = AnalyticsEngine()
    out = await eng.compression(seeded, group_by="eapg", limit=10)
    buckets = [r["bucket"] for r in out["rows"]]
    # EAPG 321 (SP debridement, $450 variance) should rank above 491 (Medical Visit, $20)
    idx_321 = next((i for i, b in enumerate(buckets) if b.startswith("321")), -1)
    idx_491 = next((i for i, b in enumerate(buckets) if b.startswith("491")), -1)
    assert idx_321 != -1 and idx_491 != -1
    assert idx_321 < idx_491


@pytest.mark.asyncio
async def test_compression_by_peer_group(seeded):
    eng = AnalyticsEngine()
    out = await eng.compression(seeded, group_by="peer_group")
    assert any(r["bucket"] == "Clinic*" for r in out["rows"])


@pytest.mark.asyncio
async def test_denials_ranked_by_amount(seeded):
    eng = AnalyticsEngine()
    out = await eng.denials(seeded)
    # Three adjustments: CO-50 ($750), CO-97 ($450), CO-45 ($100)
    # Top by amount should be CO-50
    assert out["rows"][0]["reason_code"] == "50"
    assert Decimal(out["rows"][0]["total_amount"]) == Decimal("750.00")
    # Amounts should sum to 1300
    total = sum(Decimal(r["total_amount"]) for r in out["rows"])
    assert total == Decimal("1300.00")


@pytest.mark.asyncio
async def test_trends_monthly_series_is_sorted(seeded):
    eng = AnalyticsEngine()
    out = await eng.trends(seeded, period="monthly")
    periods = [r["period"] for r in out["series"]]
    assert periods == sorted(periods)
    # Should have at least the months we seeded (2023-03, 04, 05, 07)
    assert "2023-03" in periods
    assert "2023-07" in periods


@pytest.mark.asyncio
async def test_trends_quarterly_rolls_up_months(seeded):
    eng = AnalyticsEngine()
    out = await eng.trends(seeded, period="quarterly")
    periods = [r["period"] for r in out["series"]]
    # Mar/Apr/May collapse into 2023-Q2 + 2023-Q1; July into Q3
    assert "2023-Q1" in periods  # March
    assert "2023-Q2" in periods  # Apr + May
    assert "2023-Q3" in periods  # July


@pytest.mark.asyncio
async def test_payer_scorecard_has_per_payer_rows(seeded):
    eng = AnalyticsEngine()
    out = await eng.payer_scorecard(seeded)
    payers = {r["payer_name"]: r for r in out["rows"]}
    # NYS Medicaid has 3 claims (2 underpaid, 1 denied); Aetna has 1
    assert payers["NYS MEDICAID"]["claims"] == 3
    assert payers["NYS MEDICAID"]["apg_claims"] == 3
    assert Decimal(payers["NYS MEDICAID"]["apg_underpayment_total"]) == Decimal("470.00")
    assert payers["AETNA COMMERCIAL"]["claims"] == 1
    # Aetna has no Article 28 claim, so apg_claims should be 0
    assert payers["AETNA COMMERCIAL"]["apg_claims"] == 0


@pytest.mark.asyncio
async def test_top_underpaid_procedures(seeded):
    eng = AnalyticsEngine()
    top = await eng.top_underpaid_procedures(seeded, limit=5)
    # Two underpaid procedures in the fixture — 11042 and 99213
    codes = [r["bucket"] for r in top]
    assert "11042" in codes
    assert "99213" in codes


@pytest.mark.asyncio
async def test_summary_with_no_data_returns_zeros(fresh_session):
    """Empty DB: every metric defaults to zero/0.0000 without raising."""
    eng = AnalyticsEngine()
    out = await eng.summary(fresh_session)
    assert out["total_claims"] == 0
    assert Decimal(out["total_billed"]) == Decimal("0")
    assert out["denial_rate_pct"] == "0.0000"
