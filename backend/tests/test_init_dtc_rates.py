"""Tests for the DTC base-rates partial loader."""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import openpyxl
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from backend.db.database import ApgBaseRate, Base, async_session, engine
from backend.db.init_dtc_rates import (
    _header_to_date,
    _match_header_idx,
    load_dtc_rates_from_bytes,
)


@pytest_asyncio.fixture()
async def clean_rates():
    """Snapshot the apg_base_rates table → wipe → let the test run → restore.

    This keeps tests that come later (e.g. test_apg_engine) from failing
    because our destructive tests left the shared dev DB empty.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Snapshot
    async with async_session() as snap_s:
        snapshot = (await snap_s.execute(select(ApgBaseRate))).scalars().all()
        snapshot_data = [
            dict(
                source=r.source, peer_group=r.peer_group, cure_code=r.cure_code,
                base_rate_code=r.base_rate_code, blend_rate_code=r.blend_rate_code,
                capital_rate_code=r.capital_rate_code, region=r.region,
                cheat_flag=r.cheat_flag, effective_date=r.effective_date, rate=r.rate,
            )
            for r in snapshot
        ]

    # Reset to a minimal known state for the test: wipe + seed 1 hospital row
    async with async_session() as s:
        await s.execute(delete(ApgBaseRate))
        s.add(ApgBaseRate(
            source="hospital", peer_group="Clinic*", region="Downstate",
            effective_date=date(2012, 5, 1), rate=Decimal("183.53"),
        ))
        await s.commit()

    async with async_session() as s:
        try:
            yield s
        finally:
            pass

    # Restore
    async with async_session() as s:
        await s.execute(delete(ApgBaseRate))
        for d in snapshot_data:
            s.add(ApgBaseRate(**d))
        await s.commit()


def _build_test_xlsx(
    headers: list,
    data_rows: list[list],
    sheet_name: str = "Freestanding APG Base Rates",
    title_row: str = "Freestanding Clinic and Ambulatory Surgery Center APG Base Rates",
) -> bytes:
    """Produce an in-memory .xlsx with the canonical NYS DOH layout:
        row 0: title, row 1: blank, row 2: headers, row 3+: data."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append([title_row])
    ws.append([])
    ws.append(headers)
    for r in data_rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Header-parsing helpers
# ---------------------------------------------------------------------------


def test_header_to_date_accepts_native_date_objects():
    assert _header_to_date(date(2015, 4, 1)) == date(2015, 4, 1)


def test_header_to_date_strips_footnote_asterisks():
    """NYS DOH's file uses '4/1/2022****' for the newest column — the four
    asterisks are a footnote marker, not part of the date."""
    assert _header_to_date("4/1/2022****") == date(2022, 4, 1)


def test_header_to_date_ignores_non_date_headers():
    assert _header_to_date("Peer Group") is None
    assert _header_to_date("") is None
    assert _header_to_date(None) is None


def test_match_header_idx_ignores_asterisks_and_case():
    header = ["Peer Group", "**Base Rate Code", "***Blend Rate Code", "Region"]
    assert _match_header_idx(header, "Peer Group") == 0
    assert _match_header_idx(header, "Base Rate Code") == 1
    assert _match_header_idx(header, "BLEND RATE CODE") == 2
    assert _match_header_idx(header, "Region") == 3
    assert _match_header_idx(header, "Missing Column") is None


# ---------------------------------------------------------------------------
# End-to-end loader tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_inserts_rows_and_preserves_hospital(clean_rates):
    s = clean_rates
    headers = [
        "Peer Group", "**Base Rate Code", "***Blend Rate Code",
        "Capital Rate Code", "Region",
        date(2015, 4, 1), "4/1/2022****",
    ]
    data = [
        ["Clinic*",  1407, None, None, "Downstate", 169.02, 170.71],
        ["Clinic*",  1407, None, None, "Upstate",   150.40, 152.10],
        ["Amb Surg", 1408, None, None, "Downstate", 210.50, 215.00],
    ]
    xlsx = _build_test_xlsx(headers, data)

    deleted, inserted = await load_dtc_rates_from_bytes(
        s, xlsx, filename="dtc_rates.xlsx",
    )
    await s.commit()

    # No old DTC rows to delete in the fixture
    assert deleted == 0
    # 3 peer-group rows × 2 date columns = 6 rate rows
    assert inserted == 6

    # Hospital row preserved
    q = await s.execute(select(func.count()).select_from(ApgBaseRate)
                          .where(ApgBaseRate.source == "hospital"))
    assert q.scalar_one() == 1

    # Confirm the 2022 Clinic*/Downstate rate
    q = await s.execute(
        select(ApgBaseRate).where(
            ApgBaseRate.source == "dtc",
            ApgBaseRate.peer_group == "Clinic*",
            ApgBaseRate.region == "Downstate",
            ApgBaseRate.effective_date == date(2022, 4, 1),
        )
    )
    row = q.scalar_one()
    assert row.rate == Decimal("170.71")
    assert row.base_rate_code == "1407"


