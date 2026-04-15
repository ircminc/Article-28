"""Initialize SQLite database and load reference data from the NYS DOH / APG workbook.

Run:
    python -m backend.db.init_db  --workbook "C:/path/to/workbook.xlsx"

Behavior:
    1. (Re)creates all tables via metadata.create_all (drops existing data first).
    2. Loads each sheet from the workbook into its target table, normalizing shape.
    3. Writes a CSV snapshot of each loaded table to backend/data/*.csv for audit.
    4. Prints row counts + sanity-check warnings if totals diverge from spec.

The workbook "shape" deviations vs. the original spec are handled here:
  - APG weights: spec had wide columns (weight_dec2008, ...); we load long form.
  - Base rates: spec had wide date columns; we load long form with source='dtc'|'hospital'.
  - HCPCS sheet has 2 extra columns (Mid-Quarter dates) — preserved.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import re
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional

import openpyxl
from sqlalchemy import delete, text

from backend.db.database import (
    ApgBaseRate,
    ApgWeight,
    Base,
    HcpcsToEapg,
    Icd10ToEapg,
    ProviderCounty,
    async_session,
    engine,
)

log = logging.getLogger("init_db")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")

CSV_DIR = Path(__file__).resolve().parent.parent / "data"

# Some sheets have a "Dec 1\n2009" column in newer NYS DOH revisions. We already
# emit a whitelist of every known effective-date column header so `parse_header_to_date`
# can complain loudly rather than silently skipping a new one.
_MONTH_ALIASES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean(v: Any) -> Any:
    """Trim whitespace on strings; leave other types alone; convert '' to None."""
    if isinstance(v, str):
        v = v.strip()
        if v == "" or v == "-":
            return None
    return v


def _to_decimal(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def _to_date(v: Any) -> Optional[date]:
    """Accept datetime, date, or strings like 'Oct 1, 2020' / '10/1/2020' / '2020-10-01'."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    # Try common formats
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Loose parse: "Jan 1, 2012" tolerant of extra whitespace
    m = re.match(r"([A-Za-z]+)\s+(\d+)[,\s]+(\d{4})", s)
    if m:
        mon = _MONTH_ALIASES.get(m.group(1).lower())
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                pass
    log.warning("Could not parse date: %r", v)
    return None


def _parse_weight_header_to_date(hdr: Any) -> Optional[date]:
    """Map a weight-sheet column header (e.g. 'Dec 1\\n2008', 'July 1 \\n2011')
    to a datetime.date. Returns None for non-date columns ('Final Rate', 'Year Rate')."""
    if hdr is None:
        return None
    s = str(hdr).replace("\n", " ").strip()
    if not s:
        return None
    low = s.lower()
    if "final" in low or "year" in low:
        return None
    m = re.match(r"([A-Za-z]+)\s*(\d+)?\s*,?\s*(\d{4})", s)
    if not m:
        return None
    mon = _MONTH_ALIASES.get(m.group(1).lower())
    if not mon:
        return None
    day = int(m.group(2)) if m.group(2) else 1
    try:
        return date(int(m.group(3)), mon, day)
    except ValueError:
        return None


def _iter_rows(ws, skip: int = 0) -> Iterable[tuple]:
    """Yield tuples of values from a worksheet, skipping N leading rows."""
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i < skip:
            continue
        yield row


# ---------------------------------------------------------------------------
# Loaders (one per sheet)
# ---------------------------------------------------------------------------


async def load_hcpcs(session, ws) -> int:
    """Sheet 'HCPCS to EAPGs' — 11 cols. Row 1 is the header."""
    expected = (
        "HCPCS", "Description", "EAPG", "EAPG Desc", "EAPG Type", "EAPG Category",
        "Eapg Service Line", "Quarter Effective Date", "Quarter End Date",
        "Mid-Quarter Effective Date", "Mid-Quarter End Date",
    )
    rows = iter(ws.iter_rows(values_only=True))
    header = tuple(str(v).strip() if v else "" for v in next(rows))
    if header[: len(expected)] != expected:
        log.warning("HCPCS sheet headers differ from expected. got=%s", header)

    await session.execute(delete(HcpcsToEapg))

    batch: list[HcpcsToEapg] = []
    count = 0
    for row in rows:
        if not row or row[0] is None:
            continue
        hcpcs = _clean(row[0])
        eapg = _to_int(row[2])
        if not hcpcs or eapg is None:
            continue
        batch.append(HcpcsToEapg(
            hcpcs=str(hcpcs),
            description=_clean(row[1]),
            eapg=eapg,
            eapg_desc=_clean(row[3]),
            eapg_type=_clean(row[4]),
            eapg_category=_clean(row[5]),
            eapg_service_line=str(_clean(row[6])) if _clean(row[6]) is not None else None,
            quarter_effective_date=_to_date(row[7]),
            quarter_end_date=_to_date(row[8]),
            mid_quarter_effective_date=_to_date(row[9]) if len(row) > 9 else None,
            mid_quarter_end_date=_to_date(row[10]) if len(row) > 10 else None,
        ))
        if len(batch) >= 2000:
            session.add_all(batch); await session.flush(); batch.clear()
        count += 1
    if batch:
        session.add_all(batch); await session.flush()
    return count


