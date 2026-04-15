"""FastAPI application entry point for the APG 835/837 Rate Analyzer.

Phase 1 endpoints:
    POST   /api/upload/835i              Upload one or more 835I files
    GET    /api/claims                   List parsed claims (paginated)
    GET    /api/claims/{claim_pk}        Claim detail + APG result
    GET    /api/claims/{claim_pk}/apg    APG calculation breakdown
    GET    /api/reference/hcpcs/{code}   HCPCS → EAPG lookup (date-aware)
    GET    /api/reference/icd10/{code}   ICD-10 → EAPG lookup (date-aware)
    GET    /api/reference/apg/{apg}      APG weight lookup (date-aware)
    GET    /api/reference/base-rates     Base-rate listing (filterable)
    POST   /api/config/provider          Upsert provider configuration
    GET    /api/config/provider          Get active provider configuration

Endpoints for 835P/837 parsers, CMS fee schedule, analytics, and exporters
come in later phases.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import __version__
from backend.db.database import (
    ApgBaseRate,
    ApgResult as ORMApgResult,
    ApgWeight,
    Base,
    ClaimAdjustment as ORMClaimAdjustment,
    HcpcsToEapg,
    Icd10ToEapg,
    ParsedClaim as ORMClaim,
    ParsedServiceLine as ORMLine,
    ProviderConfig,
    ProviderCounty,
    async_session,
    engine,
    get_session,
)
from backend.engines.apg_engine import APGEngine
from backend.models.schemas import (
    APGResult as APGResultOut,
    BaseRateLookupOut,
    HcpcsLookupOut,
    Icd10LookupOut,
    ApgWeightLookupOut,
    ProviderConfigIn,
    ProviderConfigOut,
    Region,
)
from backend.parsers.edi_835i import parse_835i

log = logging.getLogger("apg_analyzer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s  %(message)s")


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="APG 835/837 Rate Analyzer",
    version=__version__,
    description="NYS Medicaid Article 28 APG reimbursement analysis — Phase 1 (835I + APG engine)",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    """Ensure tables exist on startup. Does NOT drop; safe to run every launch.

    To (re)load reference data, run `python -m backend.db.init_db --workbook <path>`
    which drops + recreates + populates.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    totals = {}
    for tbl, cls in [
        ("hcpcs_to_eapg", HcpcsToEapg),
        ("icd10_to_eapg", Icd10ToEapg),
        ("apg_weights", ApgWeight),
        ("apg_base_rates", ApgBaseRate),
        ("provider_county", ProviderCounty),
    ]:
        r = await session.execute(select(cls.id if hasattr(cls, "id") else cls.county_code).limit(1))
        totals[tbl] = "loaded" if r.first() is not None else "empty"
    return {"status": "ok", "version": __version__, "reference_data": totals}


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------


@app.post("/api/config/provider", response_model=ProviderConfigOut)
async def upsert_provider(
    payload: ProviderConfigIn,
    session: AsyncSession = Depends(get_session),
) -> ProviderConfigOut:
    """Save / replace the active provider configuration.

    Phase 1 supports a single active provider. Any existing active row is
    deactivated and a new one inserted, so history is preserved.
    """
    # Resolve region from county if provided
    region = None
    if payload.county_code:
        q = await session.execute(
            select(ProviderCounty).where(ProviderCounty.county_code == payload.county_code)
        )
        row = q.scalar_one_or_none()
        if row is None:
            raise HTTPException(404, f"Unknown county_code {payload.county_code}")
        region = row.region

    await session.execute(
        delete(ProviderConfig).where(ProviderConfig.is_active.is_(True))
    )

    cfg = ProviderConfig(
        is_active=True,
        provider_name=payload.provider_name,
        npi=payload.npi,
        county_code=payload.county_code,
        region=region,
        peer_group=payload.peer_group,
        provider_type=payload.provider_type.value,
        capital_addon_eligible=payload.capital_addon_eligible,
        capital_addon_rate=payload.capital_addon_rate,
        rate_code_override=payload.rate_code_override,
        cms_locality=payload.cms_locality,
    )
    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)
    return _provider_out(cfg)


@app.get("/api/config/provider", response_model=ProviderConfigOut)
async def get_active_provider(session: AsyncSession = Depends(get_session)) -> ProviderConfigOut:
    q = await session.execute(
        select(ProviderConfig).where(ProviderConfig.is_active.is_(True)).limit(1)
    )
    cfg = q.scalar_one_or_none()
    if cfg is None:
        raise HTTPException(404, "No active provider configuration. POST /api/config/provider first.")
    return _provider_out(cfg)


