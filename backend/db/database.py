"""SQLAlchemy async database setup, ORM models for reference + operational tables.

Phase 1 — covers:
    Reference data (from NYS DOH workbook):
        - hcpcs_to_eapg
        - icd10_to_eapg
        - apg_weights          (long form: apg × effective_date)
        - apg_base_rates       (long form: peer_group × region × effective_date,
                                with source='dtc' or source='hospital')
        - provider_county
    Operational:
        - provider_config      (one active provider at a time)
        - parsed_claim         (one row per 835 CLP segment)
        - parsed_service_line  (children of parsed_claim)
        - claim_adjustment     (CAS segments, claim- and line-level)
        - apg_result           (cached APG calculation per claim)
"""
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import AsyncGenerator, Optional

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Engine / session factory
# ---------------------------------------------------------------------------

# Default SQLite path is resolved relative to the `backend/` package directory
# so it does not depend on the caller's CWD (running `python -m backend.db.init_db`
# from the repo root and running uvicorn from `backend/` must produce the same file).
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DB_FILE = _BACKEND_DIR / "data" / "apg_analyzer.db"
_DEFAULT_DB = f"sqlite+aiosqlite:///{_DEFAULT_DB_FILE.as_posix()}"
DATABASE_URL = os.getenv("DATABASE_URL", _DEFAULT_DB)

