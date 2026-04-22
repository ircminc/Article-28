"""APG rate calculation engine (Article 28 / NYS DOH).

Implements the reimbursement formula:
    APG Payment = APG Relative Weight × Base Rate × Modifiers + Capital Add-On

...with the full set of rules described in the NYS DOH APG Provider Manual:
  * Date-scoped EAPG assignment (HCPCS and ICD-10 crosswalks)
  * Date-scoped relative weights (with `final_rate` override when year_rate matches)
  * Peer-group + region + DOS base-rate selection
  * EAPG type classification (Significant Procedure, Medical Visit, Ancillary,
    Incidental, Add-On) and per-type payment rules
  * Packaging — Incidental services and certain ancillaries are not separately payable
  * Multi-procedure discounting — highest-weight significant procedure at 100%,
    additional significant procedures at 50%
  * Medical Visit / Significant Procedure interaction — medical visit packages when
    a significant procedure is on the same claim
  * Modifier U6 — per NYS DOH U6 Modifier Policy (stubbed with a configurable
    adjustment factor; refine when the policy rate is confirmed)
  * Capital add-on — per-provider flat amount added to the claim total
  * Zero-weight packaging — a weight of 0 in the applicable period means not
    separately payable for that period

All monetary math uses Decimal with ROUND_HALF_UP at 2 decimal places for final
payment amounts. Intermediate calculations retain more precision.

Design notes:
  * The engine is async because the DB layer is async — it takes an AsyncSession
    for all lookups. The engine is otherwise stateless (one instance is reusable
    across claims, provider configs are passed in at calculate() time).
  * Lookups are intentionally NOT cached in-memory for Phase 1. The reference
    tables are small enough that SQLite with proper indexes answers in <1ms.
    If we see profiling hotspots in Phase 4 we can add an LRU cache.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import (
    ApgBaseRate,
    ApgWeight,
    FeeScheduleItem,
    HcpcsToEapg,
    Icd10ToEapg,
    PxBasedWeight,
    ProviderConfig,
    ProviderCounty,
)
from backend.models.schemas import (
    APGLineResult,
    APGResult,
    EapgType,
    ParsedClaim,
    Region,
    ServiceLine,
)

log = logging.getLogger(__name__)

# Per NYS DOH U6 Modifier Policy — adjustment factor applied when U6 is present.
# TODO(Phase-2): confirm the correct factor from the published policy PDF;
# 0.75 is a placeholder representing the typical "student / supervisee" rate.
U6_ADJUSTMENT_FACTOR = Decimal("0.75")

# Multi-procedure discounting — secondary significant procedures pay at this rate.
MULTI_PROCEDURE_DISCOUNT = Decimal("0.50")

CENT = Decimal("0.01")
SENTINEL_FINAL_DATE = date(9999, 12, 31)


def _round_money(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Internal dataclasses (engine-local)
# ---------------------------------------------------------------------------


@dataclass
class _LineContext:
    """Transient per-line state during calculation."""
    line_seq: int
    svc: ServiceLine
    eapg: Optional[int] = None
    eapg_desc: Optional[str] = None
    eapg_type: EapgType = EapgType.UNKNOWN
    eapg_category: Optional[str] = None
    weight: Optional[Decimal] = None
    raw_payment: Decimal = Decimal("0")
    packaged: bool = False
    discounted: bool = False
    u6_applied: bool = False
    denied: bool = False
    fee_scheduled: bool = False     # priority #1: flat-rate fee schedule applied
    px_weight_applied: bool = False # priority #2: Px-based weight override applied
    eapg_type_raw: Optional[str] = None  # raw v3.18 type string for UI display
    notes: list[str] = None

    def __post_init__(self):
        if self.notes is None:
            self.notes = []


# ---------------------------------------------------------------------------
# Public result types are re-exported from schemas
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class APGEngine:
    """Stateless calculator; hand it an AsyncSession at call time.

    Typical usage:
        engine = APGEngine()
        result = await engine.calculate(session, claim, provider_config)
    """

    # -----------------------------------------------------------------
    # Reference lookups (each returns the best row for the given DOS)
    # -----------------------------------------------------------------

    async def lookup_hcpcs_eapg(
        self, session: AsyncSession, hcpcs: str, dos: date
    ) -> Optional[HcpcsToEapg]:
        """Return the HCPCS→EAPG row whose effective range contains DOS.

        Rule: quarter_effective_date <= dos AND (quarter_end_date IS NULL OR >= dos).
        Selects the row with the most recent quarter_effective_date if multiple match.
        """
        stmt = (
            select(HcpcsToEapg)
            .where(
                HcpcsToEapg.hcpcs == hcpcs,
                or_(
                    HcpcsToEapg.quarter_effective_date.is_(None),
                    HcpcsToEapg.quarter_effective_date <= dos,
                ),
                or_(
                    HcpcsToEapg.quarter_end_date.is_(None),
                    HcpcsToEapg.quarter_end_date >= dos,
                ),
            )
            .order_by(HcpcsToEapg.quarter_effective_date.desc().nullslast())
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    async def lookup_icd10_eapg(
        self, session: AsyncSession, dx: str, dos: date
    ) -> Optional[Icd10ToEapg]:
        """Fallback diagnosis-based EAPG lookup. Same date rules as HCPCS."""
        stmt = (
            select(Icd10ToEapg)
            .where(
                Icd10ToEapg.dx_code == dx,
                or_(Icd10ToEapg.effective_date.is_(None), Icd10ToEapg.effective_date <= dos),
                or_(Icd10ToEapg.end_date.is_(None), Icd10ToEapg.end_date >= dos),
            )
            .order_by(Icd10ToEapg.effective_date.desc().nullslast())
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    async def lookup_apg_weight(
        self, session: AsyncSession, apg: int, dos: date
    ) -> Optional[ApgWeight]:
        """Pick the weight row for the given APG + DOS.

        Preference order:
          1. If a final_rate row exists (effective_date = sentinel 9999-12-31)
             AND its year_rate >= year(DOS), use that.
          2. Otherwise, the most recent effective_date <= DOS, non-final-rate.
        """
        # Check for final_rate override first
        stmt_final = (
            select(ApgWeight)
            .where(
                ApgWeight.apg == apg,
                ApgWeight.is_final_rate.is_(True),
                ApgWeight.year_rate.isnot(None),
                ApgWeight.year_rate >= dos.year,
            )
            .limit(1)
        )
        res = await session.execute(stmt_final)
        row = res.scalar_one_or_none()
        if row is not None:
            return row

        # Fall back to the dated weight history
        stmt = (
            select(ApgWeight)
            .where(
                ApgWeight.apg == apg,
                ApgWeight.is_final_rate.is_(False),
                ApgWeight.effective_date <= dos,
            )
            .order_by(ApgWeight.effective_date.desc())
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    async def lookup_fee_schedule(
        self, session: AsyncSession, hcpcs: str, dos: date
    ) -> Optional[FeeScheduleItem]:
        """Return the Fee Schedule row (flat reimbursement) whose effective_date
        is the most recent <= DOS, if any. Priority #1 in the pricing ladder —
        when present, the APG formula is bypassed entirely."""
        stmt = (
            select(FeeScheduleItem)
            .where(
                FeeScheduleItem.hcpcs == hcpcs,
                FeeScheduleItem.effective_date <= dos,
            )
            .order_by(FeeScheduleItem.effective_date.desc())
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    async def lookup_px_weight(
        self, session: AsyncSession, hcpcs: str, dos: date
    ) -> Optional[PxBasedWeight]:
        """Return the Px-based weight row whose effective_date is the most
        recent <= DOS, if any. Priority #2 in the pricing ladder — when
        present (and fee schedule is not), this weight OVERRIDES the APG
        weight but the rest of the APG formula (base rate, discounting,
        packaging) still applies."""
        stmt = (
            select(PxBasedWeight)
            .where(
                PxBasedWeight.hcpcs == hcpcs,
                PxBasedWeight.effective_date <= dos,
            )
            .order_by(PxBasedWeight.effective_date.desc())
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    async def lookup_base_rate(
        self,
        session: AsyncSession,
        source: str,        # 'dtc' | 'hospital'
        peer_group: str,
        region: str,
        dos: date,
    ) -> Optional[ApgBaseRate]:
        """Select base rate: (source, peer_group, region) with effective_date <= DOS."""
        stmt = (
            select(ApgBaseRate)
            .where(
                ApgBaseRate.source == source,
                ApgBaseRate.peer_group == peer_group,
                ApgBaseRate.region == region,
                ApgBaseRate.effective_date <= dos,
            )
            .order_by(ApgBaseRate.effective_date.desc())
            .limit(1)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    async def resolve_region(
        self, session: AsyncSession, provider: ProviderConfig
    ) -> Region:
        """Prefer explicit region on the provider config; else derive from county."""
        if provider.region:
            return Region(provider.region)
        if provider.county_code:
            stmt = select(ProviderCounty).where(ProviderCounty.county_code == provider.county_code)
            res = await session.execute(stmt)
            row = res.scalar_one_or_none()
            if row:
                return Region(row.region)
        # Default to Downstate — most NYS Medicaid Article 28 volume is NYC
        log.warning("Could not resolve region for provider %s; defaulting to Downstate", provider.provider_name)
        return Region.DOWNSTATE

    # -----------------------------------------------------------------
    # Main calculation
    # -----------------------------------------------------------------

    async def calculate(
        self,
        session: AsyncSession,
        claim: ParsedClaim,
        provider: ProviderConfig,
    ) -> APGResult:
        """Calculate the correct APG payment for a single claim."""
        dos = claim.date_of_service
        if dos is None:
            # Every Article 28 claim must have a DOS; without one we cannot date-scope
            # the reference lookups. Return a zero result with a note.
            return self._zero_result(
                claim, provider, Region.DOWNSTATE,
                Decimal("0"), note="No date of service on claim; cannot calculate APG payment.",
            )

        region = await self.resolve_region(session, provider)
        base_rate_row = await self.lookup_base_rate(
            session, provider.provider_type, provider.peer_group, region.value, dos,
        )
        base_rate = base_rate_row.rate if base_rate_row else Decimal("0")

        notes: list[str] = []
        if base_rate_row is None:
            notes.append(
                f"No base rate found for {provider.provider_type}/{provider.peer_group}/{region.value} "
                f"on or before {dos.isoformat()}."
            )

        # Step 1: build per-line context with EAPG assignments
        contexts: list[_LineContext] = []
        for sl in claim.service_lines:
            ctx = _LineContext(line_seq=sl.line_seq, svc=sl)
            code = sl.procedure_code or ""

            # ---- Priority #1: Fee Schedule (flat reimbursement × units) ----
            # If present, this bypasses the APG formula entirely for this line.
            if code:
                fs = await self.lookup_fee_schedule(session, code, dos)
                if fs is not None and fs.reimbursement and fs.reimbursement > 0:
                    units = Decimal(sl.units or 1)
                    if fs.max_units is not None and fs.max_units > 0:
                        units = min(units, Decimal(fs.max_units))
                    ctx.raw_payment = Decimal(fs.reimbursement) * units
                    ctx.fee_scheduled = True
                    ctx.notes.append(
                        f"Fee Schedule applied: ${fs.reimbursement} × {units} units "
                        f"(eff {fs.effective_date.isoformat()}). APG formula bypassed."
                    )
                    # Still run EAPG lookup below for analytics / display,
                    # but packaging/discounting will skip this line.

            if code:
                hit = await self.lookup_hcpcs_eapg(session, code, dos)
                if hit is not None:
                    ctx.eapg = hit.eapg
                    ctx.eapg_desc = hit.eapg_desc
                    ctx.eapg_type_raw = hit.eapg_type
                    ctx.eapg_type = _coerce_eapg_type(hit.eapg_type)
                    ctx.eapg_category = hit.eapg_category
                elif not ctx.fee_scheduled:
                    ctx.notes.append(f"No EAPG mapping for HCPCS {code} on {dos.isoformat()}.")

            # Fall back to principal diagnosis if HCPCS didn't yield an EAPG
            if ctx.eapg is None and claim.principal_diagnosis:
                hit = await self.lookup_icd10_eapg(session, claim.principal_diagnosis, dos)
                if hit is not None:
                    ctx.eapg = hit.eapg
                    ctx.eapg_desc = hit.eapg_desc
                    ctx.eapg_type_raw = hit.eapg_type
                    ctx.eapg_type = _coerce_eapg_type(hit.eapg_type)
                    ctx.eapg_category = hit.eapg_category
                    ctx.notes.append(
                        f"Used ICD-10 {claim.principal_diagnosis} for EAPG assignment (no HCPCS hit)."
                    )

            # ---- Priority #2: Px-Based Weight override ----
            # If fee schedule didn't fire and the HCPCS has a Px-specific weight,
            # use that weight instead of the APG-level weight. The rest of the
            # APG formula (base rate × weight, packaging, discounting) applies.
            if not ctx.fee_scheduled and code:
                pxw = await self.lookup_px_weight(session, code, dos)
                if pxw is not None and pxw.weight and pxw.weight > 0:
                    ctx.weight = Decimal(pxw.weight)
                    ctx.px_weight_applied = True
                    ctx.notes.append(
                        f"Px-Based Weight applied: {pxw.weight} "
                        f"(eff {pxw.effective_date.isoformat()}); overrides APG weight."
                    )

            # ---- Priority #3: APG weight (fallback, existing behavior) ----
            if (
                not ctx.fee_scheduled
                and not ctx.px_weight_applied
                and ctx.eapg is not None
            ):
                wrow = await self.lookup_apg_weight(session, ctx.eapg, dos)
                if wrow is not None:
                    ctx.weight = wrow.weight
                    if wrow.weight == 0:
                        ctx.notes.append(f"APG {ctx.eapg} has weight 0 for this DOS — not separately payable.")
                else:
                    ctx.notes.append(f"No weight history for APG {ctx.eapg} on {dos.isoformat()}.")

            contexts.append(ctx)

        # Step 2: apply packaging + discounting + U6
        self._apply_packaging(contexts)
        discounting_applied = self._apply_discounting(contexts, base_rate)
        u6_any = self._apply_u6(contexts, base_rate)

        # Step 3: sum line payments for lines that weren't already handled by
        # packaging/discounting/U6 steps (e.g. plain medical visits that remain payable)
        for ctx in contexts:
            if ctx.packaged or ctx.denied:
                ctx.raw_payment = Decimal("0")
                continue
            # Fee-scheduled lines already have raw_payment set from the flat rate.
            if ctx.fee_scheduled:
                continue
            if ctx.raw_payment == 0 and ctx.weight is not None and ctx.weight > 0:
                ctx.raw_payment = base_rate * ctx.weight

        total_line_payment = sum(
            (_round_money(c.raw_payment) for c in contexts), Decimal("0")
        )

        # Step 4: capital add-on
        capital_applied = False
        capital_amount = Decimal("0")
        if provider.capital_addon_eligible and provider.capital_addon_rate:
            capital_amount = _round_money(Decimal(provider.capital_addon_rate))
            capital_applied = True

        correct_payment = _round_money(total_line_payment + capital_amount)
        actual_paid = _round_money(Decimal(claim.paid_amount))
        variance = _round_money(correct_payment - actual_paid)
        compression_pct = (
            (variance / correct_payment * Decimal(100)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if correct_payment != 0
            else Decimal("0")
        )

        line_results = [
            APGLineResult(
                line_seq=c.line_seq,
                procedure_code=c.svc.procedure_code,
                modifiers=c.svc.modifiers,
                eapg=c.eapg,
                eapg_desc=c.eapg_desc,
                eapg_type=c.eapg_type,
                eapg_category=c.eapg_category,
                weight=c.weight,
                base_rate=base_rate,
                expected_payment=_round_money(c.raw_payment),
                actual_paid=_round_money(Decimal(c.svc.paid_amount)),
                variance=_round_money(c.raw_payment - Decimal(c.svc.paid_amount)),
                packaged=c.packaged,
                discounted=c.discounted,
                u6_applied=c.u6_applied,
                denied=c.denied,
                notes=c.notes,
            )
            for c in contexts
        ]

        return APGResult(
            claim_id=claim.claim_id,
            date_of_service=dos,
            peer_group=provider.peer_group,
            region=region,
            base_rate_applied=base_rate,
            correct_apg_payment=correct_payment,
            actual_paid=actual_paid,
            variance=variance,
            compression_pct=compression_pct,
            underpaid=variance > 0,
            overpaid=variance < 0,
            discounting_applied=discounting_applied,
            u6_applied=u6_any,
            capital_applied=capital_applied,
            capital_addon_amount=capital_amount,
            line_details=line_results,
            notes=notes,
        )

    # -----------------------------------------------------------------
    # Rule helpers
    # -----------------------------------------------------------------

    def _apply_packaging(self, contexts: list[_LineContext]) -> None:
        """Incidental EAPGs are always packaged. If the claim contains at least one
        Significant Procedure, Medical Visit lines are also packaged per NYS DOH
        packaging policy (E/M services bundle into the significant procedure).

        Zero-weight lines are marked packaged because a 0 weight in the weight
        history means "not separately payable in this period."
        """
        has_significant = any(
            c.eapg_type == EapgType.SIGNIFICANT_PROCEDURE
            and (c.weight or Decimal("0")) > 0
            and not c.fee_scheduled
            for c in contexts
        )

        for c in contexts:
            # Fee-scheduled lines bypass packaging — they pay at the flat rate.
            if c.fee_scheduled:
                continue
            if c.eapg_type == EapgType.INCIDENTAL:
                c.packaged = True
                c.notes.append("Packaged: Incidental EAPG type is not separately payable.")
                continue
            if c.weight is not None and c.weight == 0:
                c.packaged = True
                c.notes.append("Packaged: weight is 0 for this date of service.")
                continue
            if c.eapg_type == EapgType.MEDICAL_VISIT and has_significant:
                c.packaged = True
                c.notes.append("Packaged: Medical Visit bundled into Significant Procedure on same claim.")

    def _apply_discounting(
        self, contexts: list[_LineContext], base_rate: Decimal
    ) -> bool:
        """Rank payable Significant Procedure lines by weight descending.
        Primary (highest weight) pays 100%. All others pay MULTI_PROCEDURE_DISCOUNT (50%)."""
        sig_lines = [
            c for c in contexts
            if c.eapg_type == EapgType.SIGNIFICANT_PROCEDURE
            and not c.packaged
            and not c.fee_scheduled
            and c.weight is not None
            and c.weight > 0
        ]
        if not sig_lines:
            return False

        sig_lines.sort(key=lambda c: c.weight or Decimal("0"), reverse=True)
        discounting_applied = False
        for idx, c in enumerate(sig_lines):
            if idx == 0:
                c.raw_payment = base_rate * c.weight
            else:
                c.raw_payment = base_rate * c.weight * MULTI_PROCEDURE_DISCOUNT
                c.discounted = True
                discounting_applied = True
                c.notes.append("Discounted to 50% as secondary significant procedure.")
        return discounting_applied

    def _apply_u6(self, contexts: list[_LineContext], base_rate: Decimal) -> bool:
        """If any modifier on a service line contains 'U6', apply U6_ADJUSTMENT_FACTOR
        to that line's raw_payment. For significant procedures that were already priced
        by discounting, we re-factor. For other types, we compute from weight here.
        """
        any_u6 = False
        for c in contexts:
            if not any(m and m.upper() == "U6" for m in c.svc.modifiers):
                continue
            any_u6 = True
            c.u6_applied = True
            if c.raw_payment > 0:
                c.raw_payment = c.raw_payment * U6_ADJUSTMENT_FACTOR
            elif c.weight is not None and c.weight > 0 and not c.packaged:
                c.raw_payment = base_rate * c.weight * U6_ADJUSTMENT_FACTOR
            c.notes.append(f"Modifier U6 applied: rate × {U6_ADJUSTMENT_FACTOR}.")
        return any_u6

    def _zero_result(
        self,
        claim: ParsedClaim,
        provider: ProviderConfig,
        region: Region,
        base_rate: Decimal,
        *,
        note: str,
    ) -> APGResult:
        return APGResult(
            claim_id=claim.claim_id,
            date_of_service=claim.date_of_service,
            peer_group=provider.peer_group,
            region=region,
            base_rate_applied=base_rate,
            correct_apg_payment=Decimal("0"),
            actual_paid=_round_money(Decimal(claim.paid_amount)),
            variance=-_round_money(Decimal(claim.paid_amount)),
            compression_pct=Decimal("0"),
            underpaid=False,
            overpaid=claim.paid_amount > 0,
            discounting_applied=False,
            u6_applied=False,
            capital_applied=False,
            capital_addon_amount=Decimal("0"),
            line_details=[],
            notes=[note],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# v3.18 eMedNY EAPG Type names -> canonical engine EapgType.
# The v3.18 crosswalk expanded the original 5-6 type names into 25+ more
# granular categories. For the engine's pricing logic (packaging,
# discounting) we collapse them back to the five categories the NYS DOH
# APG methodology defines. The raw string is still preserved on the line
# result (`eapg_type_raw`) for UI display.
_V318_TYPE_TO_ENGINE: dict[str, EapgType] = {
    # Legacy canonical five (still present in v3.18 output for many codes)
    "significant procedure":           EapgType.SIGNIFICANT_PROCEDURE,
    "medical visit":                   EapgType.MEDICAL_VISIT,
    "ancillary":                       EapgType.ANCILLARY,
    "incidental":                      EapgType.INCIDENTAL,
    "add-on":                          EapgType.ADD_ON,
    "add on":                          EapgType.ADD_ON,
    # v3.18 expanded taxonomy
    "per diem":                        EapgType.MEDICAL_VISIT,
    "drug":                            EapgType.ANCILLARY,
    "dme":                             EapgType.ANCILLARY,
    "unassigned":                      EapgType.UNKNOWN,
    "physical therapy & rehab":        EapgType.SIGNIFICANT_PROCEDURE,
    "physical therapy and rehab":      EapgType.SIGNIFICANT_PROCEDURE,
    "behavioral health & counseling":  EapgType.SIGNIFICANT_PROCEDURE,
    "behavioral health and counseling":EapgType.SIGNIFICANT_PROCEDURE,
    "dental or oral surgery procs":    EapgType.SIGNIFICANT_PROCEDURE,
    "dental or oral surgery":          EapgType.SIGNIFICANT_PROCEDURE,
    "radiologic procedure":            EapgType.SIGNIFICANT_PROCEDURE,
    "diagnostic or therapeutic proc":  EapgType.SIGNIFICANT_PROCEDURE,
    "diagnostic or therapeutic procedure": EapgType.SIGNIFICANT_PROCEDURE,
}


def _coerce_eapg_type(raw: Optional[str]) -> EapgType:
    """Map a raw EAPG Type string (v3.18 eMedNY crosswalk) to the canonical
    engine enum. Handles the legacy five categories AND the richer v3.18
    taxonomy (25+ subtypes). Unknown strings -> EapgType.UNKNOWN.
    """
    if not raw:
        return EapgType.UNKNOWN
    key = raw.strip()
    # Fast path — exact match against the enum
    try:
        return EapgType(key)
    except ValueError:
        pass
    # Normalized lookup against the v3.18 mapping
    norm = key.lower().strip()
    mapped = _V318_TYPE_TO_ENGINE.get(norm)
    if mapped is not None:
        return mapped
    # Tolerate minor punctuation variation, e.g. "Add On" vs "Add-On"
    try:
        return EapgType(key.replace(" ", "-"))
    except ValueError:
        pass
    log.warning("Unrecognized EAPG type %r; falling back to UNKNOWN", raw)
    return EapgType.UNKNOWN


# Public re-exports (imported by backend/engines/__init__.py)
__all__ = ["APGEngine", "APGResult", "APGLineResult"]