def _provider_out(cfg: ProviderConfig) -> ProviderConfigOut:
    from backend.models.schemas import ProviderType
    return ProviderConfigOut(
        id=cfg.id,
        provider_name=cfg.provider_name,
        npi=cfg.npi,
        county_code=cfg.county_code,
        region=Region(cfg.region) if cfg.region else None,
        peer_group=cfg.peer_group,
        provider_type=ProviderType(cfg.provider_type),
        capital_addon_eligible=cfg.capital_addon_eligible,
        capital_addon_rate=cfg.capital_addon_rate,
        rate_code_override=cfg.rate_code_override,
        cms_locality=cfg.cms_locality,
    )


# ---------------------------------------------------------------------------
# 835I upload
# ---------------------------------------------------------------------------


@app.post("/api/upload/835i")
async def upload_835i(
    files: list[UploadFile] = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Parse one or more 835I EDI files, persist the claims, and run APG calc.

    Returns a summary per file and total claim count ingested.
    """
    # Load active provider — required to run APG engine
    q = await session.execute(
        select(ProviderConfig).where(ProviderConfig.is_active.is_(True)).limit(1)
    )
    provider = q.scalar_one_or_none()
    if provider is None:
        raise HTTPException(400, "No active provider configured. POST /api/config/provider first.")

    engine_apg = APGEngine()
    results = []
    total_claims = 0

    for up in files:
        batch_id = uuid.uuid4().hex[:12]
        raw = (await up.read()).decode("utf-8", errors="replace")
        try:
            parsed = parse_835i(raw)
        except Exception as e:
            results.append({"file": up.filename, "error": str(e)})
            continue

        per_file_summary = {
            "file": up.filename,
            "file_id": batch_id,
            "claims_parsed": len(parsed.claims),
            "payer": parsed.payer_name,
            "payee": parsed.payee_name,
            "payment_amount": str(parsed.payment_amount),
            "claim_ids": [],
        }

        for dto in parsed.claims:
            dto.file_id = batch_id
            orm_claim = ORMClaim(
                file_id=batch_id,
                file_type=dto.file_type.value,
                payer_name=parsed.payer_name or None,
                payer_id=parsed.payer_id or None,
                provider_npi=dto.provider_npi,
                provider_name=dto.provider_name,
                claim_id=dto.claim_id,
                patient_name=dto.patient_name,
                patient_id=dto.patient_id,
                date_of_service=dto.date_of_service,
                claim_status=dto.claim_status,
                billed_amount=dto.billed_amount,
                allowed_amount=dto.allowed_amount,
                paid_amount=dto.paid_amount,
                patient_responsibility=dto.patient_responsibility,
                claim_filing_indicator=dto.claim_filing_indicator,
                principal_diagnosis=dto.principal_diagnosis,
                other_diagnoses=list(dto.other_diagnoses) if dto.other_diagnoses else [],
            )
            orm_claim.service_lines = [
                ORMLine(
                    line_seq=sl.line_seq,
                    procedure_code=sl.procedure_code,
                    modifiers=sl.modifiers,
                    revenue_code=sl.revenue_code,
                    billed_amount=sl.billed_amount,
                    allowed_amount=sl.allowed_amount,
                    paid_amount=sl.paid_amount,
                    units=sl.units,
                    date_of_service=sl.date_of_service or dto.date_of_service,
                )
                for sl in dto.service_lines
            ]
            orm_claim.adjustments = [
                ORMClaimAdjustment(
                    line_seq=None,
                    group_code=a.group_code,
                    reason_code=a.reason_code,
                    amount=a.amount,
                    quantity=a.quantity,
                )
                for a in dto.adjustments
            ]
            session.add(orm_claim)
            await session.flush()

            # Run APG calc
            apg = await engine_apg.calculate(session, dto, provider)
            orm_apg = ORMApgResult(
                claim_id_fk=orm_claim.id,
                correct_apg_payment=apg.correct_apg_payment,
                actual_paid=apg.actual_paid,
                variance=apg.variance,
                compression_pct=apg.compression_pct,
                underpaid=apg.underpaid,
                overpaid=apg.overpaid,
                base_rate_applied=apg.base_rate_applied,
                peer_group=apg.peer_group,
                region=apg.region.value,
                discounting_applied=apg.discounting_applied,
                u6_applied=apg.u6_applied,
                capital_applied=apg.capital_applied,
                line_details=[ld.model_dump(mode="json") for ld in apg.line_details],
            )
            session.add(orm_apg)
            per_file_summary["claim_ids"].append(dto.claim_id)
            total_claims += 1

        await session.commit()
        results.append(per_file_summary)

    return {"files_processed": len(files), "total_claims": total_claims, "results": results}


# ---------------------------------------------------------------------------
# Claim list + detail
# ---------------------------------------------------------------------------


@app.get("/api/claims")
async def list_claims(
    file_type: Optional[str] = Query(None, pattern="^(835I|835P|837I|837P)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict:
    stmt = select(ORMClaim).order_by(ORMClaim.created_at.desc())
    if file_type:
        stmt = stmt.where(ORMClaim.file_type == file_type)
    stmt = stmt.offset(offset).limit(limit)
    res = await session.execute(stmt)
    claims = res.scalars().all()

    return {
        "count": len(claims),
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": c.id,
                "claim_id": c.claim_id,
                "file_type": c.file_type,
                "date_of_service": c.date_of_service.isoformat() if c.date_of_service else None,
                "provider_npi": c.provider_npi,
                "patient_name": c.patient_name,
                "billed_amount": str(c.billed_amount),
                "paid_amount": str(c.paid_amount),
                "claim_status": c.claim_status,
            }
            for c in claims
        ],
    }


@app.get("/api/claims/{claim_pk}")
async def get_claim(claim_pk: int, session: AsyncSession = Depends(get_session)) -> dict:
    claim = await session.get(ORMClaim, claim_pk)
    if claim is None:
        raise HTTPException(404, "Claim not found")
    return {
        "id": claim.id,
        "file_id": claim.file_id,
        "file_type": claim.file_type,
        "payer_name": claim.payer_name,
        "payer_id": claim.payer_id,
        "provider_npi": claim.provider_npi,
        "provider_name": claim.provider_name,
        "claim_id": claim.claim_id,
        "patient_name": claim.patient_name,
        "patient_id": claim.patient_id,
        "date_of_service": claim.date_of_service.isoformat() if claim.date_of_service else None,
        "claim_status": claim.claim_status,
        "billed_amount": str(claim.billed_amount),
        "allowed_amount": str(claim.allowed_amount),
        "paid_amount": str(claim.paid_amount),
        "patient_responsibility": str(claim.patient_responsibility),
        "claim_filing_indicator": claim.claim_filing_indicator,
        "principal_diagnosis": claim.principal_diagnosis,
        "other_diagnoses": claim.other_diagnoses or [],
        "service_lines": [
            {
                "line_seq": sl.line_seq,
                "procedure_code": sl.procedure_code,
                "modifiers": sl.modifiers or [],
                "revenue_code": sl.revenue_code,
                "billed_amount": str(sl.billed_amount),
                "allowed_amount": str(sl.allowed_amount),
                "paid_amount": str(sl.paid_amount),
                "units": sl.units,
                "date_of_service": sl.date_of_service.isoformat() if sl.date_of_service else None,
            }
            for sl in claim.service_lines
        ],
        "adjustments": [
            {
                "group_code": a.group_code,
                "reason_code": a.reason_code,
                "amount": str(a.amount),
                "quantity": a.quantity,
            }
            for a in claim.adjustments
        ],
        "apg_result": (
            {
                "correct_apg_payment": str(claim.apg_result.correct_apg_payment),
                "actual_paid": str(claim.apg_result.actual_paid),
                "variance": str(claim.apg_result.variance),
                "compression_pct": str(claim.apg_result.compression_pct),
                "underpaid": claim.apg_result.underpaid,
                "overpaid": claim.apg_result.overpaid,
                "base_rate_applied": str(claim.apg_result.base_rate_applied),
                "peer_group": claim.apg_result.peer_group,
                "region": claim.apg_result.region,
                "discounting_applied": claim.apg_result.discounting_applied,
                "u6_applied": claim.apg_result.u6_applied,
                "capital_applied": claim.apg_result.capital_applied,
                "line_details": claim.apg_result.line_details,
            }
            if claim.apg_result else None
        ),
    }


@app.get("/api/claims/{claim_pk}/apg")
async def get_claim_apg(claim_pk: int, session: AsyncSession = Depends(get_session)) -> dict:
    claim = await session.get(ORMClaim, claim_pk)
    if claim is None or claim.apg_result is None:
        raise HTTPException(404, "Claim or APG result not found")
    apg = claim.apg_result
    return {
        "claim_id": claim.claim_id,
        "correct_apg_payment": str(apg.correct_apg_payment),
        "actual_paid": str(apg.actual_paid),
        "variance": str(apg.variance),
        "compression_pct": str(apg.compression_pct),
        "underpaid": apg.underpaid,
        "overpaid": apg.overpaid,
        "base_rate_applied": str(apg.base_rate_applied),
        "peer_group": apg.peer_group,
        "region": apg.region,
        "discounting_applied": apg.discounting_applied,
        "u6_applied": apg.u6_applied,
        "capital_applied": apg.capital_applied,
        "line_details": apg.line_details,
    }


# ---------------------------------------------------------------------------
# Reference lookups
# ---------------------------------------------------------------------------


@app.get("/api/reference/hcpcs/{code}", response_model=HcpcsLookupOut)
async def lookup_hcpcs(
    code: str,
    dos: date = Query(..., description="Date of service (YYYY-MM-DD) for effective-date matching"),
    session: AsyncSession = Depends(get_session),
) -> HcpcsLookupOut:
    apg = APGEngine()
    row = await apg.lookup_hcpcs_eapg(session, code.upper().strip(), dos)
    if row is None:
        raise HTTPException(404, f"No EAPG mapping for HCPCS {code} on {dos.isoformat()}")
    return HcpcsLookupOut(
        hcpcs=row.hcpcs,
        description=row.description,
        eapg=row.eapg,
        eapg_desc=row.eapg_desc,
        eapg_type=row.eapg_type,
        eapg_category=row.eapg_category,
        quarter_effective_date=row.quarter_effective_date,
        quarter_end_date=row.quarter_end_date,
    )


@app.get("/api/reference/icd10/{code}", response_model=Icd10LookupOut)
async def lookup_icd10(
    code: str,
    dos: date = Query(...),
    session: AsyncSession = Depends(get_session),
) -> Icd10LookupOut:
    apg = APGEngine()
    row = await apg.lookup_icd10_eapg(session, code.upper().strip(), dos)
    if row is None:
        raise HTTPException(404, f"No EAPG mapping for DX {code} on {dos.isoformat()}")
    return Icd10LookupOut(
        dx_code=row.dx_code,
        description=row.description,
        gender=row.gender,
        eapg=row.eapg,
        eapg_desc=row.eapg_desc,
        eapg_type=row.eapg_type,
        effective_date=row.effective_date,
    )


@app.get("/api/reference/apg/{apg_code}", response_model=ApgWeightLookupOut)
async def lookup_apg_weight(
    apg_code: int,
    dos: date = Query(...),
    session: AsyncSession = Depends(get_session),
) -> ApgWeightLookupOut:
    apg = APGEngine()
    row = await apg.lookup_apg_weight(session, apg_code, dos)
    if row is None:
        raise HTTPException(404, f"No weight history for APG {apg_code} on {dos.isoformat()}")
    return ApgWeightLookupOut(
        apg=row.apg, weight=row.weight,
        effective_date=row.effective_date,
        is_final_rate=row.is_final_rate, year_rate=row.year_rate,
    )


@app.get("/api/reference/base-rates")
async def list_base_rates(
    source: Optional[str] = Query(None, pattern="^(dtc|hospital)$"),
    peer_group: Optional[str] = Query(None),
    region: Optional[str] = Query(None, pattern="^(Upstate|Downstate)$"),
    session: AsyncSession = Depends(get_session),
) -> list[BaseRateLookupOut]:
    stmt = select(ApgBaseRate).order_by(
        ApgBaseRate.source, ApgBaseRate.peer_group, ApgBaseRate.region, ApgBaseRate.effective_date
    )
    if source:
        stmt = stmt.where(ApgBaseRate.source == source)
    if peer_group:
        stmt = stmt.where(ApgBaseRate.peer_group == peer_group)
    if region:
        stmt = stmt.where(ApgBaseRate.region == region)
    res = await session.execute(stmt)
    return [
        BaseRateLookupOut(
            source=r.source, peer_group=r.peer_group, region=Region(r.region),
            effective_date=r.effective_date, rate=r.rate,
        )
        for r in res.scalars().all()
    ]
