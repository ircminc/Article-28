"""Pydantic v2 schemas shared across parsers, engines, and API layer.

These are the DTOs — not ORM classes (those live in db/database.py). The parser
returns a ParsedClaim built from one of these; the APG engine consumes it.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


# ---------------------------------------------------------------------------
# File / claim enums
# ---------------------------------------------------------------------------


class FileType(str, Enum):
    ERA_835I = "835I"
    ERA_835P = "835P"
    CLAIM_837I = "837I"
    CLAIM_837P = "837P"


class ClaimFilingIndicator(str, Enum):
    """CLP09 — Claim Filing Indicator. Not exhaustive; most common codes only."""
    MEDICARE_A = "MA"
    MEDICARE_B = "MB"
    MEDICAID = "MC"
    COMMERCIAL = "CI"
    HMO = "HM"
    BLUE_CROSS = "BL"
    OTHER = "ZZ"


class AdjustmentGroupCode(str, Enum):
    CONTRACTUAL = "CO"
    PATIENT_RESPONSIBILITY = "PR"
    OTHER_ADJUSTMENT = "OA"
    PAYER_INITIATED = "PI"
    CORRECTION_REVERSAL = "CR"


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------


class ClaimAdjustment(BaseModel):
    """A single adjustment from a CAS segment (claim-level)."""
    group_code: str = Field(..., description="CO, PR, OA, PI, or CR")
    reason_code: str = Field(..., description="CARC code")
    amount: Decimal
    quantity: Optional[int] = None


class ServiceAdjustment(ClaimAdjustment):
    """CAS segment at service-line level. Same shape; different scope."""


# ---------------------------------------------------------------------------
# Claim structures (parser output)
# ---------------------------------------------------------------------------


class ServiceLine(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    line_seq: int
    procedure_code: str
    modifiers: list[str] = Field(default_factory=list)
    revenue_code: Optional[str] = None
    billed_amount: Decimal = Decimal("0")
    allowed_amount: Decimal = Decimal("0")
    paid_amount: Decimal = Decimal("0")
    units: int = 1
    date_of_service: Optional[date] = None
    adjustments: list[ServiceAdjustment] = Field(default_factory=list)


class ParsedClaim(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    file_type: FileType
    file_id: Optional[str] = None  # upload batch id
    payer_name: Optional[str] = None
    payer_id: Optional[str] = None
    provider_npi: Optional[str] = None
    provider_name: Optional[str] = None
    claim_id: str                   # CLP01
    patient_name: Optional[str] = None
    patient_id: Optional[str] = None
    date_of_service: Optional[date] = None
    claim_status: Optional[str] = None
    billed_amount: Decimal = Decimal("0")
    allowed_amount: Decimal = Decimal("0")
    paid_amount: Decimal = Decimal("0")
    patient_responsibility: Decimal = Decimal("0")
    claim_filing_indicator: Optional[str] = None
    principal_diagnosis: Optional[str] = None
    other_diagnoses: list[str] = Field(default_factory=list)
    service_lines: list[ServiceLine] = Field(default_factory=list)
    adjustments: list[ClaimAdjustment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------


class ProviderType(str, Enum):
    DTC = "dtc"            # Diagnostic and Treatment Center / freestanding clinic
    HOSPITAL = "hospital"  # Hospital outpatient department


class Region(str, Enum):
    UPSTATE = "Upstate"
    DOWNSTATE = "Downstate"


class ProviderConfigIn(BaseModel):
    """Payload for POST /api/config/provider."""
    provider_name: str
    npi: Optional[str] = None
    county_code: Optional[int] = None
    peer_group: str                 # e.g. 'Clinic*'
    provider_type: ProviderType
    capital_addon_eligible: bool = False
    capital_addon_rate: Optional[Decimal] = None
    rate_code_override: Optional[str] = None
    cms_locality: Optional[str] = None


class ProviderConfigOut(ProviderConfigIn):
    id: int
    region: Optional[Region] = None  # derived from county


# ---------------------------------------------------------------------------
# APG engine outputs
# ---------------------------------------------------------------------------


class EapgType(str, Enum):
    SIGNIFICANT_PROCEDURE = "Significant Procedure"
    MEDICAL_VISIT = "Medical Visit"
    ANCILLARY = "Ancillary"
    INCIDENTAL = "Incidental"
    ADD_ON = "Add-On"
    UNKNOWN = "Unknown"


class APGLineResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    line_seq: int
    procedure_code: str
    modifiers: list[str] = Field(default_factory=list)
    eapg: Optional[int] = None
    eapg_desc: Optional[str] = None
    eapg_type: EapgType = EapgType.UNKNOWN
    eapg_category: Optional[str] = None
    weight: Optional[Decimal] = None
    base_rate: Decimal
    expected_payment: Decimal = Decimal("0")
    actual_paid: Decimal = Decimal("0")
    variance: Decimal = Decimal("0")
    # Explain what happened
    packaged: bool = False
    discounted: bool = False              # paid at 50% due to multi-procedure rule
    u6_applied: bool = False
    denied: bool = False
    notes: list[str] = Field(default_factory=list)


class APGResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    claim_id: str
    date_of_service: Optional[date] = None
    peer_group: str
    region: Region
    base_rate_applied: Decimal
    correct_apg_payment: Decimal
    actual_paid: Decimal
    variance: Decimal
    compression_pct: Decimal
    underpaid: bool
    overpaid: bool
    discounting_applied: bool
    u6_applied: bool
    capital_applied: bool
    capital_addon_amount: Decimal = Decimal("0")
    line_details: list[APGLineResult]
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Reference lookup outputs
# ---------------------------------------------------------------------------


class HcpcsLookupOut(BaseModel):
    hcpcs: str
    description: Optional[str] = None
    eapg: int
    eapg_desc: Optional[str] = None
    eapg_type: Optional[str] = None
    eapg_category: Optional[str] = None
    quarter_effective_date: Optional[date] = None
    quarter_end_date: Optional[date] = None


class Icd10LookupOut(BaseModel):
    dx_code: str
    description: Optional[str] = None
    gender: Optional[str] = None
    eapg: int
    eapg_desc: Optional[str] = None
    eapg_type: Optional[str] = None
    effective_date: Optional[date] = None


class ApgWeightLookupOut(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    apg: int
    weight: Decimal
    effective_date: date
    is_final_rate: bool
    year_rate: Optional[int] = None


class BaseRateLookupOut(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    source: str
    peer_group: str
    region: Region
    effective_date: date
    rate: Decimal


# ---------------------------------------------------------------------------
# Analytics inputs (Phase 4)
# ---------------------------------------------------------------------------


class AnalyticsFilter(BaseModel):
    """Common filter payload accepted on every analytics endpoint."""
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    payer_name: Optional[str] = None
    file_type: Optional[str] = None  # '835I' | '835P' | '837I' | '837P'
    provider_npi: Optional[str] = None


# ---------------------------------------------------------------------------
# Export payload (Phase 4)
# ---------------------------------------------------------------------------


class ExportOptions(BaseModel):
    """POST body for /api/export/excel and /api/export/pdf."""
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    payer_name: Optional[str] = None
    include_835i: bool = True
    include_835p: bool = True
    # Soft cap on per-claim PDF detail pages; the Excel report always has all claims.
    pdf_max_claims: int = Field(default=25, ge=1, le=500)


# ---------------------------------------------------------------------------
# Auth (Phase 5)
# ---------------------------------------------------------------------------


class UserRole(str, Enum):
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    """Response of /api/auth/login."""
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: "UserOut"


class UserOut(BaseModel):
    """Public-facing user representation. Never includes password hash."""
    id: int
    username: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: UserRole
    disabled: bool = False
    created_at: datetime
    last_login_at: Optional[datetime] = None


class UserCreate(BaseModel):
    """Admin-only payload for POST /api/admin/users."""
    username: str = Field(..., min_length=3, max_length=64)
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: UserRole = UserRole.ANALYST
    password: str = Field(..., min_length=10, max_length=128)


class UserUpdate(BaseModel):
    """Admin payload for PATCH /api/admin/users/{id}. All fields optional."""
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[UserRole] = None
    disabled: Optional[bool] = None


class PasswordChangeIn(BaseModel):
    """Self-service password change. Requires the current password to prevent
    a stolen session from pivoting to full account takeover."""
    current_password: str
    new_password: str = Field(..., min_length=10, max_length=128)


class AdminPasswordResetIn(BaseModel):
    """Admin resetting another user's password; no current-password check."""
    new_password: str = Field(..., min_length=10, max_length=128)


