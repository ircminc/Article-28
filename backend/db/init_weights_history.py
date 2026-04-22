"""Load the NYS DOH APG weights + fee schedule history file.

Source: https://www.health.ny.gov/health_care/medicaid/rates/methodology/history_and_fee_schedule.htm
Format observed (.xls, Jan 2026 revision): three sheets:

    1. 'Final APG Based Weights'  — long-history APG weights, one column per
       effective date. Row 2 is the date header (columns 2+), row 3 has
       'APG' in col A and 'APG Description' in col B, data from row 4.

    2. 'Final Px Based Weights'   — procedure-specific weight OVERRIDES.
       Paired columns: (Px-Based Weight, Units Limit) per effective date.
       Row 2 date headers in even columns, row 3 labels in all columns.

    3. 'Fee Schedule'             — flat-rate procedures (bypass APG formula).
       Same paired-column layout as Px Weights but with (Reimbursement Amount,
       Max units). Row 4 is a '(per unit)' subtitle, not a data row.

The three sheets populate three different tables:
  - apg_weights          (replaces existing)
  - px_based_weights     (replaces existing; new in Phase 7)
  - fee_schedule         (replaces existing; new in Phase 7)

Leaves hcpcs_to_eapg, icd10_to_eapg, apg_base_rates, provider_county
untouched.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import xlrd
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import ApgWeight, FeeScheduleItem, PxBasedWeight

log = logging.getLogger(__name__)


_SENTINEL_FINAL_DATE = date(9999, 12, 31)

_MONTH_ALIASES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


# ---------------------------------------------------------------------------
# xlrd helpers
# ---------------------------------------------------------------------------


def _read_sheet(wb, sheet_name: str) -> list[list]:
    """Read a full sheet as a list of rows (each a list of cell values).
    Handles xlrd date cells → datetime.date."""
    sh = wb.sheet_by_name(sheet_name)
    rows = []
    for r in range(sh.nrows):
        row = []
        for c in range(sh.ncols):
            v = sh.cell_value(r, c)
            ct = sh.cell_type(r, c)
            if ct == xlrd.XL_CELL_DATE and v:
                try:
                    d = xlrd.xldate_as_tuple(v, wb.datemode)
                    v = date(d[0], d[1], d[2])
                except Exception:
                    pass
            elif ct == xlrd.XL_CELL_EMPTY:
                v = None
            row.append(v)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Cell decoders
# ---------------------------------------------------------------------------


def _to_decimal(v: Any) -> Optional[Decimal]:
    if v is None or v == "" or v == "-":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(Decimal(str(v).strip()))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _hcpcs_from_cell(v: Any) -> Optional[str]:
    """The xls stores numeric HCPCS codes as floats (22515.0). Normalize to
    a plain HCPCS string: '22515.0' → '22515', 'J1100' → 'J1100'."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return str(int(v))
    s = str(v).strip()
    if not s:
        return None
    return s


def _header_date(cell: Any) -> Optional[date]:
    """Parse a cell from the 'effective-date' header row into a date.
    Handles 'Dec 1\\n2008', 'July 1 \\n2011', 'Apr 1, 2010', native date, etc."""
    if cell is None:
        return None
    if isinstance(cell, datetime):
        return cell.date()
    if isinstance(cell, date):
        return cell
    s = str(cell).replace("\n", " ").strip().rstrip("*").strip()
    if not s:
        return None
    if "final" in s.lower() or "year" in s.lower() or "effective" in s.lower():
        return None
    # e.g. "Dec 1 2008", "July 1 2011", "Jan 1, 2026"
    m = re.match(r"([A-Za-z]+)\s*(\d*)\s*,?\s*(\d{4})", s)
    if not m:
        return None
    mon = _MONTH_ALIASES.get(m.group(1).lower())
    if not mon:
        return None
    day_s = m.group(2) or "1"
    try:
        return date(int(m.group(3)), mon, int(day_s))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Sheet: Final APG Based Weights  (wide → long)
# ---------------------------------------------------------------------------


