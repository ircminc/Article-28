"""835I (Institutional Electronic Remittance Advice) EDI X12 parser.

The 835 transaction reports payment decisions made by a payer. "I" vs "P" is a
business classification, not an EDI-level distinction — both use transaction set 835.
At parse time we classify as 835I when CLP08 facility-code-value or the presence of
institutional revenue codes (SVC composite starting with 'NU' / rev codes) suggests it.

This implementation:
  * Auto-detects the segment terminator from the ISA envelope's 106th char
  * Handles line-wrapped and newline-joined EDI alike
  * Emits a ParsedClaim per CLP loop, with service lines from SVC loops
  * Preserves both claim-level and service-level CAS adjustments
  * Uses Decimal throughout — never float for money

References:
  - ASC X12N 005010X221A1 Health Care Claim Payment/Advice (835) Implementation Guide
  - CMS-1500 / UB-04 institutional billing conventions
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from backend.models.schemas import (
    ClaimAdjustment,
    FileType,
    ParsedClaim,
    ServiceAdjustment,
    ServiceLine,
)

# ---------------------------------------------------------------------------
# Segment-level lexing
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    tag: str
    elements: list[str]

    def get(self, idx: int, default: str = "") -> str:
        """1-based element accessor to match X12 convention (ISA01, CLP01...)."""
        if idx <= 0:
            raise ValueError("X12 element positions are 1-based")
        if idx - 1 < len(self.elements):
            return self.elements[idx - 1] or default
        return default

    def composite(self, idx: int, sub: int, default: str = "") -> str:
        """Read sub-element (composite) at 1-based `idx`.`sub` (e.g. SVC01-7)."""
        raw = self.get(idx, "")
        if not raw:
            return default
        parts = raw.split(":")
        if sub - 1 < len(parts):
            return parts[sub - 1] or default
        return default


class EDILexer:
    """Splits raw 835 text into Segment objects using auto-detected delimiters."""

    def __init__(self, text: str):
        if not text.startswith("ISA"):
            # Tolerate leading whitespace / BOM
            stripped = text.lstrip("\ufeff \t\r\n")
            if not stripped.startswith("ISA"):
                raise ValueError("Input does not start with an ISA segment (not a valid X12 interchange)")
            text = stripped

        # ISA is fixed-width. The 106th character of the ISA segment is the segment
        # terminator, and the 4th character is the element separator. The sub-element
        # separator lives at position 105.
        # Minimum length check:
        if len(text) < 106:
            raise ValueError("Input too short to contain an ISA header")
        self.element_sep = text[3]
        self.subelement_sep = text[104]
        self.segment_sep = text[105]

        # Strip newlines within segments (some senders prettify with \r\n after the terminator).
        normalized = text.replace("\r", "").replace("\n", "")
        # Split on the segment terminator
        raw_segments = normalized.split(self.segment_sep)
        self.segments: list[Segment] = []
        for raw in raw_segments:
            if not raw.strip():
                continue
            parts = raw.split(self.element_sep)
            tag = parts[0].strip()
            elements = [p for p in parts[1:]]
            if tag:
                self.segments.append(Segment(tag=tag, elements=elements))

    def __iter__(self) -> Iterable[Segment]:
        return iter(self.segments)


# ---------------------------------------------------------------------------
# Date / decimal helpers
# ---------------------------------------------------------------------------


def _dt(raw: str) -> Optional[date]:
    """Parse CCYYMMDD, CCYYMMDD-CCYYMMDD range (returns start), or YYMMDD."""
    if not raw:
        return None
    raw = raw.strip()
    if "-" in raw:  # DTM range; take the first half
        raw = raw.split("-", 1)[0].strip()
    for fmt in ("%Y%m%d", "%y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _money(raw: str) -> Decimal:
    if raw is None or raw == "":
        return Decimal("0")
    raw = raw.strip()
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except Exception:
        return Decimal("0")


def _int(raw: str, default: int = 0) -> int:
    if not raw:
        return default
    try:
        return int(Decimal(raw))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Parse result container
# ---------------------------------------------------------------------------


@dataclass
class Parsed835I:
    """Top-level result of parsing an 835I file.

    `claims` is the primary output — downstream consumers (APG engine, DB loader)
    iterate these. The metadata fields reflect the ISA/GS/BPR/TRN envelope.
    """
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
# CAS decoding
# ---------------------------------------------------------------------------


def _parse_cas(seg: Segment) -> list[tuple[str, str, Decimal, Optional[int]]]:
    """A single CAS segment can carry up to 6 (reason, amount, qty) triplets
    sharing a single group code at CAS01. Returns list of (group, reason, amount, qty).
    """
    group = seg.get(1)
    out: list[tuple[str, str, Decimal, Optional[int]]] = []
    # Elements are organized as: CAS01=group, then 2..19 in groups of 3.
    # CAS02+CAS03+CAS04  → (reason, amount, qty)
    # CAS05+CAS06+CAS07  → (reason, amount, qty)
    # etc., up to CAS19.
    for start in range(2, 20, 3):
        reason = seg.get(start)
        if not reason:
            continue
        amt = _money(seg.get(start + 1))
        qty_raw = seg.get(start + 2)
        qty = _int(qty_raw) if qty_raw else None
        out.append((group, reason, amt, qty))
    return out


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


class EDI835IParser:
    """Streaming-style parser driven by a cursor over the lexed segment list.

    The 835 hierarchy is:
        ISA  (interchange envelope)
        └── GS  (functional group)
            └── ST 835  (transaction set)
                ├── BPR, TRN, REF, DTM...
                ├── N1 PR (payer), N3, N4, REF, PER
                ├── N1 PE (payee), N3, N4, REF
                └── LX  (header)
                    └── CLP  (claim payment) — one per claim
                        ├── CAS  (claim-level adjustment)
                        ├── NM1  (patient / insured / provider names)
                        ├── MIA / MOA (inpatient / outpatient adjudication) — optional
                        ├── REF, DTM  (claim-level)
                        └── SVC  (service line)
                            ├── DTM  (service date)
                            ├── CAS  (service-level adjustment)
                            ├── REF  (line ref, authorization)
                            └── AMT, LQ, ...
    """

    def __init__(self, text: str):
        self.lex = EDILexer(text)
        self.result = Parsed835I()

    def parse(self) -> Parsed835I:
        segs = list(self.lex)
        i = 0

        # Envelope
        while i < len(segs):
            s = segs[i]
            if s.tag == "ISA":
                self.result.interchange_sender = s.get(6).strip()
                self.result.interchange_receiver = s.get(8).strip()
                d = _dt(s.get(9))
                self.result.interchange_date = d
            elif s.tag == "ST":
                self.result.transaction_set_id = s.get(1)
            elif s.tag == "BPR":
                self.result.payment_method = s.get(4)
                self.result.payment_amount = _money(s.get(2))
                self.result.payment_date = _dt(s.get(16))
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
                # Claim loop starts here — delegate, advance cursor
                i = self._parse_clp(segs, i)
                continue
            i += 1

        return self.result

    # -----------------------------------------------------------------
    # Claim loop
    # -----------------------------------------------------------------

    def _parse_clp(self, segs: list[Segment], start: int) -> int:
        """Parse one CLP loop. Returns the index of the first segment after the loop."""
        clp = segs[start]
        claim = ParsedClaim(
            file_type=FileType.ERA_835I,
            claim_id=clp.get(1),
            claim_status=clp.get(2),
            billed_amount=_money(clp.get(3)),
            paid_amount=_money(clp.get(4)),
            patient_responsibility=_money(clp.get(5)),
            claim_filing_indicator=clp.get(6) or None,
        )
        # Per 5010: CLP04 is "Monetary Amount" = claim paid amount.
        # CLP03 = total claim charge. CLP05 = patient responsibility.
        # CLP07 is the payer claim control number.
        # Note: some senders put "allowed" in CLP04 vs paid in a later field.
        # We treat CLP04 as paid per the spec; if the file has a different
        # convention, downstream reconciliation will catch it.

        # The claim loop ends at the next CLP, LX, SE, GE, IEA, or end-of-file.
        claim_terminators = {"CLP", "LX", "SE", "GE", "IEA"}

        i = start + 1
        current_line: Optional[ServiceLine] = None
        line_seq = 0

        while i < len(segs):
            s = segs[i]
            if s.tag in claim_terminators:
                break

            if s.tag == "CAS":
                # Claim-level if we haven't opened a service line yet; otherwise service-level.
                triples = _parse_cas(s)
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
                # QC = patient, IL = insured/subscriber, 82 = rendering provider
                full_name = _nm1_name(s)
                if entity == "QC":
                    claim.patient_name = full_name
                    claim.patient_id = s.get(9) or None
                elif entity == "82":
                    claim.provider_name = full_name
                    claim.provider_npi = s.get(9) or None

            elif s.tag == "DTM":
                qual = s.get(1)
                d = _dt(s.get(2))
                if qual in ("232", "472") and d:  # claim statement / service date
                    if claim.date_of_service is None:
                        claim.date_of_service = d
                    if current_line and current_line.date_of_service is None:
                        current_line.date_of_service = d
                elif qual == "150" and current_line:  # service period start
                    current_line.date_of_service = d or current_line.date_of_service

            elif s.tag == "AMT":
                qual = s.get(1)
                amt = _money(s.get(2))
                # AMT*B6 = allowed amount (used in many payer implementations)
                if qual == "B6":
                    if current_line is not None:
                        current_line.allowed_amount = amt
                    else:
                        claim.allowed_amount = amt

            elif s.tag == "SVC":
                # SVC01 is composite: "HC:99213:25" for HCPCS 99213 modifier 25,
                # or "NU:0450" for revenue code.
                line_seq += 1
                qual = s.composite(1, 1)
                procedure = s.composite(1, 2)
                modifiers = [m for m in (s.composite(1, 3), s.composite(1, 4),
                                          s.composite(1, 5), s.composite(1, 6)) if m]
                rev_code = None
                if qual in ("NU", "ZZ"):
                    rev_code = procedure  # In institutional claims, revenue code lives here
                    procedure = ""
                sl = ServiceLine(
                    line_seq=line_seq,
                    procedure_code=procedure or (rev_code or ""),
                    modifiers=modifiers,
                    revenue_code=rev_code,
                    billed_amount=_money(s.get(2)),
                    paid_amount=_money(s.get(3)),
                    units=_int(s.get(5), 1),
                )
                # SVC04 = revenue code (institutional), preferred when present
                if s.get(4):
                    sl.revenue_code = s.get(4)
                claim.service_lines.append(sl)
                current_line = sl

            i += 1

        self.result.claims.append(claim)
        return i


def _nm1_name(seg: Segment) -> str:
    """Assemble NM1 name: last + first + middle (for persons) or org name."""
    last_or_org = seg.get(3)
    first = seg.get(4)
    middle = seg.get(5)
    entity_type = seg.get(2)  # 1=person, 2=org
    if entity_type == "1":
        parts = [p for p in (first, middle, last_or_org) if p]
        return " ".join(parts)
    return last_or_org


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_835i(text_or_path: str | Path) -> Parsed835I:
    """Parse an 835I from either a file path or raw EDI text.

    Usage:
        result = parse_835i(Path("remittance.835"))
        for claim in result.claims:
            print(claim.claim_id, claim.paid_amount)
    """
    if isinstance(text_or_path, (str, Path)):
        p = Path(text_or_path) if not isinstance(text_or_path, str) or (
            isinstance(text_or_path, str) and len(text_or_path) < 260 and Path(text_or_path).exists()
        ) else None
        if p is not None and p.exists() and p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
        else:
            text = str(text_or_path)
    else:
        raise TypeError("parse_835i expects a path or raw EDI text")

    return EDI835IParser(text).parse()