@pytest.mark.asyncio
async def test_loader_replaces_existing_dtc_rows(clean_rates):
    """Re-running the loader should delete ALL prior DTC rows before inserting."""
    s = clean_rates
    # Seed some stale DTC rows first
    for eff_date, rate in [(date(2015, 4, 1), Decimal("100")), (date(2018, 1, 1), Decimal("110"))]:
        s.add(ApgBaseRate(
            source="dtc", peer_group="Clinic*", region="Downstate",
            effective_date=eff_date, rate=rate,
        ))
    await s.commit()

    # Now upload a new file
    headers = ["Peer Group", "Region", date(2022, 4, 1)]
    data = [["Clinic*", "Downstate", 170.71]]
    xlsx = _build_test_xlsx(headers, data)

    deleted, inserted = await load_dtc_rates_from_bytes(
        s, xlsx, filename="new.xlsx",
    )
    await s.commit()

    assert deleted == 2
    assert inserted == 1

    # Only the new row remains
    q = await s.execute(select(ApgBaseRate).where(ApgBaseRate.source == "dtc"))
    rows = q.scalars().all()
    assert len(rows) == 1
    assert rows[0].effective_date == date(2022, 4, 1)
    assert rows[0].rate == Decimal("170.71")


@pytest.mark.asyncio
async def test_loader_rejects_missing_peer_group_column(clean_rates):
    xlsx = _build_test_xlsx(
        headers=["Something Else", "Region", date(2022, 4, 1)],
        data_rows=[["Clinic*", "Downstate", 170.71]],
    )
    with pytest.raises(ValueError, match="header row"):
        await load_dtc_rates_from_bytes(
            clean_rates, xlsx, filename="bad.xlsx",
        )


@pytest.mark.asyncio
async def test_loader_rejects_file_with_no_date_columns(clean_rates):
    xlsx = _build_test_xlsx(
        headers=["Peer Group", "Region", "Some Code"],
        data_rows=[["Clinic*", "Downstate", "ABC123"]],
    )
    with pytest.raises(ValueError, match="date columns"):
        await load_dtc_rates_from_bytes(
            clean_rates, xlsx, filename="bad.xlsx",
        )


@pytest.mark.asyncio
async def test_loader_skips_blank_or_footer_rows(clean_rates):
    """Empty cells and text-only footer rows should be silently ignored."""
    headers = ["Peer Group", "Region", date(2022, 4, 1)]
    data = [
        ["Clinic*", "Downstate", 170.71],
        [None, None, None],                              # blank
        ["Amb Surg", "Upstate", 215.00],
        ["* footnote about something", None, None],      # footer
        ["Rates not available during this period", None, None],
    ]
    xlsx = _build_test_xlsx(headers, data)
    _, inserted = await load_dtc_rates_from_bytes(
        clean_rates, xlsx, filename="with_footers.xlsx",
    )
    assert inserted == 2   # only the two real rows count


@pytest.mark.asyncio
async def test_loader_skips_missing_rate_cells(clean_rates):
    """Some peer-group/region combos have no rate for certain effective dates;
    those cells should be skipped (not inserted as 0)."""
    headers = ["Peer Group", "Region", date(2015, 4, 1), date(2022, 4, 1)]
    data = [
        ["Clinic*", "Downstate", 169.02, 170.71],
        ["Clinic Episode*", "Downstate", None, 175.00],   # no 2015 rate
    ]
    xlsx = _build_test_xlsx(headers, data)
    _, inserted = await load_dtc_rates_from_bytes(
        clean_rates, xlsx, filename="sparse.xlsx",
    )
    # 2 for Clinic*, 1 for Clinic Episode* → 3 total
    assert inserted == 3
