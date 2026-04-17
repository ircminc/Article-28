"""Targeted DTC (Freestanding) base-rate loader.

NYS DOH publishes the Freestanding APG base rates on their website as a
standalone Excel file (both .xls and .xlsx format have been observed over
the years). This loader consumes that file format and updates ONLY the
`apg_base_rates` rows where source='dtc', leaving:

  * hospital base rates (source='hospital')
  * HCPCS/ICD-10 crosswalks
  * APG weights
  * provider_county
  * any parsed claims, users, audit log

completely unchanged.

Expected sheet layout (verified against the July-2022 DTC inventory file):

    Row 0:  Title row (ignored)
    Row 1:  blank
    Row 2:  Column headers:
              Peer Group | **Base Rate Code | ***Blend Rate Code |
              Capital Rate Code | Region | <N date columns>
    Row 3+: Data rows (one per peer_group × region)
    Final row(s): footnotes, ignored

We tolerate small variations — extra/missing date columns, reordered
header columns, .xls vs .xlsx, trailing footer rows with stray text.

Usage:
    from backend.db.init_dtc_rates import load_dtc_rates_from_bytes
    deleted, inserted = await load_dtc_rates_from_bytes(session, file_bytes,
                                                         filename='dtc_rates.xls')
    await session.commit()
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import ApgBaseRate

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Generic row/cell iterators (format-agnostic)
# ---------------------------------------------------------------------------


def _iter_xls_rows(file_bytes: bytes) -> tuple[list[str], list[list]]:
    """Parse an .xls (BIFF) file via xlrd. Returns (sheet_names, rows_per_sheet)."""
    import xlrd
    wb = xlrd.open_workbook(file_contents=file_bytes)
    all_sheets = []
    for sname in wb.sheet_names():
        sh = wb.sheet_by_name(sname)
        sheet_rows = []
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
            sheet_rows.append(row)
        all_sheets.append((sname, sheet_rows))
    return all_sheets


def _iter_xlsx_rows(file_bytes: bytes) -> list[tuple[str, list[list]]]:
    """Parse an .xlsx via openpyxl. Returns [(sheet_name, rows), ...]."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    all_sheets = []
    try:
        for sname in wb.sheetnames:
            sh = wb[sname]
            sheet_rows = [list(row) for row in sh.iter_rows(values_only=True)]
            all_sheets.append((sname, sheet_rows))
    finally:
        wb.close()
    return all_sheets


def _open_any_workbook(file_bytes: bytes, filename: str) -> list[tuple[str, list[list]]]:
    """Dispatch to xlrd or openpyxl based on filename. Returns list of
    (sheet_name, rows) tuples where rows is a list of cell-value lists."""
    lower = (filename or "").lower()
    if lower.endswith(".xls"):
        return _iter_xls_rows(file_bytes)
    return _iter_xlsx_rows(file_bytes)


# ---------------------------------------------------------------------------
# Cell coercion helpers (mirror what init_db.py does)
# ---------------------------------------------------------------------------


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s == "" or s == "-":
            return None
        return s
    return v