async def load_icd10(session, ws) -> int:
    """Sheet 'ICD-10 DX to EAPGs' — 10 cols. Row 1 is the header."""
    expected = (
        "DX", "Description", "Gender", "EAPG", "EAPG Desc", "EAPG Type",
        "EAPG Category", "EAPG Service Line", "Effective Date", "End Date",
    )
    rows = iter(ws.iter_rows(values_only=True))
    header = tuple(str(v).strip() if v else "" for v in next(rows))
    if header[: len(expected)] != expected:
        log.warning("ICD-10 sheet headers differ from expected. got=%s", header)

    await session.execute(delete(Icd10ToEapg))

    batch: list[Icd10ToEapg] = []
    count = 0
    for row in rows:
        if not row or row[0] is None:
            continue
        dx = _clean(row[0])
        eapg = _to_int(row[3])
        if not dx or eapg is None:
            continue
        batch.append(Icd10ToEapg(
            dx_code=str(dx),
            description=_clean(row[1]),
            gender=str(_clean(row[2])) if _clean(row[2]) is not None else None,
            eapg=eapg,
            eapg_desc=_clean(row[4]),
            eapg_type=_clean(row[5]),
            eapg_category=_clean(row[6]),
            eapg_service_line=str(_clean(row[7])) if _clean(row[7]) is not None else None,
            effective_date=_to_date(row[8]),
            end_date=_to_date(row[9]),
        ))
        if len(batch) >= 2000:
            session.add_all(batch); await session.flush(); batch.clear()
        count += 1
    if batch:
        session.add_all(batch); await session.flush()
    return count


async def load_apg_weights(session, ws) -> int:
    """Sheet 'Final APG Based Weights'.

    Wide → long transformation. Row 3 is the header. Columns:
      [0] 'APG' (numeric id)  — actually named twice; first one is numeric
      [1] 'APG'               — second col, sometimes description slot
      [2] 'APG Description'
      [3..n-2] weight-by-effective-date columns
      [n-2] 'Final Rate'
      [n-1] 'Year Rate'

    Load strategy:
      - For each APG row, emit one ApgWeight per date column (if weight is not null).
      - Also emit a sentinel row with effective_date=9999-12-31 carrying final_rate
        and year_rate so query-by-date can find it last.
    """
    # Pull all rows once into a list — sheet is only ~785 rows
    all_rows = list(ws.iter_rows(values_only=True))
    if len(all_rows) < 4:
        log.warning("APG weights sheet is too short: %d rows", len(all_rows))
        return 0

    header = all_rows[2]  # row 3 (0-indexed 2)
    date_columns: list[tuple[int, date]] = []
    final_rate_idx: Optional[int] = None
    year_rate_idx: Optional[int] = None

    for idx, h in enumerate(header):
        if h is None:
            continue
        hs = str(h).strip().lower().replace("\n", " ")
        if "final rate" in hs:
            final_rate_idx = idx
        elif "year rate" in hs or hs == "year":
            year_rate_idx = idx
        else:
            d = _parse_weight_header_to_date(h)
            if d is not None:
                date_columns.append((idx, d))

    log.info("Weight sheet: %d date columns, final_rate@%s, year_rate@%s",
             len(date_columns), final_rate_idx, year_rate_idx)

    await session.execute(delete(ApgWeight))

    SENTINEL = date(9999, 12, 31)
    count = 0
    batch: list[ApgWeight] = []
    for row in all_rows[3:]:
        if not row or row[0] is None:
            continue
        apg = _to_int(row[0])
        if apg is None:
            continue
        # Description is in column index 2 per observed sheet; fall back to 1.
        descr = _clean(row[2]) if len(row) > 2 else None
        if descr is None and len(row) > 1:
            descr = _clean(row[1])

        # Per-date weights
        for col_idx, eff_date in date_columns:
            if col_idx >= len(row):
                continue
            w = _to_decimal(row[col_idx])
            if w is None:
                continue
            batch.append(ApgWeight(
                apg=apg, apg_description=descr, effective_date=eff_date,
                weight=w, is_final_rate=False, year_rate=None,
            ))

        # Sentinel row for final_rate / year_rate
        fr = _to_decimal(row[final_rate_idx]) if final_rate_idx is not None and final_rate_idx < len(row) else None
        yr = _to_int(row[year_rate_idx]) if year_rate_idx is not None and year_rate_idx < len(row) else None
        if fr is not None and fr > 0 and yr is not None:
            batch.append(ApgWeight(
                apg=apg, apg_description=descr, effective_date=SENTINEL,
                weight=fr, is_final_rate=True, year_rate=yr,
            ))
        count += 1
        if len(batch) >= 2000:
            session.add_all(batch); await session.flush(); batch.clear()
    if batch:
        session.add_all(batch); await session.flush()
    return count