# Ensure the parent dir exists when using a file-backed SQLite URL.
if DATABASE_URL.startswith("sqlite"):
    # naive extraction; acceptable for a local-only tool
    db_path_part = DATABASE_URL.split(":///", 1)[-1]
    if db_path_part and db_path_part != ":memory:":
        Path(db_path_part).parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async session per request."""
    async with async_session() as session:
        yield session


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Reference tables
# ---------------------------------------------------------------------------


class HcpcsToEapg(Base):
    """Maps a HCPCS/CPT procedure code to its EAPG assignment, date-bounded."""
    __tablename__ = "hcpcs_to_eapg"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hcpcs: Mapped[str] = mapped_column(String(12), index=True)
    description: Mapped[Optional[str]] = mapped_column(String(512))
    eapg: Mapped[int] = mapped_column(Integer, index=True)
    eapg_desc: Mapped[Optional[str]] = mapped_column(String(512))
    eapg_type: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    eapg_category: Mapped[Optional[str]] = mapped_column(String(256))
    eapg_service_line: Mapped[Optional[str]] = mapped_column(String(32))
    quarter_effective_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    quarter_end_date: Mapped[Optional[date]] = mapped_column(Date)
    mid_quarter_effective_date: Mapped[Optional[date]] = mapped_column(Date)
    mid_quarter_end_date: Mapped[Optional[date]] = mapped_column(Date)

    __table_args__ = (
        Index("ix_hcpcs_eapg_code_date", "hcpcs", "quarter_effective_date"),
    )


class Icd10ToEapg(Base):
    """Maps an ICD-10 diagnosis code to its EAPG assignment, date-bounded."""
    __tablename__ = "icd10_to_eapg"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dx_code: Mapped[str] = mapped_column(String(12), index=True)
    description: Mapped[Optional[str]] = mapped_column(String(512))
    gender: Mapped[Optional[str]] = mapped_column(String(4))   # '0', 'M', 'F' from source
    eapg: Mapped[int] = mapped_column(Integer, index=True)
    eapg_desc: Mapped[Optional[str]] = mapped_column(String(512))
    eapg_type: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    eapg_category: Mapped[Optional[str]] = mapped_column(String(256))
    eapg_service_line: Mapped[Optional[str]] = mapped_column(String(32))
    effective_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date)

    __table_args__ = (
        Index("ix_icd10_eapg_code_date", "dx_code", "effective_date"),
    )


class ApgWeight(Base):
    """APG weight by effective date, long form.

    Each row is one weight value for (apg_code, effective_date). Use the most-recent
    effective_date <= DOS at query time. `final_rate` applies when `year_rate >= year_of_dos`.
    """
    __tablename__ = "apg_weights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apg: Mapped[int] = mapped_column(Integer, index=True)
    apg_description: Mapped[Optional[str]] = mapped_column(String(512))
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    weight: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    # final_rate / year_rate are captured on the "final_rate" effective_date row,
    # which uses a sentinel date of 9999-12-31 so they sort last. See init_db.py.
    is_final_rate: Mapped[bool] = mapped_column(default=False)
    year_rate: Mapped[Optional[int]] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("apg", "effective_date", name="uq_apg_weight_date"),
    )


class ApgBaseRate(Base):
    """APG base rate, long form. Covers both DTC and Hospital tables.

    source     -- 'dtc' (freestanding) or 'hospital'
    peer_group -- e.g. 'Clinic*', 'Amb Surg', 'Renal', 'SBHC*', 'Emergency Department (ED)'
    region     -- 'Upstate' or 'Downstate'
    effective_date -- rate effective date
    rate       -- dollars (Decimal)
    """
    __tablename__ = "apg_base_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), index=True)  # 'dtc' | 'hospital'
    peer_group: Mapped[str] = mapped_column(String(64), index=True)
    cure_code: Mapped[Optional[str]] = mapped_column(String(32))
    base_rate_code: Mapped[Optional[str]] = mapped_column(String(32))
    blend_rate_code: Mapped[Optional[str]] = mapped_column(String(32))
    capital_rate_code: Mapped[Optional[str]] = mapped_column(String(32))
    region: Mapped[str] = mapped_column(String(16), index=True)
    cheat_flag: Mapped[Optional[str]] = mapped_column(String(32))  # hospital-only "cheat" col
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 4))

    __table_args__ = (
        Index("ix_base_rate_lookup", "source", "peer_group", "region", "effective_date"),
    )


class ProviderCounty(Base):
    """NY county → Upstate/Downstate region mapping (drives base-rate selection)."""
    __tablename__ = "provider_county"

    county_code: Mapped[int] = mapped_column(Integer, primary_key=True)
    county_name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    health_home_phase: Mapped[Optional[str]] = mapped_column(String(32))
    region: Mapped[str] = mapped_column(String(16))


# ---------------------------------------------------------------------------
# Operational tables
# ---------------------------------------------------------------------------


class ProviderConfig(Base):
    """Provider configuration used by the APG engine.

    Phase 1 stores a single active provider row (the one currently being analyzed).
    Later phases can support multi-provider mode.
    """
    __tablename__ = "provider_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    is_active: Mapped[bool] = mapped_column(default=True, index=True)

    provider_name: Mapped[str] = mapped_column(String(128))
    npi: Mapped[Optional[str]] = mapped_column(String(16))
    county_code: Mapped[Optional[int]] = mapped_column(Integer)
    region: Mapped[Optional[str]] = mapped_column(String(16))   # auto-derived from county
    peer_group: Mapped[str] = mapped_column(String(64))          # e.g. 'Clinic*'
    provider_type: Mapped[str] = mapped_column(String(16))       # 'dtc' | 'hospital'
    capital_addon_eligible: Mapped[bool] = mapped_column(default=False)
    capital_addon_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    rate_code_override: Mapped[Optional[str]] = mapped_column(String(16))
    cms_locality: Mapped[Optional[str]] = mapped_column(String(16))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )


class ParsedClaim(Base):
    __tablename__ = "parsed_claim"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(String(64), index=True)    # upload batch id
    file_type: Mapped[str] = mapped_column(String(8), index=True)   # '835I' | '835P' | '837I' | '837P'
    payer_name: Mapped[Optional[str]] = mapped_column(String(128))
    payer_id: Mapped[Optional[str]] = mapped_column(String(32))
    provider_npi: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    provider_name: Mapped[Optional[str]] = mapped_column(String(128))
    claim_id: Mapped[str] = mapped_column(String(64), index=True)   # CLP01
    patient_name: Mapped[Optional[str]] = mapped_column(String(128))
    patient_id: Mapped[Optional[str]] = mapped_column(String(64))
    date_of_service: Mapped[Optional[date]] = mapped_column(Date, index=True)
    claim_status: Mapped[Optional[str]] = mapped_column(String(8))  # CLP02
    billed_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    allowed_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    patient_responsibility: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    claim_filing_indicator: Mapped[Optional[str]] = mapped_column(String(4))  # CLP09
    principal_diagnosis: Mapped[Optional[str]] = mapped_column(String(16))
    other_diagnoses: Mapped[Optional[dict]] = mapped_column(JSON)   # list[str]

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    service_lines: Mapped[list["ParsedServiceLine"]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", lazy="selectin",
    )
    adjustments: Mapped[list["ClaimAdjustment"]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", lazy="selectin",
    )
    apg_result: Mapped[Optional["ApgResult"]] = relationship(
        back_populates="claim", uselist=False, cascade="all, delete-orphan", lazy="selectin",
    )


class ParsedServiceLine(Base):
    __tablename__ = "parsed_service_line"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    claim_id_fk: Mapped[int] = mapped_column(ForeignKey("parsed_claim.id", ondelete="CASCADE"), index=True)
    line_seq: Mapped[int] = mapped_column(Integer)
    procedure_code: Mapped[str] = mapped_column(String(12), index=True)
    modifiers: Mapped[Optional[dict]] = mapped_column(JSON)      # list[str]
    revenue_code: Mapped[Optional[str]] = mapped_column(String(8))
    billed_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    allowed_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    units: Mapped[int] = mapped_column(Integer, default=1)
    date_of_service: Mapped[Optional[date]] = mapped_column(Date)

    claim: Mapped[ParsedClaim] = relationship(back_populates="service_lines")


class ClaimAdjustment(Base):
    """CAS segments — both claim-level (line_seq is null) and line-level."""
    __tablename__ = "claim_adjustment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    claim_id_fk: Mapped[int] = mapped_column(ForeignKey("parsed_claim.id", ondelete="CASCADE"), index=True)
    line_seq: Mapped[Optional[int]] = mapped_column(Integer)  # null = claim-level adjustment
    group_code: Mapped[str] = mapped_column(String(4))        # CO, PR, OA, PI, CR
    reason_code: Mapped[str] = mapped_column(String(8))       # CARC code
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    quantity: Mapped[Optional[int]] = mapped_column(Integer)

    claim: Mapped[ParsedClaim] = relationship(back_populates="adjustments")


class ApgResult(Base):
    """Cached APG calculation for a parsed claim (refreshed on reprocess)."""
    __tablename__ = "apg_result"

    claim_id_fk: Mapped[int] = mapped_column(ForeignKey("parsed_claim.id", ondelete="CASCADE"), primary_key=True)
    correct_apg_payment: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    actual_paid: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    variance: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    compression_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    underpaid: Mapped[bool] = mapped_column(default=False)
    overpaid: Mapped[bool] = mapped_column(default=False)
    base_rate_applied: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    peer_group: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(16))
    discounting_applied: Mapped[bool] = mapped_column(default=False)
    u6_applied: Mapped[bool] = mapped_column(default=False)
    capital_applied: Mapped[bool] = mapped_column(default=False)
    line_details: Mapped[dict] = mapped_column(JSON)  # serialized APGLineResult list
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    claim: Mapped[ParsedClaim] = relationship(back_populates="apg_result")
