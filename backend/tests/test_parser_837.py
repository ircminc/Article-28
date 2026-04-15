"""Unit tests for the 837I/837P EDI parsers."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from backend.models.schemas import FileType
from backend.parsers.edi_837 import parse_837

FIXTURE_837P = Path(__file__).parent / "fixtures" / "sample_837p.edi"
FIXTURE_837I = Path(__file__).parent / "fixtures" / "sample_837i.edi"


# ---------------------------------------------------------------------------
# 837P (professional)
# ---------------------------------------------------------------------------


def test_837p_envelope_detection():
    """GS08 contains '005010X222A1' → professional."""
    result = parse_837(FIXTURE_837P)
    assert result.file_type == FileType.CLAIM_837P
    assert result.submitter_name == "IRC MINC"
    assert result.receiver_name == "NEW YORK STATE MEDICAID"
    assert result.billing_provider_name == "SAMPLE CLINIC LLC"
    assert result.billing_provider_npi == "1234567890"
    assert "X222" in result.implementation_guide


def test_837p_two_claims_parsed():
    result = parse_837(FIXTURE_837P)
    assert len(result.claims) == 2
    ids = [c.claim_id for c in result.claims]
    assert ids == ["CLM0001", "CLM0002"]


def test_837p_diagnosis_codes_extracted_as_principal_and_other():
    """ABK is the principal diagnosis qualifier; ABF is other.
    CLM0001 carries ABK:E119 (type 2 diabetes) + ABF:I10 (essential hypertension)."""
    result = parse_837(FIXTURE_837P)
    c1 = result.claims[0]
    assert c1.principal_diagnosis == "E119"
    assert "I10" in c1.other_diagnoses


def test_837p_service_lines_have_no_revenue_codes():
    """SV1 segments — professional only, no UB-04 revenue code."""
    result = parse_837(FIXTURE_837P)
    c1 = result.claims[0]
    assert len(c1.service_lines) == 2
    codes = sorted(sl.procedure_code for sl in c1.service_lines)
    assert codes == ["17000", "99213"]
    for sl in c1.service_lines:
        assert sl.revenue_code is None


def test_837p_claim_charges_match():
    result = parse_837(FIXTURE_837P)
    c1, c2 = result.claims
    assert c1.billed_amount == Decimal("500.00")
    assert c2.billed_amount == Decimal("1200.00")


def test_837p_patient_name_from_subscriber_when_no_patient_loop():
    """In our fixture there's no separate HL patient loop (subscriber is the patient),
    so patient_name should fall back to the IL subscriber."""
    result = parse_837(FIXTURE_837P)
    c1 = result.claims[0]
    assert c1.patient_name and "DOE" in c1.patient_name.upper()


def test_837p_dates_from_dtp_472():
    result = parse_837(FIXTURE_837P)
    c1 = result.claims[0]
    assert c1.date_of_service is not None
    assert c1.date_of_service.isoformat() == "2023-05-15"


# ---------------------------------------------------------------------------
# 837I (institutional)
# ---------------------------------------------------------------------------


def test_837i_envelope_detection():
    """GS08 contains '005010X223A2' → institutional."""
    result = parse_837(FIXTURE_837I)
    assert result.file_type == FileType.CLAIM_837I
    assert result.billing_provider_name == "SAMPLE DTC LLC"
    assert result.billing_provider_npi == "9876543210"


def test_837i_sv2_lines_have_revenue_codes():
    """Institutional claims use SV2 segments; SV201 is the revenue code."""
    result = parse_837(FIXTURE_837I)
    c = result.claims[0]
    assert len(c.service_lines) == 3
    rev_codes = sorted([sl.revenue_code for sl in c.service_lines if sl.revenue_code])
    assert rev_codes == ["0270", "0300", "0450"]

    # Procedure codes should also be populated (SV202 composite)
    procs = sorted(sl.procedure_code for sl in c.service_lines)
    assert procs == ["80053", "99284", "J1100"]


def test_837i_institutional_diagnosis_qualifiers():
    """ABJ (admitting) AND ABK (principal) present — either populates principal_diagnosis
    depending on which appears first in the HI segment."""
    result = parse_837(FIXTURE_837I)
    c = result.claims[0]
    assert c.principal_diagnosis in ("J449", "J449")  # admitting + principal both = J449 in fixture
    assert "I10" in c.other_diagnoses


def test_837i_service_units_preserved():
    """Line 3 in the institutional fixture has 2 units (J1100, a drug code)."""
    result = parse_837(FIXTURE_837I)
    c = result.claims[0]
    j1100 = next(sl for sl in c.service_lines if sl.procedure_code == "J1100")
    assert j1100.units == 2


def test_837_raw_text_input():
    """parse_837 accepts raw EDI text, not just a path."""
    raw = FIXTURE_837P.read_text()
    result = parse_837(raw)
    assert len(result.claims) == 2
    assert result.file_type == FileType.CLAIM_837P
