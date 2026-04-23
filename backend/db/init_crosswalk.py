"""Load the eMedNY APG Crosswalk workbook.

Source: https://www.emedny.org/Crosswalk/
Format observed (v3.18, Apr 2026): an .xlsx with these sheets:
    - "How to use this document"   (ignored)
    - "HCPCS to EAPGs"             → loads into hcpcs_to_eapg
    - "ICD-10 DX to EAPGs"         → loads into icd10_to_eapg
    - "EAPGs"                      (EAPG description reference, optional use)
    - "EAPG Types"                 → provides numeric → string mapping
    - "EAPG Categories"            (ignored)
    - "HCPCS Changes v3.18"        (ignored)
    - "Legal Notice"               (ignored)

Quirks handled:
  * First ~5-7 rows are title / effective-date / copyright notices;
    actual header row is found by scanning for 'HCPCS' or 'DX' in col A.
  * EAPG Type column stores a NUMBER (e.g. '4' for Ancillary). Our existing
    tables store the string form ("Ancillary"), which the APG engine's
    EapgType enum maps onto. We load the EAPG Types sheet first to build
    a `{numeric_code: type_name}` dict and translate on the fly.
  * Dates are stored as strings like 'Oct 1, 2020' or blank-space ' '.
  * A few data rows have 'Mid-Quarter Effective Date' / 'Mid-Quarter End
    Date' columns — we ignore these in the HCPCS table (we already use
    quarter-level ranges) but preserve the values if present.

Replaces ALL existing rows in hcpcs_to_eapg and icd10_to_eapg.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime
from typing import Any, Optional

import openpyxl
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import HcpcsToEapg, Icd10ToEapg

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cell coercion
# ---------------------------------------------------------------------------


def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s or s == "-":
            return None
        return s
    return v


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


_MONTH_ALIASES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _to_date(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"([A-Za-z]+)\s+(\d+)[,\s]+(\d{4})", s)
    if m:
        mon = _MONTH_ALIASES.get(m.group(1).lower())
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------------------
# Header detection
# ---------------------------------------------------------------------------


def _find_header_row(rows: list[list], first_col_value: str) -> Optional[int]:
    """Scan the first ~15 rows for a row where column A exactly matches
    the expected first header (e.g. 'HCPCS' or 'DX'). Case-insensitive."""
    target = first_col_value.strip().upper()
    for i, row in enumerate(rows[:15]):
        if row and row[0] is not None and str(row[0]).strip().upper() == target:
            return i
    return None


def _col_index(header: list, name: str) -> Optional[int]:
    """Find a column index by exact header match (case-insensitive, whitespace-trimmed)."""
    want = name.strip().lower()
    for i, h in enumerate(header):
        if h is None:
            continue
        if str(h).strip().lower() == want:
            return i
    return None


# ---------------------------------------------------------------------------
# EAPG Types sheet → numeric-to-string mapping
# ---------------------------------------------------------------------------


def _build_eapg_type_map(wb: openpyxl.Workbook) -> dict[str, str]:
    """Parse the 'EAPG Types' sheet into {code_str: description_str}.

    Returns an empty dict (not an error) if the sheet is missing — the
    loader will fall back to a hardcoded map below.
    """
    fallback = {
        "1": "Per Diem",
        "2": "Significant Procedure",
        "3": "Medical Visit",
        "4": "Ancillary",
        "5": "Incidental",
        "6": "Drug",
        "7": "DME",
        "8": "Unassigned",
        "9": "Add-On",
        "21": "Physical Therapy & Rehab",
        "22": "Behavioral Health & Counseling",
        "23": "Dental or Oral Surgery Procs",
        "24": "Radiologic Procedure",
        "25": "Diagnostic or Therapeutic Proc",
    }
    if "EAPG Types" not in wb.sheetnames:
        return fallback

    ws = wb["EAPG Types"]
    rows = list(ws.iter_rows(values_only=True))
    # Find header row (col A = 'EAPG Type')
    hdr_idx = _find_header_row(rows, "EAPG Type")
    if hdr_idx is None:
        return fallback

    mapping = {}
    for row in rows[hdr_idx + 1:]:
        if not row or row[0] is None:
            continue
        code = str(row[0]).strip()
        if not code or not code.isdigit():
            continue
        desc = str(row[1]).strip() if len(row) > 1 and row[1] else ""
        if desc:
            mapping[code] = desc
    # Layer fallback underneath in case the sheet omits some entries
    for k, v in fallback.items():
        mapping.setdefault(k, v)
    return mapping


# ---------------------------------------------------------------------------
# Per-sheet loaders
# ---------------------------------------------------------------------------


async def _load_hcpcs_sheet(
    session: AsyncSession, ws, eapg_type_map: dict[str, str],
) -> int:
    """Replace hcpcs_to_eapg from the "HCPCS to EAPGs" sheet."""
    rows = list(ws.iter_rows(values_only=True))
    hdr_idx = _find_header_row(rows, "HCPCS")
    if hdr_idx is None:
        raise ValueError("Could not find 'HCPCS' header row in HCPCS to EAPGs sheet.")

    header = rows[hdr_idx]
    log.info("HCPCS header at row %d: %s", hdr_idx, list(header))

    ci_hcpcs = _col_index(header, "HCPCS")
    ci_desc = _col_index(header, "Description")
    ci_eapg = _col_index(header, "EAPG")
    ci_type = _col_index(header, "EAPG Type")
    ci_cat = _col_index(header, "EAPG Category")
    ci_line = _col_index(header, "Eapg Service Line") or _col_index(header, "EAPG Service Line")
    ci_qeff = _col_index(header, "Quarter Effective Date")
    ci_qend = _col_index(header, "Quarter End Date")
    ci_meff = _col_index(header, "Mid-Quarter Effective Date")
    ci_mend = _col_index(header, "Mid-Quarter End Date")

    if ci_hcpcs is None or ci_eapg is None:
        raise ValueError("HCPCS sheet missing required columns (HCPCS, EAPG).")

    # Wipe existing rows
    await session.execute(delete(HcpcsToEapg))

    count = 0
    batch: list[HcpcsToEapg] = []
    for row in rows[hdr_idx + 1:]:
        if not row or row[ci_hcpcs] is None:
            continue
        hcpcs = _clean(row[ci_hcpcs])
        eapg = _to_int(row[ci_eapg])
        if not hcpcs or eapg is None:
            continue
        type_code = _clean(row[ci_type]) if ci_type is not None else None
        type_str = None
        if type_code is not None:
            # If it's numeric, translate via the map; otherwise use the string as-is
            code_s = str(type_code).strip()
            type_str = eapg_type_map.get(code_s, code_s)

        batch.append(HcpcsToEapg(
            hcpcs=str(hcpcs).upper(),
            description=_clean(row[ci_desc]) if ci_desc is not None else None,
            eapg=eapg,
            eapg_desc=None,  # not in this file — left empty
            eapg_type=type_str,
            eapg_category=_clean(row[ci_cat]) if ci_cat is not None else None,
            eapg_service_line=str(_clean(row[ci_line])) if ci_line is not None and _clean(row[ci_line]) is not None else None,
            quarter_effective_date=_to_date(row[ci_qeff]) if ci_qeff is not None else None,
            quarter_end_date=_to_date(row[ci_qend]) if ci_qend is not None else None,
            mid_quarter_effective_date=_to_date(row[ci_meff]) if ci_meff is not None else None,
            mid_quarter_end_date=_to_date(row[ci_mend]) if ci_mend is not None else None,
        ))
        count += 1
        if len(batch) >= 2000:
            session.add_all(batch); await session.flush(); batch.clear()
    if batch:
        session.add_all(batch); await session.flush()
    return count


async def _load_icd10_sheet(
    session: AsyncSession, ws, eapg_type_map: dict[str, str],
) -> int:
    """Replace icd10_to_eapg from the "ICD-10 DX to EAPGs" sheet."""
    rows = list(ws.iter_rows(values_only=True))
    hdr_idx = _find_header_row(rows, "DX")
    if hdr_idx is None:
        raise ValueError("Could not find 'DX' header row in ICD-10 DX to EAPGs sheet.")

    header = rows[hdr_idx]
    log.info("ICD-10 header at row %d: %s", hdr_idx, list(header))

    ci_dx = _col_index(header, "DX")
    ci_desc = _col_index(header, "Description")
    ci_gender = _col_index(header, "Gender")
    ci_eapg = _col_index(header, "EAPG")
    ci_type = _col_index(header, "EAPG Type")
    ci_cat = _col_index(header, "EAPG Category")
    ci_line = _col_index(header, "EAPG Service Line") or _col_index(header, "Eapg Service Line")
    ci_eff = _col_index(header, "Effective Date")
    ci_end = _col_index(header, "End Date")

    if ci_dx is None or ci_eapg is None:
        raise ValueError("ICD-10 sheet missing required columns (DX, EAPG).")

    await session.execute(delete(Icd10ToEapg))

    count = 0
    batch: list[Icd10ToEapg] = []
    for row in rows[hdr_idx + 1:]:
        if not row or row[ci_dx] is None:
            continue
        dx = _clean(row[ci_dx])
        eapg = _to_int(row[ci_eapg])
        if not dx or eapg is None:
            continue
        type_code = _clean(row[ci_type]) if ci_type is not None else None
        type_str = None
        if type_code is not None:
            code_s = str(type_code).strip()
            type_str = eapg_type_map.get(code_s, code_s)

        # Canonicalize the dx code so any stored row matches what the
        # engine / Rate Calculator / reference endpoints look up.
        # The Solventum source already publishes in dot-free canonical
        # form, but running through normalize_dx_code is cheap insurance
        # in case the sheet format ever changes.
        from backend.engines.apg_engine import normalize_dx_code
        canonical_dx = normalize_dx_code(dx)
        if not canonical_dx:
            continue
        batch.append(Icd10ToEapg(
            dx_code=canonical_dx,
            description=_clean(row[ci_desc]) if ci_desc is not None else None,
            gender=str(_clean(row[ci_gender])) if ci_gender is not None and _clean(row[ci_gender]) is not None else None,
            eapg=eapg,
            eapg_desc=None,
            eapg_type=type_str,
            eapg_category=_clean(row[ci_cat]) if ci_cat is not None else None,
            eapg_service_line=str(_clean(row[ci_line])) if ci_line is not None and _clean(row[ci_line]) is not None else None,
            effective_date=_to_date(row[ci_eff]) if ci_eff is not None else None,
            end_date=_to_date(row[ci_end]) if ci_end is not None else None,
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


async def load_crosswalk_from_bytes(
    session: AsyncSession, file_bytes: bytes, *, filename: str,
) -> dict:
    """Replace HCPCS + ICD-10 crosswalk tables from the eMedNY crosswalk file.

    Returns {"hcpcs_rows": N, "icd10_rows": N}. Caller commits.
    """
    if not (filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise ValueError("Crosswalk file must be .xlsx (eMedNY publishes in this format).")

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    try:
        if "HCPCS to EAPGs" not in wb.sheetnames:
            raise ValueError("Workbook is missing the 'HCPCS to EAPGs' sheet.")
        if "ICD-10 DX to EAPGs" not in wb.sheetnames:
            raise ValueError("Workbook is missing the 'ICD-10 DX to EAPGs' sheet.")

        type_map = _build_eapg_type_map(wb)
        log.info("EAPG Type mapping loaded: %d entries", len(type_map))

        n_hcpcs = await _load_hcpcs_sheet(session, wb["HCPCS to EAPGs"], type_map)
        log.info("Loaded %d HCPCS rows", n_hcpcs)

        n_icd10 = await _load_icd10_sheet(session, wb["ICD-10 DX to EAPGs"], type_map)
        log.info("Loaded %d ICD-10 rows", n_icd10)
    finally:
        wb.close()

    return {"hcpcs_rows": n_hcpcs, "icd10_rows": n_icd10}
