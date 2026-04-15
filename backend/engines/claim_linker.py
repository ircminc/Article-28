"""Auto-link 837 submissions with matching 835 remittances.

A single claim naturally has two EDI representations:
  * The 837 (claim submission) the provider sent to the payer — includes
    diagnosis codes, charges, and modifiers.
  * The 835 (remittance) the payer sent back — includes paid/allowed amounts,
    adjustments, and CARC reason codes.

When both land in our database for the same CLM01/CLP01 claim ID, we want to:
  1. Set linked_claim_id_fk on both records so detail views can render both sides.
  2. Copy the principal_diagnosis and other_diagnoses from the 837 onto the 835
     (if the 835 didn't already capture them), because the APG engine uses DX
     codes as a fallback for EAPG assignment.
  3. Re-run the APG engine for the 835 with the enriched data.

This is invoked after every upload of either file type.
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import (
    ApgResult as ORMApgResult,
    ParsedClaim as ORMClaim,
    ProviderConfig,
)
from backend.engines.apg_engine import APGEngine
from backend.models.schemas import FileType

log = logging.getLogger(__name__)


_ERA_TYPES = {"835I", "835P"}
_CLAIM_TYPES = {"837I", "837P"}


def _is_era(ft: str) -> bool:
    return ft in _ERA_TYPES


def _is_submission(ft: str) -> bool:
    return ft in _CLAIM_TYPES


async def link_and_enrich(
    session: AsyncSession,
    claim_ids: Iterable[str],
    provider: ProviderConfig,
) -> dict:
    """Link any matching 837↔835 pairs among the given claim IDs and re-run APG.

    Call this after a batch upload. Safe to call with claim IDs that don't have
    any siblings — it's a no-op for those.

    Returns a dict summarizing what happened (how many pairs linked, how many
    APG recalcs ran).
    """
    claim_ids = list({cid for cid in claim_ids if cid})
    if not claim_ids:
        return {"linked_pairs": 0, "apg_recalcs": 0}

    # Pull all DB rows matching these claim IDs (across both 837 and 835)
    stmt = select(ORMClaim).where(ORMClaim.claim_id.in_(claim_ids))
    res = await session.execute(stmt)
    rows = list(res.scalars().all())

    # Group by claim_id
    by_id: dict[str, list[ORMClaim]] = {}
    for r in rows:
        by_id.setdefault(r.claim_id, []).append(r)

    linked_pairs = 0
    apg_recalcs = 0
    engine = APGEngine()

    for cid, group in by_id.items():
        eras = [c for c in group if _is_era(c.file_type)]
        subs = [c for c in group if _is_submission(c.file_type)]
        if not eras or not subs:
            continue

        # Prefer the most recent of each type (multiple uploads possible)
        era = max(eras, key=lambda c: c.created_at)
        sub = max(subs, key=lambda c: c.created_at)

        # Link both sides
        era.linked_claim_id_fk = sub.id
        sub.linked_claim_id_fk = era.id
        linked_pairs += 1

        # Enrich ERA with submission's diagnosis codes if missing
        enriched = False
        if sub.principal_diagnosis and not era.principal_diagnosis:
            era.principal_diagnosis = sub.principal_diagnosis
            enriched = True
        # Merge other diagnoses (as JSON list[str])
        sub_others = list(sub.other_diagnoses or [])
        era_others = list(era.other_diagnoses or [])
        merged = list(dict.fromkeys(era_others + sub_others))  # dedupe, preserve order
        if merged != era_others:
            era.other_diagnoses = merged
            enriched = True

        if not enriched:
            continue

        # Re-run APG against the enriched ERA claim
        dto = _orm_to_dto(era)
        result = await engine.calculate(session, dto, provider)

        # Update the existing ApgResult in place via the ORM relationship.
        # The eager (selectin) loader has era.apg_result populated already, so
        # mutating its fields is the cleanest way to avoid identity-map conflicts.
        # If somehow no prior result exists, attach a new one through the relationship.
        if era.apg_result is None:
            era.apg_result = ORMApgResult(
                claim_id_fk=era.id,
                correct_apg_payment=result.correct_apg_payment,
                actual_paid=result.actual_paid,
                variance=result.variance,
                compression_pct=result.compression_pct,
                underpaid=result.underpaid,
                overpaid=result.overpaid,
                base_rate_applied=result.base_rate_applied,
                peer_group=result.peer_group,
                region=result.region.value,
                discounting_applied=result.discounting_applied,
                u6_applied=result.u6_applied,
                capital_applied=result.capital_applied,
                line_details=[ld.model_dump(mode="json") for ld in result.line_details],
            )
        else:
            era.apg_result.correct_apg_payment = result.correct_apg_payment
            era.apg_result.actual_paid = result.actual_paid
            era.apg_result.variance = result.variance
            era.apg_result.compression_pct = result.compression_pct
            era.apg_result.underpaid = result.underpaid
            era.apg_result.overpaid = result.overpaid
            era.apg_result.base_rate_applied = result.base_rate_applied
            era.apg_result.peer_group = result.peer_group
            era.apg_result.region = result.region.value
            era.apg_result.discounting_applied = result.discounting_applied
            era.apg_result.u6_applied = result.u6_applied
            era.apg_result.capital_applied = result.capital_applied
            era.apg_result.line_details = [ld.model_dump(mode="json") for ld in result.line_details]
        apg_recalcs += 1

    await session.flush()
    return {"linked_pairs": linked_pairs, "apg_recalcs": apg_recalcs}


# ---------------------------------------------------------------------------
# ORM → DTO (used to re-run APG after enrichment)
# ---------------------------------------------------------------------------


def _orm_to_dto(orm_claim: ORMClaim):
    """Convert a persisted ParsedClaim ORM row back into a Pydantic ParsedClaim
    DTO so the APG engine can consume it.

    Lazy-import the DTOs to avoid pulling Pydantic into import-time for modules
    that don't need it.
    """
    from backend.models.schemas import (
        ClaimAdjustment as DTOAdj,
        FileType as DTOFT,
        ParsedClaim as DTOClaim,
        ServiceLine as DTOLine,
    )

    return DTOClaim(
        file_type=DTOFT(orm_claim.file_type),
        file_id=orm_claim.file_id,
        payer_name=orm_claim.payer_name,
        payer_id=orm_claim.payer_id,
        provider_npi=orm_claim.provider_npi,
        provider_name=orm_claim.provider_name,
        claim_id=orm_claim.claim_id,
        patient_name=orm_claim.patient_name,
        patient_id=orm_claim.patient_id,
        date_of_service=orm_claim.date_of_service,
        claim_status=orm_claim.claim_status,
        billed_amount=orm_claim.billed_amount,
        allowed_amount=orm_claim.allowed_amount,
        paid_amount=orm_claim.paid_amount,
        patient_responsibility=orm_claim.patient_responsibility,
        claim_filing_indicator=orm_claim.claim_filing_indicator,
        principal_diagnosis=orm_claim.principal_diagnosis,
        other_diagnoses=list(orm_claim.other_diagnoses or []),
        service_lines=[
            DTOLine(
                line_seq=sl.line_seq,
                procedure_code=sl.procedure_code,
                modifiers=list(sl.modifiers or []),
                revenue_code=sl.revenue_code,
                billed_amount=sl.billed_amount,
                allowed_amount=sl.allowed_amount,
                paid_amount=sl.paid_amount,
                units=sl.units,
                date_of_service=sl.date_of_service,
            )
            for sl in orm_claim.service_lines
        ],
        adjustments=[
            DTOAdj(
                group_code=a.group_code,
                reason_code=a.reason_code,
                amount=a.amount,
                quantity=a.quantity,
            )
            for a in orm_claim.adjustments
            if a.line_seq is None
        ],
    )