async def _load_apg_weights(session: AsyncSession, rows: list[list]) -> int:
    """Populate apg_weights long-form.

    Layout:
      row 0: title
      row 1: revision note
      row 2: date headers in columns 2+ (col 0 and col 1 are blank here)
      row 3: 'APG' in col 0, 'APG Description' in col 1 (rest blank)
      row 4+: data — [apg_number, apg_desc, w_col2, w_col3, ...]

    Some rows at the end may have 'Final Rate' / 'Year Rate' columns — the
    newer NYS DOH file doesn't include these, but we tolerate them if present.
    """
    if len(rows) < 5:
        raise ValueError("APG Weights sheet too short (expected header in row 2, data from row 4).")

    date_header_row = rows[2]
    field_header_row = rows[3]

    # Column 0 should be 'APG'
    if not field_header_row or str(field_header_row[0] or "").strip().upper() != "APG":
        raise ValueError(
            "APG Weights sheet: expected 'APG' in row 3 column A. "
            f"Got {field_header_row[0]!r}. File may have an unexpected layout."
        )

    # Scan the date-header row for effective dates (skipping col 0 and col 1).
    date_columns: list[tuple[int, date]] = []
    final_rate_idx: Optional[int] = None
    year_rate_idx: Optional[int] = None
    for i, cell in enumerate(date_header_row):
        if i < 2:
            continue
        sl = str(cell or "").lower()
        if "final rate" in sl:
            final_rate_idx = i
        elif "year rate" in sl or sl.strip() == "year":
            year_rate_idx = i
        else:
            d = _header_date(cell)
            if d is not None:
                date_columns.append((i, d))

    log.info("APG Weights: %d date columns, final_rate@%s, year_rate@%s",
             len(date_columns), final_rate_idx, year_rate_idx)

    await session.execute(delete(ApgWeight))

    count = 0
    batch: list[ApgWeight] = []
    for row in rows[4:]:
        if not row or row[0] is None:
            continue
        apg = _to_int(row[0])
        if apg is None:
            continue
        desc = str(row[1]).strip() if len(row) > 1 and row[1] else None

        for col_idx, eff_date in date_columns:
            if col_idx >= len(row):
                continue
            w = _to_decimal(row[col_idx])
            if w is None:
                continue
            batch.append(ApgWeight(
                apg=apg, apg_description=desc, effective_date=eff_date,
                weight=w, is_final_rate=False, year_rate=None,
            ))
            count += 1

        fr = _to_decimal(row[final_rate_idx]) if final_rate_idx is not None and final_rate_idx < len(row) else None
        yr = _to_int(row[year_rate_idx]) if year_rate_idx is not None and year_rate_idx < len(row) else None
        if fr is not None and fr > 0 and yr is not None:
            batch.append(ApgWeight(
                apg=apg, apg_description=desc, effective_date=_SENTINEL_FINAL_DATE,
                weight=fr, is_final_rate=True, year_rate=yr,
            ))
            count += 1
        if len(batch) >= 2000:
            session.add_all(batch); await session.flush(); batch.clear()
    if batch:
        session.add_all(batch); await session.flush()

    return count


# ---------------------------------------------------------------------------
# Paired-column parser for Px Weights + Fee Schedule sheets
# ---------------------------------------------------------------------------


def _parse_paired_header(
    date_row: list, field_row: list,
) -> list[tuple[int, int, date]]:
    """Return [(value_col, secondary_col, effective_date), ...].

    Both the Px Weights and Fee Schedule sheets use paired columns:
      - date in column N (of the date-header row)
      - primary value ('Px-Based Weight' or 'Reimbursement Amount') in col N of field-row
      - secondary value ('Units Limit' or 'Max units') in col N+1 of field-row
    """
    pairs = []
    max_len = max(len(date_row), len(field_row))
    for i in range(2, max_len):
        d = _header_date(date_row[i] if i < len(date_row) else None)
        if d is None:
            continue
        # secondary column is the next cell
        secondary = i + 1
        pairs.append((i, secondary, d))
    return pairs


# ---------------------------------------------------------------------------
# Sheet: Final Px Based Weights  (paired columns → long form)
# ---------------------------------------------------------------------------


async def _load_px_weights(session: AsyncSession, rows: list[list]) -> int:
    """Populate px_based_weights. Rows with zero/empty weights are skipped
    — only procedures that actually have a non-zero Px override get stored."""
    if len(rows) < 5:
        return 0

    date_row = rows[2]
    field_row = rows[3]
    if not field_row or str(field_row[0] or "").strip().lower() != "hcpcs code":
        raise ValueError("Px Weights sheet: expected 'HCPCS Code' in row 3 column A.")

    pairs = _parse_paired_header(date_row, field_row)
    log.info("Px Weights: %d (weight, units) effective-date pairs", len(pairs))

    await session.execute(delete(PxBasedWeight))

    count = 0
    batch: list[PxBasedWeight] = []
    for row in rows[4:]:
        if not row or row[0] is None:
            continue
        hcpcs = _hcpcs_from_cell(row[0])
        if not hcpcs:
            continue
        desc = str(row[1]).strip() if len(row) > 1 and row[1] else None

        for weight_col, units_col, eff_date in pairs:
            if weight_col >= len(row):
                continue
            weight = _to_decimal(row[weight_col])
            if weight is None or weight <= 0:
                continue
            units_limit = _to_decimal(row[units_col]) if units_col < len(row) else None
            batch.append(PxBasedWeight(
                hcpcs=hcpcs.upper(),
                description=desc,
                effective_date=eff_date,
                weight=weight,
                units_limit=units_limit,
            ))
            count += 1
        if len(batch) >= 2000:
            session.add_all(batch); await session.flush(); batch.clear()
    if batch:
        session.add_all(batch); await session.flush()

    return count


