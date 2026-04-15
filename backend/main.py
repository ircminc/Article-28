"""FastAPI application entry point for the APG 835/837 Rate Analyzer.

Phase 1 + Phase 2 endpoints:

  Uploads
    POST   /api/upload/835i              Upload one or more 835I files
    POST   /api/upload/835p              Upload one or more 835P files
    POST   /api/upload/837               Upload one or more 837I/P files (auto-detected)

  Claims
    GET    /api/claims                   List parsed claims (paginated)
    GET    /api/claims/{claim_pk}        Claim detail + APG result + linked claim
    GET    /api/claims/{claim_pk}/apg    APG calculation breakdown

  Reference lookups (Article 28)
    GET    /api/reference/hcpcs/{code}   HCPCS → EAPG lookup (date-aware)
    GET    /api/reference/icd10/{code}   ICD-10 → EAPG lookup (date-aware)
    GET    /api/reference/apg/{apg}      APG weight lookup (date-aware)
    GET    /api/reference/base-rates     Base-rate listing (filterable)

  Reference lookups (CMS)
    GET    /api/reference/cms/{code}     CMS MPFS rate (cached, date-aware)
    GET    /api/reference/zip-locality/{zip}   ZIP → Medicare locality

  Provider config
    POST   /api/config/provider          Upsert provider configuration
    GET    /api/config/provider          Get active provider configuration
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import Decimal
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
    ZipLocality,
    async_session,
    engine,
    get_session,
)
from backend.engines.apg_engine import APGEngine
from backend.engines.claim_linker import link_and_enrich
from backend.engines.cms_engine import CMSFeeScheduleEngine
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
from backend.parsers.edi_835p import parse_835p
from backend.parsers.edi_837 import parse_837

log = logging.getLogger("apg_analyzer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s  %(message)s")


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="APG 835/837 Rate Analyzer",
    version=__version__,
    description=(
        "NYS Medicaid Article 28 APG reimbursement analysis. "
        "Phase 2: 835I + 835P + 837 parsers, APG engine, CMS MPFS engine."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_cms_engine: Optional[CMSFeeScheduleEngine] = None


@app.on_event("startup")
async def _startup() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    global _cms_engine
    _cms_engine = CMSFeeScheduleEngine()


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _cms_engine
    if _cms_engine is not None:
        await _cms_engine.aclose()
        _cms_engine = None


def get_cms_engine() -> CMSFeeScheduleEngine:
    global _cms_engine
    if _cms_engine is None:
        _cms_engine = CMSFeeScheduleEngine()
    return _cms_engine


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
        ("zip_locality", ZipLocality),
    ]:
        pk = cls.id if hasattr(cls, "id") else cls.county_code
        r = await session.execute(select(pk).limit(1))
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
# Shared persistence + APG calculation
# ---------------------------------------------------------------------------


async def _require_provider(session: AsyncSession) -> ProviderConfig:
    q = await session.execute(
        select(ProviderConfig).where(ProviderConfig.is_active.is_(True)).limit(1)
    )
    provider = q.scalar_one_or_none()
    if provider is None:
        raise HTTPException(400, "No active provider configured. POST /api/config/provider first.")
    return provider


async def _persist_parsed_claim(session, dto, parsed_envelope, batch_id: str) -> ORMClaim:
    """Convert a Pydantic ParsedClaim DTO to an ORM row + child rows.

    `parsed_envelope` supplies payer info common to all claims in the file;
    it can be either Parsed835I / Parsed835P / Parsed837.
    """
    payer_name = getattr(parsed_envelope, "payer_name", None) or None
    payer_id = getattr(parsed_envelope, "payer_id", None) or None
    orm = ORMClaim(
        file_id=batch_id,
        file_type=dto.file_type.value,
        payer_name=payer_name,
        payer_id=payer_id,
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
    orm.service_lines = [
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
    orm.adjustments = [
        ORMClaimAdjustment(
            line_seq=None,
            group_code=a.group_code,
            reason_code=a.reason_code,
            amount=a.amount,
            quantity=a.quantity,
        )
        for a in dto.adjustments
    ]
    session.add(orm)
    await session.flush()
    return orm


async def _run_apg_and_persist(session, dto, orm_claim, provider) -> None:
    """Run the APG engine and attach the result via the ORM relationship.

    Assigning through `orm_claim.apg_result = ...` (rather than session.add on
    a bare ORMApgResult) keeps the in-memory relationship consistent, which
    matters later when link_and_enrich re-fetches the claim and wants to update
    its apg_result in place.
    """
    engine_apg = APGEngine()
    apg = await engine_apg.calculate(session, dto, provider)
    orm_claim.apg_result = ORMApgResult(
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


# ---------------------------------------------------------------------------
# Upload endpoints
# ---------------------------------------------------------------------------


@app.post("/api/upload/835i")
async def upload_835i(
    files: list[UploadFile] = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    provider = await _require_provider(session)
    results, total_claims, all_claim_ids = [], 0, []

    for up in files:
        batch_id = uuid.uuid4().hex[:12]
        raw = (await up.read()).decode("utf-8", errors="replace")
        try:
            parsed = parse_835i(raw)
        except Exception as e:
            results.append({"file": up.filename, "error": str(e)})
            continue
        summary = {
            "file": up.filename, "file_id": batch_id,
            "claims_parsed": len(parsed.claims),
            "payer": parsed.payer_name, "payee": parsed.payee_name,
            "payment_amount": str(parsed.payment_amount), "claim_ids": [],
        }
        for dto in parsed.claims:
            dto.file_id = batch_id
            orm = await _persist_parsed_claim(session, dto, parsed, batch_id)
            await _run_apg_and_persist(session, dto, orm, provider)
            summary["claim_ids"].append(dto.claim_id)
            all_claim_ids.append(dto.claim_id)
            total_claims += 1
        await session.commit()
        results.append(summary)

    enrichment = await link_and_enrich(session, all_claim_ids, provider)
    await session.commit()
    return {
        "files_processed": len(files), "total_claims": total_claims,
        "results": results, "enrichment": enrichment,
    }


@app.post("/api/upload/835p")
async def upload_835p(
    files: list[UploadFile] = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    provider = await _require_provider(session)
    results, total_claims, all_claim_ids = [], 0, []

    for up in files:
        batch_id = uuid.uuid4().hex[:12]
        raw = (await up.read()).decode("utf-8", errors="replace")
        try:
            parsed = parse_835p(raw)
        except Exception as e:
            results.append({"file": up.filename, "error": str(e)})
            continue
        summary = {
            "file": up.filename, "file_id": batch_id,
            "claims_parsed": len(parsed.claims),
            "payer": parsed.payer_name, "payee": parsed.payee_name,
            "payment_amount": str(parsed.payment_amount), "claim_ids": [],
        }
        for dto in parsed.claims:
            dto.file_id = batch_id
            orm = await _persist_parsed_claim(session, dto, parsed, batch_id)
            # 835P claims don't get APG calc (they're non-Article 28) — CMS comparison
            # will be available on-demand via /api/reference/cms/{code}.
            summary["claim_ids"].append(dto.claim_id)
            all_claim_ids.append(dto.claim_id)
            total_claims += 1
        await session.commit()
        results.append(summary)

    enrichment = await link_and_enrich(session, all_claim_ids, provider)
    await session.commit()
    return {
        "files_processed": len(files), "total_claims": total_claims,
        "results": results, "enrichment": enrichment,
    }


@app.post("/api/upload/837")
async def upload_837(
    files: list[UploadFile] = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Upload 837 claim files (institutional or professional; auto-detected).

    After persisting the claims, the claim-linker runs to enrich any matching
    835 remittances with diagnosis codes from these 837 submissions and
    re-compute APG on the enriched records.
    """
    provider = await _require_provider(session)
    results, total_claims, all_claim_ids = [], 0, []

    for up in files:
        batch_id = uuid.uuid4().hex[:12]
        raw = (await up.read()).decode("utf-8", errors="replace")
        try:
            parsed = parse_837(raw)
        except Exception as e:
            results.append({"file": up.filename, "error": str(e)})
            continue
        summary = {
            "file": up.filename, "file_id": batch_id,
            "file_type": parsed.file_type.value,
            "claims_parsed": len(parsed.claims),
            "submitter": parsed.submitter_name,
            "billing_provider": parsed.billing_provider_name,
            "claim_ids": [],
        }
        for dto in parsed.claims:
            dto.file_id = batch_id
            orm = await _persist_parsed_claim(session, dto, parsed, batch_id)
            summary["claim_ids"].append(dto.claim_id)
            all_claim_ids.append(dto.claim_id)
            total_claims += 1
        await session.commit()
        results.append(summary)

    enrichment = await link_and_enrich(session, all_claim_ids, provider)
    await session.commit()
    return {
        "files_processed": len(files), "total_claims": total_claims,
        "results": results, "enrichment": enrichment,
    }


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
        "count": len(claims), "offset": offset, "limit": limit,
        "items": [
            {
                "id": c.id, "claim_id": c.claim_id, "file_type": c.file_type,
                "date_of_service": c.date_of_service.isoformat() if c.date_of_service else None,
                "provider_npi": c.provider_npi, "patient_name": c.patient_name,
                "billed_amount": str(c.billed_amount), "paid_amount": str(c.paid_amount),
                "claim_status": c.claim_status,
                "linked_claim_id_fk": c.linked_claim_id_fk,
            }
            for c in claims
        ],
    }


