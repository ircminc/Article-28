"""835P (Professional Electronic Remittance Advice) EDI X12 parser.

835I and 835P share transaction set 835; the EDI structure is identical. The
business difference is what's *inside*: professional remittances carry CPT/HCPCS
procedure codes (no UB-04 revenue codes), and modifier usage is more common.

Implementation: we reuse EDI835IParser and override the file_type. The `Parsed835P`
result is structurally identical to Parsed835I — separate for type clarity at the
call site and to allow future divergence (e.g. 835P-specific CARC interpretation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from backend.models.schemas import FileType, ParsedClaim
from backend.parsers._common import load_edi_text
from backend.parsers.edi_835i import EDI835IParser


@dataclass
class Parsed835P:
    interchange_sender: str = ""
    interchange_receiver: str = ""
    interchange_date: Optional[date] = None
    transaction_set_id: str = ""
    payment_method: str = ""
    payment_amount: Decimal = Decimal("0")
    payment_date: Optional[date] = None
    check_eft_trace: str = ""
    payer_name: str = ""
    payer_id: str = ""
    payee_name: str = ""
    payee_npi: str = ""
    claims: list[ParsedClaim] = field(default_factory=list)


def parse_835p(text_or_path) -> Parsed835P:
    """Parse an 835P from either a file path or raw EDI text.

    Reuses the 835I parser internals and stamps claims with FileType.ERA_835P.
    """
    raw = load_edi_text(text_or_path)
    p = EDI835IParser(raw, file_type=FileType.ERA_835P)
    result = p.parse()
    return Parsed835P(
        interchange_sender=result.interchange_sender,
        interchange_receiver=result.interchange_receiver,
        interchange_date=result.interchange_date,
        transaction_set_id=result.transaction_set_id,
        payment_method=result.payment_method,
        payment_amount=result.payment_amount,
        payment_date=result.payment_date,
        check_eft_trace=result.check_eft_trace,
        payer_name=result.payer_name,
        payer_id=result.payer_id,
        payee_name=result.payee_name,
        payee_npi=result.payee_npi,
        claims=result.claims,
    )
