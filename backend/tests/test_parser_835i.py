"""Unit tests for the 835I EDI parser."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from backend.parsers.edi_835i import EDILexer, parse_835i

FIXTURE = Path(__file__).parent / "fixtures" / "sample_835i.edi"


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------


def test_lexer_detects_delimiters_from_isa():
    raw = FIXTURE.read_text()
    lex = EDILexer(raw)
    assert lex.element_sep == "*"
    assert lex.segment_sep == "~"
    # At least ISA, GS, ST, BPR, multiple CLP, and IEA should be present
    tags = [s.tag for s in lex]
    assert "ISA" in tags
    assert "BPR" in tags
    assert tags.count("CLP") == 3
    assert "IEA" in tags


def test_lexer_rejects_non_x12_input():
    with pytest.raises(ValueError, match="ISA"):
        EDILexer("Not an EDI document")


# ---------------------------------------------------------------------------
# Full parse
# ---------------------------------------------------------------------------


def test_parse_envelope_fields():
    result = parse_835i(FIXTURE)
    assert result.transaction_set_id == "835"
    assert result.payer_name == "NEW YORK STATE MEDICAID"
    assert result.payer_id == "NYSDOH"
    assert result.payee_name == "SAMPLE CLINIC LLC"
    assert result.payee_npi == "1234567890"
    assert result.payment_amount == Decimal("1250.75")


def test_parse_three_claims():
    result = parse_835i(FIXTURE)
    assert len(result.claims) == 3
    ids = [c.claim_id for c in result.claims]
    assert ids == ["CLM0001", "CLM0002", "CLM0003"]


def test_claim1_amounts_and_svc_lines():
    """CLM0001: billed 500, paid 320, DOS 2023-05-15, two service lines.
    One service (99213) has a CO-97 packaging denial; the other (17000) was paid."""
    result = parse_835i(FIXTURE)
    c = next(x for x in result.claims if x.claim_id == "CLM0001")
    assert c.billed_amount == Decimal("500.00")
    assert c.paid_amount == Decimal("320.00")
    assert c.patient_responsibility == Decimal("0")
    assert c.claim_filing_indicator == "MC"  # Medicaid
    assert c.date_of_service is not None and c.date_of_service.isoformat() == "2023-05-15"
    assert c.patient_name.startswith("JANE")

    assert len(c.service_lines) == 2
    codes = sorted(sl.procedure_code for sl in c.service_lines)
    assert codes == ["17000", "99213"]

    # 99213 has a CAS*CO*97 adjustment at the service level
    ln_99213 = next(sl for sl in c.service_lines if sl.procedure_code == "99213")
    assert len(ln_99213.adjustments) == 1
    assert ln_99213.adjustments[0].group_code == "CO"
    assert ln_99213.adjustments[0].reason_code == "97"
    assert ln_99213.adjustments[0].amount == Decimal("150.00")


def test_claim2_multiple_significant_procedures():
    """CLM0002 has 11042 + 17000 — both significant procedures. The parser
    just emits them; discounting logic is the engine's concern."""
    result = parse_835i(FIXTURE)
    c = next(x for x in result.claims if x.claim_id == "CLM0002")
    assert len(c.service_lines) == 2
    assert c.billed_amount == Decimal("1200.00")
    assert c.paid_amount == Decimal("750.00")


def test_claim3_denied_with_cas_reason_50():
    """CLM0003 was denied (claim status 4, paid 0, patient resp 250).
    Service line has CAS*CO*50 (non-covered — requires authorization / not medically necessary)."""
    result = parse_835i(FIXTURE)
    c = next(x for x in result.claims if x.claim_id == "CLM0003")
    assert c.claim_status == "4"
    assert c.paid_amount == Decimal("0")
    assert c.patient_responsibility == Decimal("250.00")
    assert len(c.service_lines) == 1
    sl = c.service_lines[0]
    assert sl.procedure_code == "99213"
    assert sl.adjustments[0].reason_code == "50"


def test_allowed_amount_from_amt_b6():
    """AMT*B6 in CLM0001's second service line (17000) should populate allowed_amount."""
    result = parse_835i(FIXTURE)
    c = next(x for x in result.claims if x.claim_id == "CLM0001")
    ln_17000 = next(sl for sl in c.service_lines if sl.procedure_code == "17000")
    assert ln_17000.allowed_amount == Decimal("320.00")


def test_raw_string_input():
    """parse_835i should accept raw EDI text directly, not only a file path."""
    raw = FIXTURE.read_text()
    result = parse_835i(raw)
    assert len(result.claims) == 3