def _to_decimal(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


_HEADER_DATE_RE = re.compile(
    r"(?P<m>\d{1,2})[/-](?P<d>\d{1,2})[/-](?P<y>\d{4})|"
    r"(?P<y2>\d{4})[/-](?P<m2>\d{1,2})[/-](?P<d2>\d{1,2})"
)


def _header_to_date(h) -> Optional[date]:
    """Cell from the header row → effective date, if the cell represents one."""
    if h is None:
        return None
    if isinstance(h, (datetime,)):
        return h.date()
    if isinstance(h, date):
        return h
    s = str(h).strip()
    if not s:
        return None
    # Strip footnote asterisks ('4/1/2022****' → '4/1/2022')
    s = s.rstrip("*").strip()
    m = _HEADER_DATE_RE.search(s)
    if not m:
        return None
    try:
        if m.group("y"):
            return date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        return date(int(m.group("y2")), int(m.group("m2")), int(m.group("d2")))
    except ValueError:
        return None


def _match_header_idx(header: list, wanted: str) -> Optional[int]:
    """Return the column index whose header text (ignoring asterisks and
    case) matches `wanted`. None if not found."""
    target = wanted.lower().replace("*", "").strip()
    for i, h in enumerate(header):
        if h is None:
            continue
        cand = str(h).lower().replace("*", "").strip()
        if cand == target:
            return i
    return None


# ---------------------------------------------------------------------------
# Core loader
# ---------------------------------------------------------------------------


async def load_dtc_rates_from_bytes(
    session: AsyncSession,
    file_bytes: bytes,
    *,
    filename: str,
) -> tuple[int, int]:
    """Replace the DTC base-rate rows in `apg_base_rates`.

    Returns (deleted_count, inserted_count).

    Raises ValueError on a file that doesn't look like the expected format
    (e.g. missing required columns). The caller is responsible for
    `session.commit()` — this function only `flush()`es.
    """
    sheets = _open_any_workbook(file_bytes, filename)
    if not sheets:
        raise ValueError("Workbook has no sheets.")

    # Prefer a sheet whose name contains 'base rate' (case-insensitive).
    chosen_name, rows = None, None
    for sname, srows in sheets:
        if "base rate" in sname.lower():
            chosen_name, rows = sname, srows
            break
    if rows is None:
        # Fall back to the first sheet
        chosen_name, rows = sheets[0]

    log.info("Using sheet %r (%d rows)", chosen_name, len(rows))

    # Find the header row — it's the one with 'Peer Group' in column 0
    header_idx = None
    for i, row in enumerate(rows[:10]):
        if row and row[0] is not None:
            cell = str(row[0]).strip().lower()
            if cell == "peer group":
                header_idx = i
                break
    if header_idx is None:
        raise ValueError(
            "Could not find header row (expected 'Peer Group' in column A within the first 10 rows)."
        )

    header = rows[header_idx]
    log.info("Header row index: %d", header_idx)

    meta_cols = {
        "peer_group": _match_header_idx(header, "Peer Group"),
        "base_rate_code": _match_header_idx(header, "Base Rate Code"),
        "blend_rate_code": _match_header_idx(header, "Blend Rate Code"),
        "capital_rate_code": _match_header_idx(header, "Capital Rate Code"),
        "region": _match_header_idx(header, "Region"),
        "cure_code": _match_header_idx(header, "Cure Code"),  # may be absent
    }
    if meta_cols["peer_group"] is None or meta_cols["region"] is None:
        raise ValueError("Required columns 'Peer Group' and/or 'Region' not found.")

    # Identify date columns — everything that parses to a date
    date_cols: list[tuple[int, date]] = []
    for idx, h in enumerate(header):
        d = _header_to_date(h)
        if d is not None:
            date_cols.append((idx, d))
    if not date_cols:
        raise ValueError("No date columns found in the header row.")
    log.info("Found %d date columns: %s", len(date_cols),
             [d.isoformat() for _, d in date_cols])

    # Build ApgBaseRate rows from the data rows (header_idx + 1 onwards)
    new_rows: list[ApgBaseRate] = []
    for r in rows[header_idx + 1:]:
        if not r:
            continue
        peer = _clean(r[meta_cols["peer_group"]]) if meta_cols["peer_group"] is not None else None
        if not peer:
            continue   # blank / footer row
        # Skip footnote-looking rows
        if any(str(peer).lower().startswith(p) for p in (
            "rate", "note", "*", "source"
        )):
            continue
        region = _clean(r[meta_cols["region"]]) if meta_cols["region"] is not None else None
        if not region:
            continue   # must have a region

        base_rc = _clean(r[meta_cols["base_rate_code"]]) if meta_cols["base_rate_code"] is not None else None
        blend_rc = _clean(r[meta_cols["blend_rate_code"]]) if meta_cols["blend_rate_code"] is not None else None
        cap_rc = _clean(r[meta_cols["capital_rate_code"]]) if meta_cols["capital_rate_code"] is not None else None
        cure = _clean(r[meta_cols["cure_code"]]) if meta_cols["cure_code"] is not None else None

        def _code_str(v):
            if v is None:
                return None
            # Excel stores small integers as floats ('1428.0' → '1428')
            if isinstance(v, (int, float)):
                return str(int(v))
            return str(v).strip() or None

        for col_idx, eff_date in date_cols:
            if col_idx >= len(r):
                continue
            rate = _to_decimal(r[col_idx])
            if rate is None or rate <= 0:
                continue
            new_rows.append(ApgBaseRate(
                source="dtc",
                peer_group=str(peer),
                cure_code=_code_str(cure),
                base_rate_code=_code_str(base_rc),
                blend_rate_code=_code_str(blend_rc),
                capital_rate_code=_code_str(cap_rc),
                region=str(region),
                effective_date=eff_date,
                rate=rate,
            ))

    if not new_rows:
        raise ValueError(
            "Workbook parsed but no DTC base-rate rows extracted. "
            "Double-check the sheet layout."
        )

    # Atomic-ish replace: delete existing DTC rows, insert new ones
    deleted = await session.execute(delete(ApgBaseRate).where(ApgBaseRate.source == "dtc"))
    deleted_count = deleted.rowcount or 0
    session.add_all(new_rows)
    await session.flush()

    log.info("DTC base rates: %d old rows removed, %d new rows inserted",
             deleted_count, len(new_rows))
    return deleted_count, len(new_rows)
