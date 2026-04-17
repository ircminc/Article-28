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
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from sqlalchemy import delete, func, select
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
from backend.auth_routes import admin_router, auth_router
from backend.deps import CurrentUser, RequireAdmin, RequireAnalyst, audit
from backend.engines.analytics_engine import AnalyticsEngine
from backend.engines.apg_engine import APGEngine
from backend.engines.claim_linker import link_and_enrich
from backend.engines.cms_engine import CMSFeeScheduleEngine
from backend.exporters.excel_exporter import ExcelExporter
from backend.exporters.pdf_exporter import PDFExporter
from backend.models.schemas import (
    APGResult as APGResultOut,
    BaseRateLookupOut,
    CalculationTarget,
    CalculatorIn,
    CalculatorLineCMS,
    CalculatorOut,
    ExportOptions,
    HcpcsLookupOut,
    Icd10LookupOut,
    ApgWeightLookupOut,
    ParsedClaim as ParsedClaimDTO,
    ProviderConfigIn,
    ProviderConfigOut,
    Region,
    ServiceLine as ServiceLineDTO,
    FileType,
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

# Auth + admin routes (Phase 5). Mounted early so they take priority.
app.include_router(auth_router)
app.include_router(admin_router)

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
    current: RequireAnalyst,
    request: Request,
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
    await session.flush()
    await audit(
        session, user=current, request=request,
        action="provider.update", resource=cfg.provider_name,
        details={"peer_group": cfg.peer_group, "region": cfg.region},
    )
    await session.commit()
    await session.refresh(cfg)
    return _provider_out(cfg)


@app.get("/api/config/provider", response_model=ProviderConfigOut)
async def get_active_provider(
    current: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> ProviderConfigOut:
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
    current: RequireAnalyst,
    request: Request,
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
    await audit(
        session, user=current, request=request,
        action="upload.835i",
        resource=f"{len(files)} file(s), {total_claims} claim(s)",
        details={"file_names": [up.filename for up in files]},
    )
    await session.commit()
    return {
        "files_processed": len(files), "total_claims": total_claims,
        "results": results, "enrichment": enrichment,
    }


@app.post("/api/upload/835p")
async def upload_835p(
    current: RequireAnalyst,
    request: Request,
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
    await audit(
        session, user=current, request=request,
        action="upload.835p",
        resource=f"{len(files)} file(s), {total_claims} claim(s)",
        details={"file_names": [up.filename for up in files]},
    )
    await session.commit()
    return {
        "files_processed": len(files), "total_claims": total_claims,
        "results": results, "enrichment": enrichment,
    }


@app.post("/api/upload/837")
async def upload_837(
    current: RequireAnalyst,
    request: Request,
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
    await audit(
        session, user=current, request=request,
        action="upload.837",
        resource=f"{len(files)} file(s), {total_claims} claim(s)",
        details={"file_names": [up.filename for up in files]},
    )
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
    current: CurrentUser,
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
async def get_claim(
    claim_pk: int,
    current: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
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
async def get_claim_apg(
    claim_pk: int,
    current: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
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


@app.delete("/api/admin/cms-cache", status_code=200)
async def clear_cms_cache(
    current: RequireAnalyst,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Flush the CMS MPFS rate cache. Next CMS lookup will hit the live API.

    Admin or analyst only. Useful when CMS publishes a mid-year update and
    you don't want to wait for the 24h TTL to expire naturally.
    """
    from backend.db.database import CmsRateCache
    n = (await session.execute(select(func.count()).select_from(CmsRateCache))).scalar_one()
    await session.execute(delete(CmsRateCache))
    await audit(
        session, user=current, request=request,
        action="cms_cache.clear", resource=f"{n} cached rate(s)",
    )
    await session.commit()
    return {"cached_rates_cleared": n}


@app.post("/api/admin/reload-reference-data")
async def reload_reference_data(
    current: RequireAdmin,
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Upload a new NYS DOH workbook and reload all reference tables.

    Admin only. Accepts an .xlsx file, writes it to a temp location, and
    runs the same init_db logic as the CLI. Existing claims, users, audit
    log, and CMS cache are preserved — only the 5 APG reference tables
    are replaced (hcpcs_to_eapg, icd10_to_eapg, apg_weights, apg_base_rates,
    provider_county).
    """
    import tempfile
    from pathlib import Path
    from backend.db.init_db import run as run_init_db

    if not file.filename.endswith(('.xlsx', '.xlsm')):
        raise HTTPException(400, "File must be an Excel workbook (.xlsx or .xlsm)")

    # Write to a temp file so the init_db loader can read it with openpyxl
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        await run_init_db(tmp_path)
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Failed to load workbook: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)

    await audit(
        session, user=current, request=request,
        action="reference_data.reload",
        resource=file.filename,
        details={"file_size": len(content)},
    )
    await session.commit()
    return {"ok": True, "filename": file.filename, "message": "Reference data reloaded successfully."}


@app.delete("/api/claims", status_code=200)
async def clear_all_claims(
    current: RequireAnalyst,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Wipe every parsed claim and its APG result from the database.

    Analyst or admin only (viewers cannot trigger this). Keeps users, reference
    data, provider settings, and the audit log. An audit entry records who
    performed the clear and how many rows were removed.

    Cascade rules on ParsedClaim ensure service_lines, adjustments, and the
    apg_result row all go when the parent claim is deleted — so we only issue
    a single `DELETE FROM parsed_claim`. The three child tables also get
    explicit deletes as a safety net in case any orphan rows ever exist.
    """
    # Count first (for the audit log + response)
    n = (await session.execute(select(func.count()).select_from(ORMClaim))).scalar_one()

    # Delete child tables first in case any orphans exist (belt + suspenders
    # on top of the ON DELETE CASCADE already on the foreign keys)
    await session.execute(delete(ORMApgResult))
    await session.execute(delete(ORMClaimAdjustment))
    await session.execute(delete(ORMLine))
    await session.execute(delete(ORMClaim))

    await audit(
        session, user=current, request=request,
        action="claims.clear_all",
        resource=f"{n} claim(s)",
        details={"claims_deleted": n},
    )
    await session.commit()
    return {"claims_deleted": n}


# ---------------------------------------------------------------------------
# Reference lookups — Article 28
# ---------------------------------------------------------------------------


@app.get("/api/reference/hcpcs/{code}", response_model=HcpcsLookupOut)
async def lookup_hcpcs(
    code: str,
    current: CurrentUser,
    dos: date = Query(...),
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
    code: str,
    current: CurrentUser,
    dos: date = Query(...),
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
    apg_code: int,
    current: CurrentUser,
    dos: date = Query(...),
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
    current: CurrentUser,
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
    current: CurrentUser,
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


@app.get("/api/reference/cms-localities")
async def list_cms_localities(
    current: CurrentUser,
    year: int = Query(..., ge=2000, le=2100,
                      description="Calendar year of the CMS fee schedule"),
) -> dict:
    """List all CMS Medicare localities for a given year.

    Used by the UI to populate locality dropdowns so users pick by name
    ('MANHATTAN') instead of having to remember the 7-digit MAC-locality code
    ('1320201'). Cached in the engine for 24h per year.
    """
    cms = get_cms_engine()
    rows = await cms.list_localities(year)
    return {
        "year": year,
        "count": len(rows),
        "localities": rows,
    }


@app.get("/api/reference/zip-locality/{zip_code}")
async def lookup_zip_locality(
    zip_code: str,
    current: CurrentUser,
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


# ---------------------------------------------------------------------------
# Rate Calculator (Phase 8) — manual CPT/ICD entry
# ---------------------------------------------------------------------------


@app.post("/api/calculator/calculate", response_model=CalculatorOut)
async def calculator_calculate(
    payload: CalculatorIn,
    current: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> CalculatorOut:
    """Compute APG and/or CMS MPFS rates for manually-entered service lines.

    Reuses the same APGEngine and CMSFeeScheduleEngine as the EDI upload path,
    so results match the batch-processing flow exactly.

    Requires an active provider configuration when the target includes 'apg'.
    Requires a CMS locality (either on the payload or the active provider) when
    the target includes 'cms'.
    """
    warnings: list[str] = []

    # ------------------------------ APG ------------------------------------
    apg_result = None
    if payload.target in (CalculationTarget.APG, CalculationTarget.BOTH):
        q = await session.execute(
            select(ProviderConfig).where(ProviderConfig.is_active.is_(True)).limit(1)
        )
        provider = q.scalar_one_or_none()
        if provider is None:
            if payload.target == CalculationTarget.APG:
                raise HTTPException(
                    400,
                    "APG calculation requires an active provider. "
                    "Set one via Settings before using the calculator.",
                )
            warnings.append("APG skipped: no active provider configured.")
        else:
            # The form's "Paid $" input gets stored on billed_amount in the
            # CalculatorLineIn payload (legacy field name). We flow that value
            # through to the synthetic ParsedClaim as paid_amount so the APG
            # engine's variance = correct - paid math is meaningful — which is
            # what the user actually wants to see in the variance column.
            paid_total = sum(
                (Decimal(str(sl.billed_amount or 0)) for sl in payload.service_lines),
                Decimal("0"),
            )
            synthetic = ParsedClaimDTO(
                file_type=FileType.ERA_835I,
                claim_id=f"CALC-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}",
                date_of_service=payload.date_of_service,
                billed_amount=paid_total,   # shown as "Billed total" in UI
                paid_amount=paid_total,     # drives variance
                allowed_amount=paid_total,
                principal_diagnosis=payload.principal_diagnosis,
                other_diagnoses=list(payload.other_diagnoses),
                service_lines=[
                    ServiceLineDTO(
                        line_seq=i,
                        procedure_code=sl.procedure_code.upper().strip(),
                        modifiers=[m.strip().upper() for m in (sl.modifiers or []) if m],
                        units=sl.units,
                        billed_amount=Decimal(str(sl.billed_amount)) if sl.billed_amount else Decimal("0"),
                        paid_amount=Decimal(str(sl.billed_amount)) if sl.billed_amount else Decimal("0"),
                        allowed_amount=Decimal(str(sl.billed_amount)) if sl.billed_amount else Decimal("0"),
                        date_of_service=payload.date_of_service,
                    )
                    for i, sl in enumerate(payload.service_lines, start=1)
                ],
            )
            apg_engine = APGEngine()
            apg_result = await apg_engine.calculate(session, synthetic, provider)

    # ------------------------------ CMS ------------------------------------
    cms_lines = None
    cms_locality_used = None
    if payload.target in (CalculationTarget.CMS, CalculationTarget.BOTH):
        # Locality resolution order: form → active provider → fail
        locality = payload.cms_locality
        if not locality:
            q = await session.execute(
                select(ProviderConfig).where(ProviderConfig.is_active.is_(True)).limit(1)
            )
            prov = q.scalar_one_or_none()
            if prov and prov.cms_locality:
                locality = prov.cms_locality
        if not locality:
            if payload.target == CalculationTarget.CMS:
                raise HTTPException(
                    400,
                    "CMS calculation requires a locality. Either pass cms_locality "
                    "or set one on the active provider.",
                )
            warnings.append("CMS skipped: no locality (pass cms_locality or configure provider).")
        else:
            import asyncio as _asyncio
            from backend.engines.cms_engine import CMSDatasetMovedError
            cms_locality_used = locality
            cms = get_cms_engine()
            year = payload.date_of_service.year

            async def _one_line(sl) -> CalculatorLineCMS:
                code = sl.procedure_code.upper().strip()
                modifier = (sl.modifiers[0].upper().strip() if sl.modifiers else "")
                # Always fetch the base (user-supplied-modifier) rate
                base_task = cms.get_mpfs_rate(session, code, modifier, locality, year)

                # Optionally fetch the -26 (professional) and -TC (technical) rows
                # in parallel. Not every HCPCS code has a PC/TC split — for E/M,
                # drugs, most labs, those queries return None and we render '—'.
                if payload.cms_include_pc_tc:
                    pro_task = cms.get_mpfs_rate(session, code, "26", locality, year)
                    tec_task = cms.get_mpfs_rate(session, code, "TC", locality, year)
                    try:
                        row, pro_row, tec_row = await _asyncio.gather(
                            base_task, pro_task, tec_task, return_exceptions=False,
                        )
                    except CMSDatasetMovedError:
                        # Propagate so the outer handler can show a banner
                        raise
                    except Exception as e:
                        return CalculatorLineCMS(procedure_code=code, error=f"CMS API error: {e}")
                else:
                    try:
                        row = await base_task
                    except CMSDatasetMovedError:
                        raise
                    except Exception as e:
                        return CalculatorLineCMS(procedure_code=code, error=f"CMS API error: {e}")
                    pro_row = tec_row = None

                if row is None:
                    return CalculatorLineCMS(
                        procedure_code=code,
                        error=f"No MPFS rate for HCPCS {code} "
                              f"(modifier {modifier or 'none'}, locality {locality}, "
                              f"year {year})",
                    )
                chosen = row.facility_rate if payload.cms_use_facility_rate else row.non_facility_rate
                expected = (chosen * sl.units) if chosen is not None else None

                # PC/TC rates: each of pro_row / tec_row has its own non-facility
                # rate. We surface the non-facility side as 'the' professional or
                # technical rate (facility/non-facility distinction rarely applies
                # on the PC row). None values naturally pass through.
                pro_rate = pro_row.non_facility_rate if pro_row is not None else None
                tec_rate = tec_row.non_facility_rate if tec_row is not None else None

                return CalculatorLineCMS(
                    procedure_code=code,
                    non_facility_rate=row.non_facility_rate,
                    facility_rate=row.facility_rate,
                    professional_rate=pro_rate,
                    technical_rate=tec_rate,
                    work_rvu=row.work_rvu,
                    pe_rvu=row.pe_rvu,
                    mp_rvu=row.mp_rvu,
                    total_rvu=row.total_rvu,
                    conversion_factor=row.conversion_factor,
                    expected_payment=expected,
                )

            # Process lines in parallel across the top level too, not just
            # within a single line's PC/TC fetches. Semaphore in CMSFeeScheduleEngine
            # caps total concurrent outbound HTTP requests to 10.
            try:
                cms_lines = list(await _asyncio.gather(
                    *[_one_line(sl) for sl in payload.service_lines]
                ))
            except CMSDatasetMovedError:
                # The CMS dataset URL has been retired. Don't spam per-line
                # errors — surface a single banner-level warning + set
                # cms_lines to None so the UI knows to hide the CMS panel.
                cms_lines = None
                cms_locality_used = None
                warnings.append(
                    "CMS MPFS integration is being updated — CMS migrated their "
                    "Physician Fee Schedule API to a new schema and our dataset "
                    "reference has been retired. APG/Article 28 results are "
                    "unaffected. CMS rates will return once the integration is "
                    "updated against the new pfs.data.cms.gov API."
                )

    return CalculatorOut(
        date_of_service=payload.date_of_service,
        target=payload.target,
        apg=apg_result,
        cms_locality_used=cms_locality_used,
        cms_lines=cms_lines,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Analytics (Phase 4)
# ---------------------------------------------------------------------------


_analytics = AnalyticsEngine()


def _analytics_filters(date_from, date_to, payer_name, file_type, provider_npi) -> dict:
    """Collect the query-string filter args into the kwarg dict the engine expects."""
    return {
        "date_from": date_from,
        "date_to": date_to,
        "payer_name": payer_name,
        "file_type": file_type,
        "provider_npi": provider_npi,
    }


@app.get("/api/analytics/summary")
async def analytics_summary(
    current: CurrentUser,
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    payer_name: Optional[str] = Query(None),
    file_type: Optional[str] = Query(None, pattern="^(835I|835P|837I|837P)$"),
    provider_npi: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _analytics.summary(
        session, **_analytics_filters(date_from, date_to, payer_name, file_type, provider_npi)
    )


@app.get("/api/analytics/compression")
async def analytics_compression(
    current: CurrentUser,
    group_by: str = Query("eapg", pattern="^(eapg|procedure|peer_group|region|date_year)$"),
    limit: int = Query(20, ge=1, le=200),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    payer_name: Optional[str] = Query(None),
    file_type: Optional[str] = Query(None, pattern="^(835I|835P|837I|837P)$"),
    provider_npi: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _analytics.compression(
        session, group_by=group_by, limit=limit,
        **_analytics_filters(date_from, date_to, payer_name, file_type, provider_npi),
    )


@app.get("/api/analytics/denials")
async def analytics_denials(
    current: CurrentUser,
    limit: int = Query(20, ge=1, le=200),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    payer_name: Optional[str] = Query(None),
    file_type: Optional[str] = Query(None, pattern="^(835I|835P|837I|837P)$"),
    provider_npi: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _analytics.denials(
        session, limit=limit,
        **_analytics_filters(date_from, date_to, payer_name, file_type, provider_npi),
    )


@app.get("/api/analytics/trends")
async def analytics_trends(
    current: CurrentUser,
    period: str = Query("monthly", pattern="^(monthly|quarterly)$"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    payer_name: Optional[str] = Query(None),
    file_type: Optional[str] = Query(None, pattern="^(835I|835P|837I|837P)$"),
    provider_npi: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _analytics.trends(
        session, period=period,
        **_analytics_filters(date_from, date_to, payer_name, file_type, provider_npi),
    )


@app.get("/api/analytics/payer-scorecard")
async def analytics_payer_scorecard(
    current: CurrentUser,
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    payer_name: Optional[str] = Query(None),
    file_type: Optional[str] = Query(None, pattern="^(835I|835P|837I|837P)$"),
    provider_npi: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _analytics.payer_scorecard(
        session, **_analytics_filters(date_from, date_to, payer_name, file_type, provider_npi),
    )


# ---------------------------------------------------------------------------
# Export (Phase 4)
# ---------------------------------------------------------------------------


async def _gather_export_payload(session: AsyncSession, opts: ExportOptions) -> dict:
    """Assemble the nested dict the Excel/PDF builders consume.

    Kept here (rather than in the exporters) so the exporter modules stay
    trivial to test with fixtures — no ORM imports required in either.
    """
    filters = _analytics_filters(opts.date_from, opts.date_to, opts.payer_name, None, None)

    # Active provider (if any)
    q = await session.execute(
        select(ProviderConfig).where(ProviderConfig.is_active.is_(True)).limit(1)
    )
    provider = q.scalar_one_or_none()
    provider_dict = None
    if provider:
        provider_dict = {
            "provider_name": provider.provider_name,
            "npi": provider.npi,
            "peer_group": provider.peer_group,
            "provider_type": provider.provider_type,
            "region": provider.region,
            "county_code": provider.county_code,
            "cms_locality": provider.cms_locality,
        }

    summary = await _analytics.summary(session, **filters)
    eapg_breakdown = await _analytics.compression(session, group_by="eapg", limit=50, **filters)
    denials = await _analytics.denials(session, limit=50, **filters)

    claims_835i = await _fetch_claims_for_export(session, "835I", opts) if opts.include_835i else []
    claims_835p = await _fetch_claims_for_export(session, "835P", opts) if opts.include_835p else []

    # Observed HCPCS → EAPG crosswalk snapshot
    observed = set()
    for c in claims_835i + claims_835p:
        for sl in c.get("service_lines", []):
            if sl.get("procedure_code"):
                observed.add(sl["procedure_code"])
    reference_codes = []
    any_dos = next(
        (c.get("date_of_service") for c in claims_835i + claims_835p if c.get("date_of_service")),
        None,
    )
    if isinstance(any_dos, str):
        try:
            any_dos = date.fromisoformat(any_dos)
        except ValueError:
            any_dos = None
    if observed and any_dos:
        apg = APGEngine()
        for code in sorted(observed):
            row = await apg.lookup_hcpcs_eapg(session, code, any_dos)
            if row is not None:
                reference_codes.append({
                    "hcpcs": row.hcpcs,
                    "eapg": row.eapg,
                    "eapg_desc": row.eapg_desc,
                    "eapg_type": row.eapg_type,
                    "effective": row.quarter_effective_date.isoformat()
                                   if row.quarter_effective_date else None,
                })

    # Base rates used by the active provider (all effective dates)
    base_rates: list[dict] = []
    if provider:
        q = await session.execute(
            select(ApgBaseRate).where(
                ApgBaseRate.source == provider.provider_type,
                ApgBaseRate.peer_group == provider.peer_group,
                ApgBaseRate.region == (provider.region or "Downstate"),
            ).order_by(ApgBaseRate.effective_date)
        )
        for br in q.scalars().all():
            base_rates.append({
                "source": br.source, "peer_group": br.peer_group, "region": br.region,
                "effective_date": br.effective_date.isoformat(), "rate": str(br.rate),
            })

    return {
        "generated_at": datetime.utcnow(),
        "filters": {
            k: v for k, v in {
                "date_from": opts.date_from.isoformat() if opts.date_from else None,
                "date_to": opts.date_to.isoformat() if opts.date_to else None,
                "payer_name": opts.payer_name,
            }.items() if v is not None
        },
        "provider": provider_dict,
        "summary": summary,
        "claims_835i": claims_835i,
        "claims_835p": claims_835p,
        "eapg_breakdown": eapg_breakdown,
        "denials": denials,
        "reference_codes": reference_codes,
        "base_rates": base_rates,
    }


async def _fetch_claims_for_export(
    session: AsyncSession, file_type: str, opts: ExportOptions,
) -> list[dict]:
    stmt = select(ORMClaim).where(ORMClaim.file_type == file_type)
    if opts.date_from:
        stmt = stmt.where(ORMClaim.date_of_service >= opts.date_from)
    if opts.date_to:
        stmt = stmt.where(ORMClaim.date_of_service <= opts.date_to)
    if opts.payer_name:
        stmt = stmt.where(ORMClaim.payer_name == opts.payer_name)
    stmt = stmt.order_by(ORMClaim.date_of_service.desc(), ORMClaim.id.desc())
    res = await session.execute(stmt)
    claims = res.scalars().all()

    out = []
    for c in claims:
        apg = None
        if c.apg_result:
            apg = {
                "correct_apg_payment": str(c.apg_result.correct_apg_payment),
                "actual_paid": str(c.apg_result.actual_paid),
                "variance": str(c.apg_result.variance),
                "compression_pct": str(c.apg_result.compression_pct),
                "peer_group": c.apg_result.peer_group,
                "region": c.apg_result.region,
                "base_rate_applied": str(c.apg_result.base_rate_applied),
                "discounting_applied": c.apg_result.discounting_applied,
                "u6_applied": c.apg_result.u6_applied,
                "capital_applied": c.apg_result.capital_applied,
                "line_details": c.apg_result.line_details or [],
            }
        out.append({
            "claim_id": c.claim_id,
            "date_of_service": c.date_of_service.isoformat() if c.date_of_service else None,
            "patient_name": c.patient_name,
            "provider_npi": c.provider_npi,
            "payer_name": c.payer_name,
            "billed_amount": str(c.billed_amount),
            "allowed_amount": str(c.allowed_amount),
            "paid_amount": str(c.paid_amount),
            "patient_responsibility": str(c.patient_responsibility),
            "claim_filing_indicator": c.claim_filing_indicator,
            "claim_status": c.claim_status,
            "principal_diagnosis": c.principal_diagnosis,
            "service_lines": [
                {
                    "line_seq": sl.line_seq,
                    "procedure_code": sl.procedure_code,
                    "modifiers": list(sl.modifiers or []),
                    "revenue_code": sl.revenue_code,
                    "billed_amount": str(sl.billed_amount),
                    "allowed_amount": str(sl.allowed_amount),
                    "paid_amount": str(sl.paid_amount),
                    "units": sl.units,
                }
                for sl in c.service_lines
            ],
            "apg_result": apg,
        })
    return out


@app.post("/api/export/excel")
async def export_excel(
    opts: ExportOptions,
    current: CurrentUser,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    payload = await _gather_export_payload(session, opts)
    blob = ExcelExporter().build(payload)
    fname = f"apg_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"
    await audit(session, user=current, request=request,
                action="export.excel", resource=fname,
                details={"bytes": len(blob)})
    await session.commit()
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/export/pdf")
async def export_pdf(
    opts: ExportOptions,
    current: CurrentUser,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    payload = await _gather_export_payload(session, opts)
    blob = PDFExporter().build(payload, max_claims=opts.pdf_max_claims)
    fname = f"apg_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
    await audit(session, user=current, request=request,
                action="export.pdf", resource=fname,
                details={"bytes": len(blob)})
    await session.commit()
    return Response(
        content=blob,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