async def _load_base_rates_wide(session, ws, source: str, meta_cols: list[str]) -> int:
    """Shared long-form loader for 'APG Base Rates' (DTC) and 'Hospital APG Base Rates'.

    meta_cols is the list of pre-date column names; everything after is a date.
    Row 3 (0-indexed 2) is the header row.
    """
    all_rows = list(ws.iter_rows(values_only=True))
    if len(all_rows) < 4:
        return 0
    header = all_rows[2]

    # Find the first date column by scanning left-to-right from after meta_cols
    date_cols: list[tuple[int, date]] = []
    for idx, h in enumerate(header):
        if idx < len(meta_cols):
            continue
        d = None
        if isinstance(h, (datetime, date)):
            d = h.date() if isinstance(h, datetime) else h
        if d is not None:
            date_cols.append((idx, d))

    # Build a mapping from meta column name -> index in header
    meta_idx: dict[str, int] = {}
    for name in meta_cols:
        for idx, h in enumerate(header):
            if h is not None and str(h).strip().lower().replace("*", "") == name.lower().replace("*", ""):
                meta_idx[name] = idx
                break

    log.info("[%s] meta=%s  date_cols=%d", source, meta_idx, len(date_cols))

    count = 0
    batch: list[ApgBaseRate] = []
    for row in all_rows[3:]:
        if not row or row[0] is None:
            continue
        peer = _clean(row[meta_idx.get("Peer Group", 0)])
        if peer is None:
            continue
        region = _clean(row[meta_idx["Region"]]) if "Region" in meta_idx else None
        if region is None:
            continue
        cure_code = _clean(row[meta_idx["Cure Code"]]) if "Cure Code" in meta_idx else None
        base_rc = _clean(row[meta_idx["**Base Rate Code"]]) if "**Base Rate Code" in meta_idx else None
        blend_rc = _clean(row[meta_idx["***Blend Rate Code"]]) if "***Blend Rate Code" in meta_idx else None
        cap_rc = _clean(row[meta_idx["Capital Rate Code"]]) if "Capital Rate Code" in meta_idx else None
        cheat = _clean(row[meta_idx["cheat"]]) if "cheat" in meta_idx else None

        for col_idx, eff_date in date_cols:
            if col_idx >= len(row):
                continue
            rate = _to_decimal(row[col_idx])
            if rate is None:
                continue
            batch.append(ApgBaseRate(
                source=source,
                peer_group=str(peer),
                cure_code=str(cure_code) if cure_code is not None else None,
                base_rate_code=str(base_rc) if base_rc is not None else None,
                blend_rate_code=str(blend_rc) if blend_rc is not None else None,
                capital_rate_code=str(cap_rc) if cap_rc is not None else None,
                region=str(region),
                cheat_flag=str(cheat) if cheat is not None else None,
                effective_date=eff_date,
                rate=rate,
            ))
        count += 1
        if len(batch) >= 2000:
            session.add_all(batch); await session.flush(); batch.clear()
    if batch:
        session.add_all(batch); await session.flush()
    return count


async def load_apg_base_rates(session, ws) -> int:
    return await _load_base_rates_wide(
        session, ws, source="dtc",
        meta_cols=["Peer Group", "Cure Code", "**Base Rate Code",
                   "***Blend Rate Code", "Capital Rate Code", "Region"],
    )


async def load_hospital_base_rates(session, ws) -> int:
    return await _load_base_rates_wide(
        session, ws, source="hospital",
        meta_cols=["Peer Group", "**Base Rate Code", "***Blend Rate Code",
                   "Capital Rate Code", "Region", "cheat"],
    )


