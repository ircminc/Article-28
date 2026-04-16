"""Analytics engine — aggregations over parsed claims + APG results.

All methods are async, take an AsyncSession, accept optional filter arguments,
and return plain dicts suitable for direct JSON serialization. The engine is
intentionally thin: SQL does the aggregation (SQLite + SQLAlchemy Core) rather
than pulling rows into Python and looping.

Filters supported consistently across methods:
    date_from, date_to   → filter by parsed_claim.date_of_service (inclusive)
    payer_name           → exact match (str)
    file_type            → '835I' | '835P' | '837I' | '837P'
    provider_npi         → exact match (str)

Metric definitions (important — auditable):

  compression_pct
      (correct_apg_payment - actual_paid) / correct_apg_payment * 100
      Positive = underpaid. We report this as percentage with 2 decimals.

  paid_as_pct_of_billed
      actual_paid / billed_amount * 100 (at the claim level, summed).
      Different from compression_pct — this measures payer behavior against
      charges, not against the correct APG expectation.

  denial_rate
      count(claim_status == '4') / count(claims) * 100

  underpayment_total
      sum over claims where variance > 0 (the "money left on the table")

Decimal safety: values returned to callers are strings (or numerics from the
database) so JSON serialization doesn't lose precision. Frontend code parses
on display.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import (
    ApgResult as ORMApgResult,
    ClaimAdjustment as ORMAdjustment,
    ParsedClaim as ORMClaim,
    ParsedServiceLine as ORMLine,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decimal → serializable helpers
# ---------------------------------------------------------------------------


def _d(v) -> str:
    """Decimal/number → string for JSON without losing precision."""
    if v is None:
        return "0.00"
    if isinstance(v, Decimal):
        return str(v)
    return str(Decimal(str(v)))


def _pct(numerator, denominator) -> str:
    """Safe percentage to string with 4dp — avoids div/0, returns '0.0000' on zero base."""
    if denominator in (None, 0, Decimal("0")):
        return "0.0000"
    n = Decimal(str(numerator or 0))
    d = Decimal(str(denominator))
    return str((n / d * Decimal("100")).quantize(Decimal("0.0001")))


# ---------------------------------------------------------------------------
# Filter assembly (shared across methods)
# ---------------------------------------------------------------------------


def _apply_filters(stmt, *, date_from=None, date_to=None, payer_name=None,
                   file_type=None, provider_npi=None):
    if date_from is not None:
        stmt = stmt.where(ORMClaim.date_of_service >= date_from)
    if date_to is not None:
        stmt = stmt.where(ORMClaim.date_of_service <= date_to)
    if payer_name:
        stmt = stmt.where(ORMClaim.payer_name == payer_name)
    if file_type:
        stmt = stmt.where(ORMClaim.file_type == file_type)
    if provider_npi:
        stmt = stmt.where(ORMClaim.provider_npi == provider_npi)
    return stmt


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class AnalyticsEngine:
    """Stateless. Instantiate once, reuse across requests; pass session at call time."""

    # -----------------------------------------------------------------
    # 1. Summary KPIs
    # -----------------------------------------------------------------

    async def summary(
        self, session: AsyncSession, **filters
    ) -> dict:
        """Top-of-dashboard metrics: total claims/billed/paid/variance,
        average compression, denial rate.

        Note: variance and compression come from apg_result (Article 28 claims
        only). Billed/paid totals include all file types (835I + 835P).
        """
        # Claim-level totals (all file types)
        totals_stmt = _apply_filters(
            select(
                func.count(ORMClaim.id).label("n"),
                func.coalesce(func.sum(ORMClaim.billed_amount), 0).label("billed"),
                func.coalesce(func.sum(ORMClaim.paid_amount), 0).label("paid"),
            ), **filters,
        )
        totals = (await session.execute(totals_stmt)).one()

        # Denial counts
        denial_stmt = _apply_filters(
            select(func.count(ORMClaim.id)).where(ORMClaim.claim_status == "4"),
            **filters,
        )
        denied = (await session.execute(denial_stmt)).scalar_one()

        # APG variance (835I only, joined through apg_result)
        apg_stmt = _apply_filters(
            select(
                func.count(ORMApgResult.claim_id_fk).label("n_apg"),
                func.coalesce(func.sum(ORMApgResult.correct_apg_payment), 0).label("correct_sum"),
                func.coalesce(func.sum(ORMApgResult.actual_paid), 0).label("actual_sum"),
                func.coalesce(func.sum(ORMApgResult.variance), 0).label("variance_sum"),
                func.coalesce(func.avg(ORMApgResult.compression_pct), 0).label("avg_comp"),
            ).join(ORMClaim, ORMClaim.id == ORMApgResult.claim_id_fk),
            **filters,
        )
        apg = (await session.execute(apg_stmt)).one()

        # Underpayment total — positive variances only
        underpaid_stmt = _apply_filters(
            select(func.coalesce(func.sum(ORMApgResult.variance), 0))
            .join(ORMClaim, ORMClaim.id == ORMApgResult.claim_id_fk)
            .where(ORMApgResult.variance > 0),
            **filters,
        )
        underpaid_sum = (await session.execute(underpaid_stmt)).scalar_one() or 0

        return {
            "total_claims": totals.n,
            "total_billed": _d(totals.billed),
            "total_paid": _d(totals.paid),
            "paid_as_pct_of_billed": _pct(totals.paid, totals.billed),
            "total_denied": denied,
            "denial_rate_pct": _pct(denied, totals.n) if totals.n else "0.0000",
            "apg_claims": apg.n_apg,
            "apg_correct_payment_total": _d(apg.correct_sum),
            "apg_actual_paid_total": _d(apg.actual_sum),
            "apg_total_variance": _d(apg.variance_sum),
            "apg_underpayment_total": _d(underpaid_sum),
            "apg_avg_compression_pct": _d(apg.avg_comp),
        }

    # -----------------------------------------------------------------
    # 2. Rate compression breakdown
    # -----------------------------------------------------------------

    async def compression(
        self,
        session: AsyncSession,
        group_by: str = "eapg",
        limit: int = 20,
        **filters,
    ) -> dict:
        """Group APG variance by one of:
            eapg          — per-line EAPG (unpacks apg_result.line_details JSON)
            procedure     — per-line HCPCS/CPT
            peer_group    — provider peer group used at calc time
            region        — Upstate / Downstate
            date_year     — DOS year

        Returns rows with billed/paid/expected/variance/avg_compression.
        """
        if group_by in ("peer_group", "region"):
            # Claim-level aggregation (no need to unpack line_details)
            col = ORMApgResult.peer_group if group_by == "peer_group" else ORMApgResult.region
            stmt = _apply_filters(
                select(
                    col.label("bucket"),
                    func.count(ORMApgResult.claim_id_fk).label("n"),
                    func.coalesce(func.sum(ORMApgResult.correct_apg_payment), 0).label("expected"),
                    func.coalesce(func.sum(ORMApgResult.actual_paid), 0).label("paid"),
                    func.coalesce(func.sum(ORMApgResult.variance), 0).label("variance"),
                    func.coalesce(func.avg(ORMApgResult.compression_pct), 0).label("avg_comp"),
                )
                .join(ORMClaim, ORMClaim.id == ORMApgResult.claim_id_fk)
                .group_by(col)
                .order_by(func.sum(ORMApgResult.variance).desc()),
                **filters,
            )
            rows = (await session.execute(stmt)).all()
            return {
                "group_by": group_by,
                "rows": [
                    {
                        "bucket": r.bucket,
                        "n": r.n,
                        "expected": _d(r.expected),
                        "paid": _d(r.paid),
                        "variance": _d(r.variance),
                        "avg_compression_pct": _d(r.avg_comp),
                    }
                    for r in rows
                ],
            }

        if group_by == "date_year":
            year_expr = func.cast(func.strftime("%Y", ORMClaim.date_of_service), type_=None)
            stmt = _apply_filters(
                select(
                    year_expr.label("bucket"),
                    func.count(ORMApgResult.claim_id_fk).label("n"),
                    func.coalesce(func.sum(ORMApgResult.correct_apg_payment), 0).label("expected"),
                    func.coalesce(func.sum(ORMApgResult.actual_paid), 0).label("paid"),
                    func.coalesce(func.sum(ORMApgResult.variance), 0).label("variance"),
                    func.coalesce(func.avg(ORMApgResult.compression_pct), 0).label("avg_comp"),
                )
                .join(ORMClaim, ORMClaim.id == ORMApgResult.claim_id_fk)
                .group_by(year_expr)
                .order_by(year_expr),
                **filters,
            )
            rows = (await session.execute(stmt)).all()
            return {
                "group_by": group_by,
                "rows": [
                    {
                        "bucket": r.bucket or "unknown",
                        "n": r.n,
                        "expected": _d(r.expected),
                        "paid": _d(r.paid),
                        "variance": _d(r.variance),
                        "avg_compression_pct": _d(r.avg_comp),
                    }
                    for r in rows
                ],
            }

        # eapg / procedure — needs per-line detail from apg_result.line_details JSON
        # We unpack in Python because SQLite JSON1 functions vary; this is
        # bounded by the number of APG results, which is small relative to
        # reference data.
        stmt = _apply_filters(
            select(ORMApgResult.line_details)
            .join(ORMClaim, ORMClaim.id == ORMApgResult.claim_id_fk),
            **filters,
        )
        res = await session.execute(stmt)
        buckets: dict[str, dict] = defaultdict(
            lambda: {"n": 0, "expected": Decimal("0"), "paid": Decimal("0"),
                     "variance": Decimal("0"), "_comps": []}
        )
        for (lines,) in res:
            if not lines:
                continue
            for ld in lines:
                if group_by == "eapg":
                    key = str(ld.get("eapg") or "unknown")
                    if ld.get("eapg_desc"):
                        key = f"{key} — {ld['eapg_desc']}"
                else:  # procedure
                    key = ld.get("procedure_code") or "unknown"
                entry = buckets[key]
                entry["n"] += 1
                entry["expected"] += Decimal(str(ld.get("expected_payment", 0)))
                entry["paid"] += Decimal(str(ld.get("actual_paid", 0)))
                entry["variance"] += Decimal(str(ld.get("variance", 0)))

        rows = [
            {
                "bucket": k,
                "n": v["n"],
                "expected": _d(v["expected"]),
                "paid": _d(v["paid"]),
                "variance": _d(v["variance"]),
                "avg_compression_pct": _pct(v["variance"], v["expected"])
                                       if v["expected"] > 0 else "0.0000",
            }
            for k, v in buckets.items()
        ]
        rows.sort(key=lambda r: Decimal(r["variance"]), reverse=True)
        return {"group_by": group_by, "rows": rows[:limit]}

    # -----------------------------------------------------------------
    # 3. Denial analysis
    # -----------------------------------------------------------------

    async def denials(
        self, session: AsyncSession, limit: int = 20, **filters
    ) -> dict:
        """Count + total-amount by (group_code, reason_code) aka CARC.

        Pulls from claim_adjustment (CAS segments, both claim- and service-level).
        """
        stmt = _apply_filters(
            select(
                ORMAdjustment.group_code,
                ORMAdjustment.reason_code,
                func.count(ORMAdjustment.id).label("n"),
                func.coalesce(func.sum(ORMAdjustment.amount), 0).label("amount"),
            )
            .join(ORMClaim, ORMClaim.id == ORMAdjustment.claim_id_fk)
            .group_by(ORMAdjustment.group_code, ORMAdjustment.reason_code)
            .order_by(func.sum(ORMAdjustment.amount).desc()),
            **filters,
        )
        rows = (await session.execute(stmt)).all()
        total_amount = sum((r.amount or 0) for r in rows)

        out_rows = [
            {
                "group_code": r.group_code,
                "reason_code": r.reason_code,
                "count": r.n,
                "total_amount": _d(r.amount),
                "pct_of_adjustments": _pct(r.amount, total_amount)
                                       if total_amount else "0.0000",
            }
            for r in rows[:limit]
        ]

        return {
            "rows": out_rows,
            "total_adjustments": len(rows),
            "total_amount": _d(total_amount),
        }

    # -----------------------------------------------------------------
    # 4. Trend analysis
    # -----------------------------------------------------------------

    async def trends(
        self,
        session: AsyncSession,
        period: str = "monthly",
        **filters,
    ) -> dict:
        """Time-series of billed/paid/variance grouped by month or quarter.

        Uses SQLite's strftime for the bucket key. 'monthly' returns YYYY-MM,
        'quarterly' returns YYYY-Qn.
        """
        if period not in ("monthly", "quarterly"):
            raise ValueError(f"period must be 'monthly' or 'quarterly', got {period!r}")

        month_expr = func.strftime("%Y-%m", ORMClaim.date_of_service)
        bucket = month_expr
        stmt = _apply_filters(
            select(
                bucket.label("bucket"),
                func.count(ORMClaim.id).label("n"),
                func.coalesce(func.sum(ORMClaim.billed_amount), 0).label("billed"),
                func.coalesce(func.sum(ORMClaim.paid_amount), 0).label("paid"),
                func.coalesce(func.sum(ORMApgResult.variance), 0).label("variance"),
            )
            .outerjoin(ORMApgResult, ORMClaim.id == ORMApgResult.claim_id_fk)
            .group_by(bucket)
            .order_by(bucket),
            **filters,
        )
        rows = (await session.execute(stmt)).all()

        series = []
        if period == "monthly":
            for r in rows:
                if not r.bucket:
                    continue
                series.append({
                    "period": r.bucket,
                    "claims": r.n,
                    "billed": _d(r.billed),
                    "paid": _d(r.paid),
                    "variance": _d(r.variance),
                })
        else:
            # Roll monthly → quarterly in Python. More predictable across DBs
            # than strftime gymnastics.
            buckets: dict[str, dict] = defaultdict(
                lambda: {"claims": 0, "billed": Decimal("0"),
                         "paid": Decimal("0"), "variance": Decimal("0")}
            )
            for r in rows:
                if not r.bucket:
                    continue
                y, m = r.bucket.split("-")
                q = (int(m) - 1) // 3 + 1
                key = f"{y}-Q{q}"
                entry = buckets[key]
                entry["claims"] += r.n
                entry["billed"] += Decimal(str(r.billed or 0))
                entry["paid"] += Decimal(str(r.paid or 0))
                entry["variance"] += Decimal(str(r.variance or 0))
            for key in sorted(buckets):
                v = buckets[key]
                series.append({
                    "period": key,
                    "claims": v["claims"],
                    "billed": _d(v["billed"]),
                    "paid": _d(v["paid"]),
                    "variance": _d(v["variance"]),
                })

        return {"period": period, "series": series}

    # -----------------------------------------------------------------
    # 5. Payer scorecard
    # -----------------------------------------------------------------

    async def payer_scorecard(
        self, session: AsyncSession, **filters
    ) -> dict:
        """Per-payer metrics: claim count, billed/paid totals, denial rate,
        avg compression for Article 28 claims, underpayment total.
        """
        # Main aggregation. `case` is the top-level SQL construct (not func.case).
        main_stmt = _apply_filters(
            select(
                ORMClaim.payer_name,
                func.count(ORMClaim.id).label("n"),
                func.coalesce(func.sum(ORMClaim.billed_amount), 0).label("billed"),
                func.coalesce(func.sum(ORMClaim.paid_amount), 0).label("paid"),
                func.sum(
                    case((ORMClaim.claim_status == "4", 1), else_=0)
                ).label("denied"),
            ).group_by(ORMClaim.payer_name),
            **filters,
        )
        main_rows = (await session.execute(main_stmt)).all()

        # APG-side aggregation joined separately
        apg_stmt = _apply_filters(
            select(
                ORMClaim.payer_name,
                func.count(ORMApgResult.claim_id_fk).label("n_apg"),
                func.coalesce(func.sum(ORMApgResult.variance), 0).label("variance"),
                func.coalesce(func.avg(ORMApgResult.compression_pct), 0).label("avg_comp"),
                func.coalesce(
                    func.sum(
                        case((ORMApgResult.variance > 0, ORMApgResult.variance), else_=0)
                    ),
                    0,
                ).label("underpaid"),
            )
            .join(ORMApgResult, ORMClaim.id == ORMApgResult.claim_id_fk)
            .group_by(ORMClaim.payer_name),
            **filters,
        )
        apg_rows = {r.payer_name: r for r in (await session.execute(apg_stmt)).all()}

        out = []
        for r in main_rows:
            apg = apg_rows.get(r.payer_name)
            out.append({
                "payer_name": r.payer_name or "(unknown)",
                "claims": r.n,
                "billed": _d(r.billed),
                "paid": _d(r.paid),
                "paid_as_pct_of_billed": _pct(r.paid, r.billed),
                "denied": r.denied or 0,
                "denial_rate_pct": _pct(r.denied, r.n) if r.n else "0.0000",
                "apg_claims": apg.n_apg if apg else 0,
                "apg_variance_total": _d(apg.variance) if apg else "0.00",
                "apg_underpayment_total": _d(apg.underpaid) if apg else "0.00",
                "apg_avg_compression_pct": _d(apg.avg_comp) if apg else "0.0000",
            })

        # Sort by claim volume descending — typically what's most useful
        out.sort(key=lambda x: x["claims"], reverse=True)
        return {"rows": out}

    # -----------------------------------------------------------------
    # 6. Top offenders (used by Excel/PDF reports)
    # -----------------------------------------------------------------

    async def top_underpaid_procedures(
        self, session: AsyncSession, limit: int = 10, **filters
    ) -> list[dict]:
        """Top N procedure codes by absolute variance (positive = underpaid)."""
        compression = await self.compression(session, group_by="procedure", limit=limit * 2, **filters)
        rows = [r for r in compression["rows"] if Decimal(r["variance"]) > 0]
        return rows[:limit]

    async def top_denial_reasons(
        self, session: AsyncSession, limit: int = 10, **filters
    ) -> list[dict]:
        denials = await self.denials(session, limit=limit, **filters)
        return denials["rows"]


__all__ = ["AnalyticsEngine"]
