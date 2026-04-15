"""Unit tests for the 835P (professional remittance) parser.

835P reuses the 835I parser under the hood; these tests verify that the wrapper
correctly tags claims as FileType.ERA_835P and that the fixture's distinctive
features (CAS claim vs service level adjustments, AMT*B6 allowed amounts,
commercial claim_filing_indicator) round-trip properly.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from backend.models.schemas import FileType
from backend.parsers.edi_835p import parse_835p

FIXTURE = Path(__file__).parent / "fixtures" / "sample_835p.edi"


def test_835p_envelope_and_classification():
    r = parse_835p(FIXTURE)
    assert r.transaction_set_id == "835"
    assert r.payer_name == "AETNA COMMERCIAL"
    assert r.payment_amount == Decimal("980.50")
    assert r.payee_name == "SAMPLE PROVIDER GROUP"
    for claim in r.claims:
        assert claim.file_type == FileType.ERA_835P


def test_835p_two_claims():
    r = parse_835p(FIXTURE)
    assert len(r.claims) == 2
    ids = [c.claim_id for c in r.claims]
    assert ids == ["COMMCLM01", "COMMCLM02"]


def test_835p_commercial_filing_indicator():
    """CI = Commercial Insurance (not MA/MC Medicaid)."""
    r = parse_835p(FIXTURE)
    assert all(c.claim_filing_indicator == "CI" for c in r.claims)


def test_835p_claim1_cas_breakdown():
    """COMMCLM01 first line: 99214 billed 250, paid 180.
    CAS*CO*45 for 50 (contractual), CAS*PR*2 for 20 (patient coinsurance)."""
    r = parse_835p(FIXTURE)
    c = r.claims[0]
    ln_99214 = next(sl for sl in c.service_lines if sl.procedure_code == "99214")
    assert ln_99214.billed_amount == Decimal("250.00")
    assert ln_99214.paid_amount == Decimal("180.00")
    # Two service-level adjustments
    adjustments = {(a.group_code, a.reason_code): a.amount for a in ln_99214.adjustments}
    assert adjustments[("CO", "45")] == Decimal("50.00")
    assert adjustments[("PR", "2")] == Decimal("20.00")


def test_835p_amt_b6_allowed_amount():
    r = parse_835p(FIXTURE)
    c = r.claims[0]
    ln_99214 = next(sl for sl in c.service_lines if sl.procedure_code == "99214")
    assert ln_99214.allowed_amount == Decimal("180.00")


def test_835p_no_revenue_codes():
    """Professional remittances should not carry UB-04 revenue codes on the service lines."""
    r = parse_835p(FIXTURE)
    for c in r.claims:
        for sl in c.service_lines:
            assert sl.revenue_code is None