@app.get("/api/claims/{claim_pk}")
async def get_claim(claim_pk: int, session: AsyncSession = Depends(get_session)) -> dict:
    claim = await session.get(ORMClaim, claim_pk)
    if claim is None:
        raise HTTPException(404, "Claim not found")

    linked = None
    if claim.linked_claim_id_fk:
        linked_claim = await session.get(ORMClaim, claim.linked_claim_id_fk)
        if linked_claim:
            linked = {
                "id": linked_claim.id,
                "file_type": linked_claim.file_type,
                "claim_id": linked_claim.claim_id,
                "billed_amount": str(linked_claim.billed_amount),
                "paid_amount": str(linked_claim.paid_amount),
            }

    return {
        "id": claim.id, "file_id": claim.file_id, "file_type": claim.file_type,
        "payer_name": claim.payer_name, "payer_id": claim.payer_id,
        "provider_npi": claim.provider_npi, "provider_name": claim.provider_name,
        "claim_id": claim.claim_id, "patient_name": claim.patient_name,
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
        "linked_claim": linked,
        "service_lines": [
            {
                "line_seq": sl.line_seq, "procedure_code": sl.procedure_code,
                "modifiers": sl.modifiers or [], "revenue_code": sl.revenue_code,
                "billed_amount": str(sl.billed_amount), "allowed_amount": str(sl.allowed_amount),
                "paid_amount": str(sl.paid_amount), "units": sl.units,
                "date_of_service": sl.date_of_service.isoformat() if sl.date_of_service else None,
            }
            for sl in claim.service_lines
        ],
        "adjustments": [
            {"group_code": a.group_code, "reason_code": a.reason_code,
             "amount": str(a.amount), "quantity": a.quantity}
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
        "underpaid": apg.underpaid, "overpaid": apg.overpaid,
        "base_rate_applied": str(apg.base_rate_applied),
        "peer_group": apg.peer_group, "region": apg.region,
        "discounting_applied": apg.discounting_applied,
        "u6_applied": apg.u6_applied, "capital_applied": apg.capital_applied,
        "line_details": apg.line_details,
    }


# ---------------------------------------------------------------------------
# Reference lookups — Article 28
# ---------------------------------------------------------------------------


@app.get("/api/reference/hcpcs/{code}", response_model=HcpcsLookupOut)
async def lookup_hcpcs(
    code: str, dos: date = Query(...),
    session: AsyncSession = Depends(get_session),
) -> HcpcsLookupOut:
    apg = APGEngine()
    row = await apg.lookup_hcpcs_eapg(session, code.upper().strip(), dos)
    if row is None:
        raise HTTPException(404, f"No EAPG mapping for HCPCS {code} on {dos.isoformat()}")
    return HcpcsLookupOut(
        hcpcs=row.hcpcs, description=row.description, eapg=row.eapg,
        eapg_desc=row.eapg_desc, eapg_type=row.eapg_type, eapg_category=row.eapg_category,
        quarter_effective_date=row.quarter_effective_date, quarter_end_date=row.quarter_end_date,
    )


@app.get("/api/reference/icd10/{code}", response_model=Icd10LookupOut)
async def lookup_icd10(
    code: str, dos: date = Query(...),
    session: AsyncSession = Depends(get_session),
) -> Icd10LookupOut:
    apg = APGEngine()
    row = await apg.lookup_icd10_eapg(session, code.upper().strip(), dos)
    if row is None:
        raise HTTPException(404, f"No EAPG mapping for DX {code} on {dos.isoformat()}")
    return Icd10LookupOut(
        dx_code=row.dx_code, description=row.description, gender=row.gender,
        eapg=row.eapg, eapg_desc=row.eapg_desc, eapg_type=row.eapg_type,
        effective_date=row.effective_date,
    )


@app.get("/api/reference/apg/{apg_code}", response_model=ApgWeightLookupOut)
async def lookup_apg_weight(
    apg_code: int, dos: date = Query(...),
    session: AsyncSession = Depends(get_session),
) -> ApgWeightLookupOut:
    apg = APGEngine()
    row = await apg.lookup_apg_weight(session, apg_code, dos)
    if row is None:
        raise HTTPException(404, f"No weight history for APG {apg_code} on {dos.isoformat()}")
    return ApgWeightLookupOut(
        apg=row.apg, weight=row.weight, effective_date=row.effective_date,
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


# ---------------------------------------------------------------------------
# Reference lookups — CMS
# ---------------------------------------------------------------------------


@app.get("/api/reference/cms/{code}")
async def lookup_cms_rate(
    code: str,
    dos: date = Query(..., description="Date of service; year is used for the rate lookup"),
    modifier: str = Query("", description="2-char modifier, optional"),
    locality: Optional[str] = Query(None, description="CMS locality number; defaults to provider config"),
    force_refresh: bool = Query(False, description="Ignore cache and refetch from CMS API"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Resolve locality from provider config if not supplied
    if not locality:
        q = await session.execute(
            select(ProviderConfig).where(ProviderConfig.is_active.is_(True)).limit(1)
        )
        provider = q.scalar_one_or_none()
        if provider is None or not provider.cms_locality:
            raise HTTPException(
                400,
                "No locality supplied and active provider has no cms_locality set. "
                "Either pass ?locality=... or set it in the provider config.",
            )
        locality = provider.cms_locality

    cms = get_cms_engine()
    row = await cms.get_mpfs_rate(session, code, modifier, locality, dos.year, force_refresh=force_refresh)
    if row is None:
        raise HTTPException(
            404,
            f"No CMS MPFS rate found for HCPCS {code}, modifier {modifier!r}, "
            f"locality {locality}, year {dos.year}. "
            "Verify the code exists in the MPFS and that the locality number is correct.",
        )

    return {
        "hcpcs": row.hcpcs, "modifier": row.modifier, "locality": row.locality, "year": row.year,
        "non_facility_rate": str(row.non_facility_rate) if row.non_facility_rate is not None else None,
        "facility_rate": str(row.facility_rate) if row.facility_rate is not None else None,
        "work_rvu": str(row.work_rvu) if row.work_rvu is not None else None,
        "pe_rvu": str(row.pe_rvu) if row.pe_rvu is not None else None,
        "mp_rvu": str(row.mp_rvu) if row.mp_rvu is not None else None,
        "total_rvu": str(row.total_rvu) if row.total_rvu is not None else None,
        "conversion_factor": str(row.conversion_factor) if row.conversion_factor is not None else None,
        "cached_at": row.cached_at.isoformat() + "Z",
        "cached_until": row.cached_until.isoformat() + "Z",
    }


@app.get("/api/reference/zip-locality/{zip_code}")
async def lookup_zip_locality(
    zip_code: str,
    year: Optional[int] = Query(None, description="Year for the lookup (most recent if omitted)"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    cms = get_cms_engine()
    locality = await cms.get_locality_from_zip(session, zip_code, year=year)
    if locality is None:
        raise HTTPException(
            404,
            f"No locality found for ZIP {zip_code}. "
            "The zip_locality table may be empty; run `python -m backend.db.init_zip_locality "
            "--file <path>` to load the CMS ZIP5 file.",
        )
    return {"zip_code": cms.normalize_zip(zip_code), "locality": locality}
