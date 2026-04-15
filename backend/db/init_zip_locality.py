"""Load the CMS ZIP5 → Medicare locality mapping into the `zip_locality` table.

Source: CMS publishes an annual "ZIP Code to Carrier Locality File" at
  https://www.cms.gov/medicare/medicare-fee-service-payments/physicianfeesched/zip-code-carrier-locality-file

That file comes as an Excel (.xlsx) workbook with one row per ZIP. Column names
vary slightly year-to-year; this loader is tolerant — it matches by fuzzy column
header names to pick up the essential fields:

    ZIP CODE, STATE, CARRIER, LOCALITY, RURAL INDICATOR, PLUS 4 FLAG, YEAR

Any year column in the filename or inside the workbook is used as `effective_year`.
If the workbook is a CSV, that's fine too — the loader sniffs the extension.

Usage:
    python -m backend.db.init_zip_locality --file "C:/path/to/ZIP5_OCT2025.xlsx"
    python -m backend.db.init_zip_locality --file "C:/path/to/zip_locality.csv"

`--year` can be passed to set effective_year explicitly if the filename doesn't
include a year and the sheet has no year column.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import delete

from backend.db.database import ZipLocality, async_session, engine, Base

log = logging.getLogger("init_zip_locality")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")


# Fuzzy column-name matchers. Order matters — first match wins.
_COLUMN_ALIASES = {
    "zip_code": ["zip code", "zip5", "zip", "zipcode"],
    "state": ["state", "st"],
    "carrier_number": ["carrier", "carrier number", "mac"],
    "locality_number": ["locality", "locality number", "locality no"],
    "locality_name": ["locality name", "locality description"],
    "rural_indicator": ["rural indicator", "rural ind", "rural"],
    "plus4_flag": ["plus 4 flag", "plus4 flag", "plus 4"],
    "effective_year": ["year", "effective year"],
}


def _match_headers(raw_headers: list[str]) -> dict[str, int]:
    """Return a mapping field → column-index for the supplied raw header row."""
    lowered = [(i, (h or "").strip().lower()) for i, h in enumerate(raw_headers)]
    out: dict[str, int] = {}
    for field, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            for i, h in lowered:
                if h == alias.lower():
                    out[field] = i
                    break
            if field in out:
                break
    return out


def _infer_year_from_filename(path: Path) -> Optional[int]:
    m = re.search(r"(20\d{2})", path.name)
    return int(m.group(1)) if m else None


def _iter_xlsx(path: Path) -> Iterable[tuple]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            yield row
    finally:
        wb.close()


def _iter_csv(path: Path) -> Iterable[tuple]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        for row in csv.reader(f):
            yield tuple(row)


def _iter_rows(path: Path) -> Iterable[tuple]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        yield from _iter_xlsx(path)
    elif path.suffix.lower() in (".csv", ".txt", ".tsv"):
        yield from _iter_csv(path)
    else:
        raise ValueError(f"Unsupported file type: {path.suffix!r} (expected .xlsx or .csv)")


def _to_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def _normalize_zip(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    m = re.match(r"(\d{5})", s)
    return m.group(1) if m else None


async def run(path: Path, explicit_year: Optional[int]) -> None:
    log.info("Loading ZIP → locality from %s", path)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    year_from_name = _infer_year_from_filename(path)
    default_year = explicit_year or year_from_name
    log.info("Default effective_year = %s (from %s)", default_year,
             "--year" if explicit_year else ("filename" if year_from_name else "none"))

    async with async_session() as session:
        await session.execute(delete(ZipLocality))

        rows_iter = _iter_rows(path)
        header = None
        header_map: dict[str, int] = {}
        count = 0
        batch: list[ZipLocality] = []

        for raw in rows_iter:
            if header is None:
                header = raw
                header_map = _match_headers(list(raw))
                if "zip_code" not in header_map or "locality_number" not in header_map:
                    raise RuntimeError(
                        f"Could not find required columns. Got headers: {list(raw)}"
                    )
                log.info("Header map: %s", header_map)
                continue

            def get(field: str):
                idx = header_map.get(field)
                if idx is None or idx >= len(raw):
                    return None
                return raw[idx]

            zip5 = _normalize_zip(get("zip_code"))
            locality = _to_str(get("locality_number"))
            if not zip5 or not locality:
                continue

            eff_year = _to_int(get("effective_year")) or default_year

            batch.append(ZipLocality(
                zip_code=zip5,
                state=_to_str(get("state")),
                carrier_number=_to_str(get("carrier_number")),
                locality_number=locality,
                locality_name=_to_str(get("locality_name")),
                rural_indicator=_to_str(get("rural_indicator")),
                plus4_flag=_to_str(get("plus4_flag")),
                effective_year=eff_year,
            ))
            count += 1
            if len(batch) >= 2000:
                session.add_all(batch); await session.flush(); batch.clear()

        if batch:
            session.add_all(batch); await session.flush()
        await session.commit()

    log.info("Loaded %d ZIP → locality rows", count)


def main():
    p = argparse.ArgumentParser(description="Load CMS ZIP → locality mapping.")
    p.add_argument("--file", required=True, type=Path,
                   help="Path to CMS ZIP5 .xlsx or equivalent CSV")
    p.add_argument("--year", type=int, default=None,
                   help="effective_year to use when the sheet has no year column")
    args = p.parse_args()

    if not args.file.exists():
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(2)

    asyncio.run(run(args.file, args.year))


if __name__ == "__main__":
    main()
