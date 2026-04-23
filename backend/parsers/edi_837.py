"""837 (Health Care Claim) EDI X12 parser — institutional (837I) and professional (837P).

837 files are what providers *submit* to payers (unlike 835 which is what payers
*return*). The key value for our application: 837 files carry the diagnosis codes
(HI segments) that let the APG engine improve EAPG assignment when HCPCS alone
doesn't cut it.

Transaction set 837 comes in three flavors in X12 5010:
  - 005010X222A1 — Professional (837P)
  - 005010X223A2 — Institutional (837I)
  - 005010X224A2 — Dental (837D) — not supported in Phase 2
We classify as 837I vs 837P by:
  1. The GS08 implementation guide version if present
  2. The presence of SV2 segments (institutional) vs SV1 (professional)
  3. Caller-specified file_type_hint when ambiguous

Key differences from 835:
  * CLM not CLP (claim information, not claim payment)
  * DTP not DTM (with a format qualifier in DTP02)
  * Hierarchical HL loops identify billing provider → subscriber → patient
  * HI segments carry diagnosis codes (principal + up to 24 additional)
  * No paid/allowed amounts — this is a submission, not a remittance

References:
  - ASC X12N 005010X222A1 Health Care Claim: Professional (837)
  - ASC X12N 005010X223A2 Health Care Claim: Institutional (837)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from backend.models.schemas import (
    FileType,
    ParsedClaim,
    ServiceLine,
)
from backend.parsers._common import (
    EDILexer,
    Segment,
    decode_hcpcs_composite,
    load_edi_text,
    nm1_id,
    nm1_name,
    parse_date,
    parse_dtp_date,
    parse_int,
    parse_money,
)


# ---------------------------------------------------------------------------
# Parse result container
# ---------------------------------------------------------------------------


@dataclass
class Parsed837:
    """Top-level result of parsing an 837 (I or P) file."""
    file_type: FileType = FileType.CLAIM_837P
    interchange_sender: str = ""
    interchange_receiver: str = ""
    interchange_date: Optional[date] = None
    transaction_set_id: str = ""
    implementation_guide: str = ""
    submitter_name: str = ""
    receiver_name: str = ""
    billing_provider_name: str = ""
    billing_provider_npi: str = ""
    claims: list[ParsedClaim] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Diagnosis code extraction
# ---------------------------------------------------------------------------


def _extract_hi_diagnoses(seg: Segment) -> list[tuple[str, str]]:
    """Pull diagnosis code composites out of a single HI segment.

    HI elements are composites: qualifier:code[:date[:...]].
    Qualifiers we care about (5010):
      ABK / BK  — Principal diagnosis (ICD-10-CM / ICD-9-CM)
      ABF / BF  — Other diagnosis
      ABJ / BJ  — Admitting diagnosis (institutional)
      APR / PR  — Patient reason for visit
      ABN / BN  — External cause of injury
      ABR / BR  — Reason for visit (institutional)

    Returns list of (qualifier, dx_code). First HI element conventionally
    carries the principal diagnosis when ABK/BK is used.
    """
    out: list[tuple[str, str]] = []
    for idx in range(1, 13):  # up to HI12 (12 composites)
        raw = seg.get(idx)
        if not raw:
            continue
        parts = raw.split(":")
        if len(parts) < 2:
            continue
        qualifier = parts[0].strip().upper()
        # Canonicalize ICD codes to the dot-free uppercase form the crosswalk
        # stores them in (NYS DOH / eMedNY publish without dots). EDI 5010
        # submitters are supposed to omit dots too, but in practice many
        # don't — stripping here keeps the engine's lookup from missing.
        code = parts[1].strip().upper().replace(".", "")
        if code:
            out.append((qualifier, code))
    return out


def _is_principal(qualifier: str) -> bool:
    return qualifier in ("ABK", "BK", "ABJ", "BJ", "PR", "APR")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class EDI837Parser:
    """Parser driven by a linear walk over segments, tracking HL loop context.

    The 837 hierarchy:
        ISA / GS / ST 837
        ├── BHT
        ├── NM1 41 (submitter)
        ├── NM1 40 (receiver)
        └── HL* loops:
            └── HL (billing provider level — HL03=20)
                ├── NM1 85 (billing provider)
                ├── NM1 87 (pay-to provider) — optional
                └── HL (subscriber level — HL03=22)
                    ├── NM1 IL (subscriber)
                    ├── NM1 PR (payer)
                    └── (optional) HL (patient level — HL03=23)
                        ├── NM1 QC (patient)
                        └── CLM loop:
                            ├── DTP (dates)
                            ├── CL1 (institutional only — admission/discharge)
                            ├── HI (diagnosis codes)
                            ├── NM1 82 (rendering provider)
                            └── LX service lines
                                ├── SV1 (professional) or SV2 (institutional)
                                ├── DTP (service date)
                                └── REF (line refs)

    This implementation flattens: we don't enforce HL nesting, we just track the
    "current subscriber / current patient / current billing provider" as we walk.
    For our purposes (claim extraction) that's sufficient.
    """

    _CLAIM_TERMINATORS = {"CLM", "HL", "SE", "GE", "IEA"}

    def __init__(self, text: str, file_type_hint: Optional[FileType] = None):
        self.lex = EDILexer(text)
        self.result = Parsed837()
        if file_type_hint:
            self.result.file_type = file_type_hint

    def parse(self) -> Parsed837:
        segs = list(self.lex)

        # Pre-scan for implementation-guide hint (GS08) and file type detection
        for s in segs:
            if s.tag == "GS":
                self.result.implementation_guide = s.get(8)
                if "X222" in s.get(8):
                    self.result.file_type = FileType.CLAIM_837P
                elif "X223" in s.get(8):
                    self.result.file_type = FileType.CLAIM_837I
                break
        # Fallback detection: SV2 → institutional, SV1 → professional
        if not self.result.implementation_guide:
            has_sv2 = any(s.tag == "SV2" for s in segs)
            has_sv1 = any(s.tag == "SV1" for s in segs)
            if has_sv2 and not has_sv1:
                self.result.file_type = FileType.CLAIM_837I
            elif has_sv1:
                self.result.file_type = FileType.CLAIM_837P

        i = 0
        current_subscriber_name = ""
        current_patient_name = ""
        current_patient_id: Optional[str] = None

        while i < len(segs):
            s = segs[i]

            if s.tag == "ISA":
                self.result.interchange_sender = s.get(6).strip()
                self.result.interchange_receiver = s.get(8).strip()
                self.result.interchange_date = parse_date(s.get(9))
            elif s.tag == "ST":
                self.result.transaction_set_id = s.get(1)
            elif s.tag == "NM1":
                entity = s.get(1)
                name = nm1_name(s)
                ident = nm1_id(s)
                if entity == "41":
                    self.result.submitter_name = name
                elif entity == "40":
                    self.result.receiver_name = name
                elif entity == "85":
                    self.result.billing_provider_name = name
                    self.result.billing_provider_npi = ident or ""
                elif entity == "IL":
                    current_subscriber_name = name
                elif entity == "QC":
                    current_patient_name = name
                    current_patient_id = ident
            elif s.tag == "CLM":
                # Patient defaults to subscriber if no separate patient loop
                patient_name = current_patient_name or current_subscriber_name
                patient_id = current_patient_id
                i = self._parse_clm(
                    segs, i,
                    patient_name=patient_name,
                    patient_id=patient_id,
                )
                continue

            i += 1

        return self.result

    # -----------------------------------------------------------------
    # Claim loop
    # -----------------------------------------------------------------

    def _parse_clm(
        self, segs: list[Segment], start: int,
        *, patient_name: str, patient_id: Optional[str],
    ) -> int:
        clm = segs[start]
        claim = ParsedClaim(
            file_type=self.result.file_type,
            claim_id=clm.get(1),
            billed_amount=parse_money(clm.get(2)),
            patient_name=patient_name,
            patient_id=patient_id,
            # CLM submission has no paid/allowed amounts
            allowed_amount=Decimal("0"),
            paid_amount=Decimal("0"),
            patient_responsibility=Decimal("0"),
            claim_filing_indicator=None,
        )

        # Local context for service lines
        current_line: Optional[ServiceLine] = None
        line_seq = 0

        i = start + 1
        while i < len(segs):
            s = segs[i]
            if s.tag in self._CLAIM_TERMINATORS:
                break

            if s.tag == "HI":
                # Diagnosis codes — principal first, then additional
                for qual, code in _extract_hi_diagnoses(s):
                    if _is_principal(qual) and not claim.principal_diagnosis:
                        claim.principal_diagnosis = code
                    elif code not in claim.other_diagnoses:
                        if claim.principal_diagnosis != code:
                            claim.other_diagnoses.append(code)

            elif s.tag == "NM1":
                entity = s.get(1)
                if entity == "82":  # rendering provider
                    claim.provider_name = nm1_name(s)
                    claim.provider_npi = nm1_id(s) or ""

            elif s.tag == "DTP":
                qual = s.get(1)
                d = parse_dtp_date(s)
                if d is None:
                    pass
                # 431 = onset of illness, 472 = service date, 434 = statement from/through,
                # 096 = discharge, 435 = admission
                elif qual in ("472", "434"):
                    if claim.date_of_service is None:
                        claim.date_of_service = d
                    if current_line and current_line.date_of_service is None:
                        current_line.date_of_service = d

            elif s.tag == "LX":
                # LX header starts a new service line loop; actual line data is in SV1/SV2
                pass

            elif s.tag in ("SV1", "SV2"):
                line_seq += 1
                if s.tag == "SV1":
                    # Professional: SV101 is composite HCPCS code, SV102 is charge,
                    # SV103 = unit basis, SV104 = quantity
                    qual, procedure, modifiers, _rev = decode_hcpcs_composite(s, 1)
                    charge = parse_money(s.get(2))
                    units = parse_int(s.get(4), 1)
                    sl = ServiceLine(
                        line_seq=line_seq,
                        procedure_code=procedure,
                        modifiers=modifiers,
                        revenue_code=None,
                        billed_amount=charge,
                        units=units,
                    )
                else:  # SV2
                    # Institutional: SV201 = revenue code, SV202 = composite HCPCS,
                    # SV203 = charge, SV204 = unit basis, SV205 = quantity
                    revenue_code = s.get(1) or None
                    qual, procedure, modifiers, _rev = decode_hcpcs_composite(s, 2)
                    charge = parse_money(s.get(3))
                    units = parse_int(s.get(5), 1)
                    sl = ServiceLine(
                        line_seq=line_seq,
                        procedure_code=procedure or (revenue_code or ""),
                        modifiers=modifiers,
                        revenue_code=revenue_code,
                        billed_amount=charge,
                        units=units,
                    )
                claim.service_lines.append(sl)
                current_line = sl

            i += 1

        self.result.claims.append(claim)
        return i


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_837(text_or_path, file_type_hint: Optional[FileType] = None) -> Parsed837:
    """Parse an 837I or 837P from either a file path or raw EDI text.

    file_type_hint is optional; the parser auto-detects from the GS08
    implementation guide ID and/or SV1 vs SV2 presence.
    """
    return EDI837Parser(load_edi_text(text_or_path), file_type_hint=file_type_hint).parse()
