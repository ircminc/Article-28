"""Tests for the PMTAC 'Updated APG Fee Calculator' base-rates loader."""
from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal

import openpyxl
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from backend.db.database import ApgBaseRate, Base, async_session, engine
from backend.db.init_apg_base_rates_v2 import (
    _parse_date_header,
    _to_decimal,
    load_apg_base_rates_v2_from_bytes,
)


# ---------------------------------------------------------------------------
# Header / cell parsers — pure functions, no DB required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (datetime(2022, 4, 1),        date(2022, 4, 1)),
        (datetime(2015, 4, 1, 12, 0), date(2015, 4, 1)),
        ("4/1/2022****",              date(2022, 4, 1)),
        ("4/1/2022*",                 date(2022, 4, 1)),
        ("4/1/2022",                  date(2022, 4, 1)),
        ("2022-04-01",                date(2022, 4, 1)),
        ("10-15-2020",                date(2020, 10, 15)),
        ("Effective Date",            None),
        (None,                        None),
        ("",                          None),
    ],
)
def test_parse_date_header(raw, expected):
    assert _parse_date_header(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (170.71,    Decimal("170.71")),
        ("170.71",  Decimal("170.71")),
        ("$170.71", Decimal("170.71")),
        ("1,234.56",Decimal("1234.56")),
        (0,         Decimal("0")),
        ("",        None),
        (None,      None),
        ("abc",     None),
    ],
)
def test_to_decimal(raw, expected):
    assert _to_decimal(raw) == expected


# ---------------------------------------------------------------------------
# End-to-end loader against a synthetic workbook mimicking the real layout
# ---------------------------------------------------------------------------


def _build_synthetic_workbook() -> bytes:
    """Mimic the real 'Updated APG Base Rate' sheet layout tightly enough
    that the loader's header detection + data-block parsing code exercises
    the same paths it would on the production file."""
    wb = openpyxl.Workbook()
    # Drop the default sheet + add the required one
    ws = wb.active
    ws.title = "Updated APG Base Rate"

    # Row 1 (index 0): section title
    ws.cell(row=1, column=1, value="Freestanding Clinic and Ambulatory Surgery APG Base Rates")
    # Row 2 (index 1): blank
    # Row 3 (index 2): header row
    headers = [
        "Peer Group", "**Base Rate Code", "***Blend Rate Code",
        "Capital Rate Code", "Region", "",
        datetime(2015, 4, 1), "4/1/2022****",
    ]
    for c, v in enumerate(headers, start=1):
        ws.cell(row=3, column=c, value=v)
    # Row 4+: data
    data = [
        ("Clinic*",          "1407", "1411", "1409", "Downstate", "",  169.02, 170.71),
        ("Clinic*",          "1407", "1411", "1409", "Upstate",   "",  141.64, 143.06),
        ("Amb Surg",         "1408", "1412", "1410", "Downstate", "",  116.24, 117.40),
        ("Clinic Episode*",  "1422", "1423", "1424", "Downstate", "",  169.02, 170.71),
        # Empty row marks end of data
        ("",) * 8,
        # Footnote rows (should be skipped)
        ("*For Clinic and SBHC peer groups, the Blend rate applies…",) + ("",) * 7,
    ]
    for r_off, row in enumerate(data, start=4):
        for c, v in enumerate(row, start=1):
            ws.cell(row=r_off, column=c, value=v)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest_asyncio.fixture()
async def clean_dtc_rates():
    """Snapshot source='dtc' rows → wipe → run test → restore."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as snap_s:
        snapshot = (await snap_s.execute(
            select(ApgBaseRate).where(ApgBaseRate.source == "dtc")
        )).scalars().all()
        snap_data = [
            dict(
                source=r.source, peer_group=r.peer_group,
                base_rate_code=r.base_rate_code, blend_rate_code=r.blend_rate_code,
                capital_rate_code=r.capital_rate_code, region=r.region,
                effective_date=r.effective_date, rate=r.rate,
                cure_code=r.cure_code, cheat_flag=r.cheat_flag,
            )
            for r in snapshot
        ]

    async with async_session() as s:
        await s.execute(delete(ApgBaseRate).where(ApgBaseRate.source == "dtc"))
        await s.commit()

    async with async_session() as s:
        yield s

    # Restore
    async with async_session() as s:
        await s.execute(delete(ApgBaseRate).where(ApgBaseRate.source == "dtc"))
        for row in snap_data:
            s.add(ApgBaseRate(**row))
        await s.commit()


@pytest.mark.asyncio
async def test_load_synthetic_workbook(clean_dtc_rates):
    data = _build_synthetic_workbook()
    s = clean_dtc_rates
    result = await load_apg_base_rates_v2_from_bytes(s, data, filename="synthetic.xlsx")
    await s.commit()

    # Data shape
    assert result["rows_inserted"] == 8      # 4 peer-group/region × 2 dates
    # 3 distinct peer groups: Clinic*, Amb Surg, Clinic Episode*
    # (Clinic* appears twice — Downstate + Upstate — but that's one group)
    assert result["peer_groups"] == 3
    assert result["effective_dates"] == 2
    assert result["most_recent_effective_date"] == "2022-04-01"

    # Spot: Clinic*/Downstate most-recent rate = 170.71
    q = await s.execute(
        select(ApgBaseRate)
        .where(
            ApgBaseRate.source == "dtc",
            ApgBaseRate.peer_group == "Clinic*",
            ApgBaseRate.region == "Downstate",
        )
        .order_by(ApgBaseRate.effective_date.desc())
        .limit(1)
    )
    r = q.scalar_one()
    assert r.effective_date == date(2022, 4, 1)
    assert r.rate == Decimal("170.71")
    assert r.base_rate_code == "1407"
    assert r.blend_rate_code == "1411"
    assert r.capital_rate_code == "1409"

    # Footnote row must not leak in as a peer group
    q = await s.execute(
        select(ApgBaseRate).where(
            ApgBaseRate.source == "dtc",
            ApgBaseRate.peer_group.like("*For Clinic%"),
        )
    )
    assert q.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_load_rejects_wrong_sheet_layout(clean_dtc_rates):
    """If 'Updated APG Base Rate' sheet is missing OR its header row doesn't
    start with 'Peer Group', the loader refuses with a clear error."""
    wb = openpyxl.Workbook()
    wb.active.title = "Some Other Sheet"
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ValueError, match="not found in workbook"):
        await load_apg_base_rates_v2_from_bytes(
            clean_dtc_rates, buf.getvalue(), filename="bad.xlsx",
        )