# Resolve forward refs (TokenOut.user)
TokenOut.model_rebuild()


# ---------------------------------------------------------------------------
# Rate Calculator (Phase 8) — manual CPT/ICD entry instead of EDI upload
# ---------------------------------------------------------------------------


class CalculationTarget(str, Enum):
    APG = "apg"
    CMS = "cms"
    BOTH = "both"


class CalculatorLineIn(BaseModel):
    """A single service line typed into the calculator form."""
    procedure_code: str = Field(..., min_length=1, max_length=12)
    modifiers: list[str] = Field(default_factory=list, max_length=4)
    units: int = Field(default=1, ge=1, le=999)
    billed_amount: Optional[Decimal] = None   # optional; drives the variance column if present


class CalculatorIn(BaseModel):
    """POST body for /api/calculator/calculate."""
    date_of_service: date
    service_lines: list[CalculatorLineIn] = Field(..., min_length=1, max_length=50)
    principal_diagnosis: Optional[str] = None
    other_diagnoses: list[str] = Field(default_factory=list, max_length=24)
    target: CalculationTarget = CalculationTarget.BOTH
    # CMS-specific overrides (otherwise pulled from the active provider config)
    cms_locality: Optional[str] = None
    cms_use_facility_rate: bool = False


class CalculatorLineCMS(BaseModel):
    """CMS MPFS result for a single line."""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    non_facility_rate: Optional[Decimal] = None
    facility_rate: Optional[Decimal] = None
    work_rvu: Optional[Decimal] = None
    pe_rvu: Optional[Decimal] = None
    mp_rvu: Optional[Decimal] = None
    total_rvu: Optional[Decimal] = None
    conversion_factor: Optional[Decimal] = None
    expected_payment: Optional[Decimal] = None   # facility or non_facility depending on flag
    error: Optional[str] = None                  # e.g. "no rate found" / "locality missing"


class CalculatorOut(BaseModel):
    """POST response: the full APG calculation (if requested) plus per-line CMS
    comparisons (if requested). Either or both may be null depending on target."""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    date_of_service: date
    target: CalculationTarget
    apg: Optional[APGResult] = None
    cms_locality_used: Optional[str] = None
    cms_lines: Optional[list[CalculatorLineCMS]] = None
    warnings: list[str] = Field(default_factory=list)
