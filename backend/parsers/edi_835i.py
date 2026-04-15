"""835I (Institutional Electronic Remittance Advice) EDI X12 parser.

The 835 transaction reports payment decisions made by a payer. "I" vs "P" is a
business classification, not an EDI-level distinction — both use transaction set 835.
At parse time we classify as 835I when the envelope/claim signals institutional
context (revenue codes in composite, UB-04 facility type, etc.). Phase 1 callers
explicitly invoke parse_835i() when they know the file is institutional.

Shared primitives (lexer, Segment, CAS expansion, NM1 helpers, decoders) live in
parsers._common so 835P and 837 parsers can reuse them.

References:
  - ASC X12N 005010X221A1 Health Care Claim Payment/Advice (835) Implementation Guide
  - CMS-1500 / UB-04 institutional billing conventions
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from backend.models.schemas import (
    ClaimAdjustment,
    FileType,
    ParsedClaim,
    ServiceAdjustment,
    ServiceLine,
)
from backend.parsers._common import (
    EDILexer,
    Segment,
    decode_hcpcs_composite,
    expand_cas,
    load_edi_text,
    nm1_id,
    nm1_name,
    parse_date,
    parse_money,
    parse_int,
)


# ---------------------------------------------------------------------------
# Parse result container
# ---------------------------------------------------------------------------


@dataclass
class Parsed835I:
    """Top-level result of parsing an 835I file."""
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


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


class EDI835IParser:
    """Stream-style parser over the lexed segment list.

    The 835 hierarchy is:
        ISA / GS / ST 835
        ├── BPR, TRN, REF, DTM
        ├── N1 PR (payer), N3, N4, REF, PER
        ├── N1 PE (payee), N3, N4, REF
        └── LX header
            └── CLP (claim payment) — one per claim
                ├── CAS (claim-level adjustment)
                ├── NM1 (patient / insured / provider names)
                ├── REF, DTM (claim-level)
                └── SVC (service line)
                    ├── DTM (service date)
                    ├── CAS (service-level adjustment)
                    ├── REF (line ref, authorization)
                    └── AMT, LQ, ...
    """

    # Segments that terminate a CLP loop when we see them while inside one.
    _CLAIM_TERMINATORS = {"CLP", "LX", "SE", "GE", "IEA"}

    def __init__(self, text: str, file_type: FileType = FileType.ERA_835I):
        self.lex = EDILexer(text)
        self.file_type = file_type
        self.result = Parsed835I()

    # -----------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------

    def parse(self) -> Parsed835I:
        segs = list(self.lex)
        i = 0
        while i < len(segs):
            s = segs[i]
            if s.tag == "ISA":
                self.result.interchange_sender = s.get(6).strip()
                self.result.interchange_receiver = s.get(8).strip()
                self.result.interchange_date = parse_date(s.get(9))
            elif s.tag == "ST":
                self.result.transaction_set_id = s.get(1)
            elif s.tag == "BPR":
                self.result.payment_method = s.get(4)
                self.result.payment_amount = parse_money(s.get(2))
                self.result.payment_date = parse_date(s.get(16))
            elif s.tag == "TRN":
                self.result.check_eft_trace = s.get(2)
            elif s.tag == "N1":
                entity = s.get(1)
                name = s.get(2)
                if entity == "PR":
                    self.result.payer_name = name
                    self.result.payer_id = s.get(4)
                elif entity == "PE":
                    self.result.payee_name = name
                    self.result.payee_npi = s.get(4)
            elif s.tag == "CLP":
                i = self._parse_clp(segs, i)
                continue
            i += 1
        return self.result

    # -----------------------------------------------------------------
    # Claim loop
    # -----------------------------------------------------------------

    def _parse_clp(self, segs: list[Segment], start: int) -> int:
        """Parse one CLP loop. Returns the index of the first segment after it."""
        clp = segs[start]
        claim = ParsedClaim(
            file_type=self.file_type,
            claim_id=clp.get(1),
            claim_status=clp.get(2),
            billed_amount=parse_money(clp.get(3)),
            paid_amount=parse_money(clp.get(4)),
            patient_responsibility=parse_money(clp.get(5)),
            claim_filing_indicator=clp.get(6) or None,
        )

        i = start + 1
        current_line: Optional[ServiceLine] = None
        line_seq = 0

        while i < len(segs):
            s = segs[i]
            if s.tag in self._CLAIM_TERMINATORS:
                break

            if s.tag == "CAS":
                triples = expand_cas(s)
                if current_line is None:
                    for group, reason, amount, qty in triples:
                        claim.adjustments.append(ClaimAdjustment(
                            group_code=group, reason_code=reason,
                            amount=amount, quantity=qty,
                        ))
                else:
                    for group, reason, amount, qty in triples:
                        current_line.adjustments.append(ServiceAdjustment(
                            group_code=group, reason_code=reason,
                            amount=amount, quantity=qty,
                        ))

            elif s.tag == "NM1":
                entity = s.get(1)
                full_name = nm1_name(s)
                nm1_identifier = nm1_id(s)
                # QC = patient, IL = insured/subscriber, 82 = rendering provider
                if entity == "QC":
                    claim.patient_name = full_name
                    claim.patient_id = nm1_identifier
                elif entity == "82":
                    claim.provider_name = full_name
                    claim.provider_npi = nm1_identifier

            elif s.tag == "DTM":
                qual = s.get(1)
                d = parse_date(s.get(2))
                # 232 = claim statement start, 472 = service date
                if qual in ("232", "472") and d:
                    if claim.date_of_service is None:
                        claim.date_of_service = d
                    if current_line and current_line.date_of_service is None:
                        current_line.date_of_service = d
                elif qual == "150" and current_line:
                    current_line.date_of_service = d or current_line.date_of_service

            elif s.tag == "AMT":
                qual = s.get(1)
                amt = parse_money(s.get(2))
                # AMT*B6 = allowed amount
                if qual == "B6":
                    if current_line is not None:
                        current_line.allowed_amount = amt
                    else:
                        claim.allowed_amount = amt

            elif s.tag == "SVC":
                # SVC01 composite: HC:99213:25 (procedure) or NU:0450 (revenue code)
                line_seq += 1
                qual, procedure, modifiers, rev_code = decode_hcpcs_composite(s, 1)
                if qual in ("NU", "ZZ") and not procedure:
                    # Institutional — revenue code in the composite, no HCPCS.
                    rev_code = s.composite(1, 2) or rev_code
                    procedure = ""
                sl = ServiceLine(
                    line_seq=line_seq,
                    procedure_code=procedure or (rev_code or ""),
                    modifiers=modifiers,
                    revenue_code=rev_code,
                    billed_amount=parse_money(s.get(2)),
                    paid_amount=parse_money(s.get(3)),
                    units=parse_int(s.get(5), 1),
                )
                # SVC04 = revenue code (institutional). Senders sometimes put "0" or "1"
                # as a placeholder on professional claims where there's no revenue code —
                # treat those as absent. Real UB-04 revenue codes are always 3-4 digits.
                svc04 = s.get(4)
                if svc04 and len(svc04) >= 3:
                    sl.revenue_code = svc04
                claim.service_lines.append(sl)
                current_line = sl

            i += 1

        self.result.claims.append(claim)
        return i


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_835i(text_or_path) -> Parsed835I:
    """Parse an 835I from either a file path or raw EDI text."""
    return EDI835IParser(load_edi_text(text_or_path)).parse()