# ---------------------------------------------------------------------------
# Sheet: Fee Schedule  (paired columns → long form)
# ---------------------------------------------------------------------------


async def _load_fee_schedule(session: AsyncSession, rows: list[list]) -> int:
    """Populate fee_schedule. Only rows with non-zero reimbursement amounts
    are stored. Note: row 4 is a '(per unit)' subtitle, so data starts at row 5."""
    if len(rows) < 6:
        return 0

    date_row = rows[2]
    field_row = rows[3]
    if not field_row or str(field_row[0] or "").strip().lower() != "hcpcs code":
        raise ValueError("Fee Schedule sheet: expected 'HCPCS Code' in row 3 column A.")

    pairs = _parse_paired_header(date_row, field_row)
    log.info("Fee Schedule: %d (reimbursement, max_units) effective-date pairs", len(pairs))

    await session.execute(delete(FeeScheduleItem))

    # Data starts at row 5 (row 4 is a '(per unit)' subtitle)
    data_start = 4
    if len(rows) > 4 and rows[4] and rows[4][0] is None:
        # Likely the '(per unit)' subtitle row where col A is empty
        data_start = 5

    count = 0
    batch: list[FeeScheduleItem] = []
    for row in rows[data_start:]:
        if not row or row[0] is None:
            continue
        hcpcs = _hcpcs_from_cell(row[0])
        if not hcpcs:
            continue
        desc = str(row[1]).strip() if len(row) > 1 and row[1] else None

        for amt_col, units_col, eff_date in pairs:
            if amt_col >= len(row):
                continue
            amt = _to_decimal(row[amt_col])
            if amt is None or amt <= 0:
                continue
            max_units = _to_decimal(row[units_col]) if units_col < len(row) else None
            batch.append(FeeScheduleItem(
                hcpcs=hcpcs.upper(),
                description=desc,
                effective_date=eff_date,
                reimbursement=amt,
                max_units=max_units,
            ))
            count += 1
        if len(batch) >= 2000:
            session.add_all(batch); await session.flush(); batch.clear()
    if batch:
        session.add_all(batch); await session.flush()

    return count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def load_weights_history_from_bytes(
    session: AsyncSession, file_bytes: bytes, *, filename: str,
) -> dict:
    """Replace APG weights + Px weights + Fee Schedule from the NYS DOH
    history file. Returns row counts for each table."""
    lower = (filename or "").lower()
    if not lower.endswith((".xls", ".xlsx", ".xlsm")):
        raise ValueError("Weights history file must be .xls, .xlsx, or .xlsm.")

    if lower.endswith(".xls"):
        wb = xlrd.open_workbook(file_contents=file_bytes)
    else:
        # Support .xlsx too (NYS DOH might publish a new format in future)
        # We'd need openpyxl + a different reader shim — deferred until needed.
        raise ValueError(
            ".xlsx for weights history not yet supported. NYS DOH publishes "
            "this file in .xls format. Upload the original .xls."
        )

    results = {}
    try:
        sheet_names = wb.sheet_names()

        if "Final APG Based Weights" in sheet_names:
            results["apg_weights"] = await _load_apg_weights(
                session, _read_sheet(wb, "Final APG Based Weights"),
            )
        else:
            results["apg_weights"] = 0
            log.warning("Sheet 'Final APG Based Weights' not in workbook — skipped.")

        if "Final Px Based Weights" in sheet_names:
            results["px_weights"] = await _load_px_weights(
                session, _read_sheet(wb, "Final Px Based Weights"),
            )
        else:
            results["px_weights"] = 0
            log.warning("Sheet 'Final Px Based Weights' not in workbook — skipped.")

        if "Fee Schedule" in sheet_names:
            results["fee_schedule"] = await _load_fee_schedule(
                session, _read_sheet(wb, "Fee Schedule"),
            )
        else:
            results["fee_schedule"] = 0
            log.warning("Sheet 'Fee Schedule' not in workbook — skipped.")
    finally:
        pass   # xlrd.open_workbook has no close()

    return results