async def load_provider_county(session, ws) -> int:
    """Sheet 'Provider County' — 4 cols, row 1 header."""
    rows = iter(ws.iter_rows(values_only=True))
    next(rows)  # skip header
    await session.execute(delete(ProviderCounty))
    count = 0
    batch: list[ProviderCounty] = []
    for row in rows:
        if not row or row[0] is None:
            continue
        code = _to_int(row[1])
        name = _clean(row[0])
        if not name or code is None:
            continue
        batch.append(ProviderCounty(
            county_code=code,
            county_name=str(name),
            health_home_phase=str(_clean(row[2])) if _clean(row[2]) is not None else None,
            region=str(_clean(row[3])),
        ))
        count += 1
    if batch:
        session.add_all(batch)
        await session.flush()
    return count


# ---------------------------------------------------------------------------
# CSV snapshot export
# ---------------------------------------------------------------------------


async def export_csv_snapshots() -> None:
    """Write each reference table to backend/data/*.csv for human audit.

    Uses synchronous sqlite3 because aiosqlite cursor for CSV streaming is overkill;
    only runs once per init_db call and the tables are <100K rows.
    """
    import sqlite3

    CSV_DIR.mkdir(parents=True, exist_ok=True)

    db_url = engine.url.database  # e.g. './data/apg_analyzer.db'
    if not db_url:
        return
    # Resolve relative to working dir like the engine does
    db_path = Path(db_url)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path

    tables = [
        ("hcpcs_to_eapg", "hcpcs_to_eapg.csv"),
        ("icd10_to_eapg", "icd10_to_eapg.csv"),
        ("apg_weights", "apg_weights.csv"),
        ("apg_base_rates", "apg_base_rates.csv"),
        ("provider_county", "provider_county.csv"),
    ]

    with sqlite3.connect(db_path) as conn:
        for table, fname in tables:
            out = CSV_DIR / fname
            cur = conn.execute(f"SELECT * FROM {table}")
            cols = [d[0] for d in cur.description]
            with out.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for row in cur:
                    w.writerow(row)
            log.info("Wrote CSV snapshot: %s", out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run(workbook_path: Path) -> None:
    log.info("Dropping + creating schema...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    log.info("Opening workbook: %s", workbook_path)
    wb = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        async with async_session() as session:
            def ws(name: str):
                if name not in wb.sheetnames:
                    raise RuntimeError(f"Sheet not found: {name!r} (available: {wb.sheetnames})")
                return wb[name]

            log.info("Loading HCPCS → EAPG...")
            n_hcpcs = await load_hcpcs(session, ws("HCPCS to EAPGs"))
            log.info("  %d rows", n_hcpcs)

            log.info("Loading ICD-10 → EAPG...")
            n_icd = await load_icd10(session, ws("ICD-10 DX to EAPGs"))
            log.info("  %d rows", n_icd)

            log.info("Loading APG weights (wide → long)...")
            n_w = await load_apg_weights(session, ws("Final APG Based Weights"))
            log.info("  %d distinct APGs processed", n_w)

            log.info("Loading APG base rates (DTC, wide → long)...")
            n_dtc = await load_apg_base_rates(session, ws("APG Base Rates"))
            log.info("  %d DTC rows processed", n_dtc)

            log.info("Loading APG base rates (Hospital, wide → long)...")
            n_hosp = await load_hospital_base_rates(session, ws("Hospital APG Base Rates"))
            log.info("  %d hospital rows processed", n_hosp)

            log.info("Loading provider counties...")
            n_c = await load_provider_county(session, ws("Provider County"))
            log.info("  %d rows", n_c)

            await session.commit()
    finally:
        wb.close()

    # Post-load sanity summaries
    async with async_session() as session:
        stats = []
        for tbl in ("hcpcs_to_eapg", "icd10_to_eapg", "apg_weights", "apg_base_rates", "provider_county"):
            res = await session.execute(text(f"SELECT COUNT(*) FROM {tbl}"))
            stats.append((tbl, res.scalar_one()))
    log.info("Database totals:")
    for tbl, n in stats:
        log.info("  %-24s %d", tbl, n)

    log.info("Exporting CSV snapshots...")
    await export_csv_snapshots()
    log.info("Done.")


def main():
    p = argparse.ArgumentParser(description="Initialize APG reference database from workbook.")
    p.add_argument("--workbook", required=True, type=Path,
                   help="Path to the NYS DOH / APG reference .xlsx workbook")
    args = p.parse_args()

    if not args.workbook.exists():
        print(f"Workbook not found: {args.workbook}", file=sys.stderr)
        sys.exit(2)

    asyncio.run(run(args.workbook))


if __name__ == "__main__":
    main()
