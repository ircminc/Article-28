"""Multi-sheet Excel export for APG analyzer reports.

Generates a workbook with the following sheets (per the project spec):
  1. Summary        — KPIs and filter summary
  2. 835I Claims    — per-claim Article 28 detail with APG breakdown
  3. 835P Claims    — per-claim professional detail (charges, CAS, no APG)
  4. EAPG Breakdown — count/billed/paid/expected/variance per EAPG
  5. Denial Analysis — CARC codes ordered by total dollar impact
  6. Rate Reference — HCPCS→EAPG and base rates used across loaded claims

Formatting conventions:
  - Arial 11pt base, bold white on brand-navy for headers
  - Currency cells: $#,##0.00
  - Percent cells: 0.0000%
  - Conditional fills on claim detail: red (variance > 10%), amber (1-10%), green (<1%)
  - Column widths: reasonable defaults, key identifier columns wider
  - Summary sheet has a filter-applied block showing date range / payer / etc
"""
from __future__ import annotations

import io
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Side, Border
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Style tokens (match the UI's brand colors)
# ---------------------------------------------------------------------------

BRAND_NAVY = "FF1A2E4A"
PRIMARY_BLUE = "FF2563EB"
SUCCESS_GREEN = "FFDCFCE7"     # light green (cell fill)
WARNING_AMBER = "FFFEF3C7"     # light amber
DANGER_RED = "FFFEE2E2"        # light red
SLATE_50 = "FFF8FAFC"
SLATE_200 = "FFE2E8F0"

BASE_FONT = Font(name="Arial", size=11)
BOLD_WHITE = Font(name="Arial", size=11, bold=True, color="FFFFFFFF")
BOLD = Font(name="Arial", size=11, bold=True)
MUTED_ITALIC = Font(name="Arial", size=10, italic=True, color="FF64748B")

HEADER_FILL = PatternFill("solid", fgColor=BRAND_NAVY)
SECTION_FILL = PatternFill("solid", fgColor=SLATE_50)
FILL_SUCCESS = PatternFill("solid", fgColor=SUCCESS_GREEN)
FILL_WARNING = PatternFill("solid", fgColor=WARNING_AMBER)
FILL_DANGER = PatternFill("solid", fgColor=DANGER_RED)

THIN = Side(style="thin", color="FFCBD5E1")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

FMT_MONEY = '_-"$"* #,##0.00_-;-"$"* #,##0.00_-;_-"$"* "-"??_-;_-@_-'
FMT_PCT = "0.0000%"
FMT_INT = "#,##0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _header_row(ws: Worksheet, row: int, headers: list[str]) -> None:
    for col, title in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=title)
        cell.font = BOLD_WHITE
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", horizontal="left")
        cell.border = BORDER
    ws.row_dimensions[row].height = 22


def _autosize(ws: Worksheet, min_width: int = 8, max_width: int = 60) -> None:
    """Heuristic column-width pass. Walks cells; caps width to avoid megacolumns."""
    for col in ws.columns:
        letter = get_column_letter(col[0].column)
        longest = 0
        for cell in col:
            v = cell.value
            if v is None:
                continue
            length = len(str(v))
            if length > longest:
                longest = length
        ws.column_dimensions[letter].width = max(min_width, min(max_width, longest + 2))


def _to_decimal(v) -> Decimal:
    if v is None or v == "":
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


def _variance_fill(variance_pct: Decimal) -> Optional[PatternFill]:
    """Return a conditional fill based on compression severity. Positive variance
    means underpaid — red if >10%, amber if 1-10%, green otherwise."""
    abs_pct = abs(variance_pct)
    if abs_pct >= Decimal("10"):
        return FILL_DANGER
    if abs_pct >= Decimal("1"):
        return FILL_WARNING
    return FILL_SUCCESS


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------


class ExcelExporter:
    """Builds the report workbook. Caller supplies pre-fetched data structures
    (so the exporter does no DB work — easy to test with fixtures).

    Expected payload:
        {
            "generated_at": datetime,
            "filters": {"date_from": ..., "payer_name": ..., ...},
            "provider": {...},                  # provider config dict
            "summary": {...},                   # AnalyticsEngine.summary() output
            "claims_835i": [claim-detail dicts],
            "claims_835p": [claim-detail dicts],
            "eapg_breakdown": {"rows": [...]},  # AnalyticsEngine.compression(group_by='eapg')
            "denials": {"rows": [...]},         # AnalyticsEngine.denials()
            "reference_codes": [                # HCPCS observations
                {"hcpcs": ..., "eapg": ..., "eapg_desc": ..., "effective": ...},
            ],
            "base_rates": [                     # base rates used
                {"source": ..., "peer_group": ..., "region": ..., "effective_date": ..., "rate": ...}
            ],
        }
    """

    def build(self, payload: dict) -> bytes:
        wb = Workbook()
        # openpyxl creates an empty first sheet; rename to Summary rather than delete.
        ws_summary = wb.active
        ws_summary.title = "Summary"
        self._write_summary(ws_summary, payload)

        self._write_claims_835i(wb.create_sheet("835I Claims"), payload.get("claims_835i", []))
        self._write_claims_835p(wb.create_sheet("835P Claims"), payload.get("claims_835p", []))
        self._write_eapg_breakdown(wb.create_sheet("EAPG Breakdown"), payload.get("eapg_breakdown", {}))
        self._write_denials(wb.create_sheet("Denial Analysis"), payload.get("denials", {}))
        self._write_reference(wb.create_sheet("Rate Reference"),
                              payload.get("reference_codes", []),
                              payload.get("base_rates", []))

        # Apply base font to any cells we forgot
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if cell.font is None or cell.font.name != "Arial":
                        # Only override if the default came through
                        if cell.font is None:
                            cell.font = BASE_FONT

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    # -----------------------------------------------------------------
    # Summary sheet
    # -----------------------------------------------------------------

    def _write_summary(self, ws: Worksheet, payload: dict) -> None:
        ws.column_dimensions["A"].width = 32
        ws.column_dimensions["B"].width = 24

        # Title
        ws["A1"] = "APG 835/837 Rate Analyzer — Report"
        ws["A1"].font = Font(name="Arial", size=16, bold=True, color=BRAND_NAVY)
        ws.merge_cells("A1:D1")

        gen_at = payload.get("generated_at")
        if isinstance(gen_at, datetime):
            gen_at = gen_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        ws["A2"] = f"Generated: {gen_at or '—'}"
        ws["A2"].font = MUTED_ITALIC
        ws.merge_cells("A2:D2")

        # Provider block
        row = 4
        ws.cell(row=row, column=1, value="Provider").font = BOLD
        provider = payload.get("provider") or {}
        for label, key in [
            ("Name", "provider_name"), ("NPI", "npi"),
            ("Peer group", "peer_group"), ("Type", "provider_type"),
            ("Region", "region"), ("County code", "county_code"),
            ("CMS locality", "cms_locality"),
        ]:
            row += 1
            ws.cell(row=row, column=1, value=label)
            ws.cell(row=row, column=2, value=str(provider.get(key) or "—"))

        # Filters block
        row += 2
        ws.cell(row=row, column=1, value="Filters").font = BOLD
        filters = payload.get("filters") or {}
        for k, v in filters.items():
            row += 1
            ws.cell(row=row, column=1, value=k)
            ws.cell(row=row, column=2, value=str(v) if v is not None else "—")
        if not filters:
            row += 1
            ws.cell(row=row, column=1, value="(none)").font = MUTED_ITALIC

        # KPIs block
        row += 2
        ws.cell(row=row, column=1, value="KPIs").font = BOLD
        row += 1
        kpi_headers = ["Metric", "Value"]
        _header_row(ws, row, kpi_headers)
        summary = payload.get("summary") or {}
        for label, key, fmt in [
            ("Total claims",              "total_claims",                FMT_INT),
            ("Total billed",              "total_billed",                FMT_MONEY),
            ("Total paid",                "total_paid",                  FMT_MONEY),
            ("Paid % of billed",          "paid_as_pct_of_billed",       FMT_PCT),
            ("Denied claims",             "total_denied",                FMT_INT),
            ("Denial rate",               "denial_rate_pct",             FMT_PCT),
            ("APG claims",                "apg_claims",                  FMT_INT),
            ("APG correct payment total", "apg_correct_payment_total",   FMT_MONEY),
            ("APG actual paid total",     "apg_actual_paid_total",       FMT_MONEY),
            ("APG variance total",        "apg_total_variance",          FMT_MONEY),
            ("APG underpayment total",    "apg_underpayment_total",      FMT_MONEY),
            ("APG avg compression %",     "apg_avg_compression_pct",     FMT_PCT),
        ]:
            row += 1
            ws.cell(row=row, column=1, value=label).border = BORDER
            raw = summary.get(key)
            val = _to_decimal(raw) if fmt in (FMT_MONEY,) else raw
            # Percent fields come as strings like "12.3400" — convert to a
            # decimal fraction for Excel's '0.0000%' format (4dp = 2dp pct).
            if fmt == FMT_PCT and raw is not None:
                try:
                    val = float(Decimal(str(raw)) / Decimal("100"))
                except Exception:
                    val = raw
            cell = ws.cell(row=row, column=2, value=val)
            cell.number_format = fmt
            cell.border = BORDER

    # -----------------------------------------------------------------
    # 835I claims sheet
    # -----------------------------------------------------------------

    def _write_claims_835i(self, ws: Worksheet, claims: list[dict]) -> None:
        headers = [
            "Claim ID", "DOS", "Patient", "NPI", "Payer",
            "Billed", "Allowed", "Paid",
            "Correct APG", "Variance", "Compression %",
            "Peer Group", "Region", "Base Rate",
            "Discounting", "U6", "Capital", "Principal DX",
        ]
        _header_row(ws, 1, headers)
        for i, c in enumerate(claims, start=2):
            apg = c.get("apg_result") or {}
            row = [
                c.get("claim_id"),
                c.get("date_of_service"),
                c.get("patient_name"),
                c.get("provider_npi"),
                c.get("payer_name"),
                _to_decimal(c.get("billed_amount")),
                _to_decimal(c.get("allowed_amount")),
                _to_decimal(c.get("paid_amount")),
                _to_decimal(apg.get("correct_apg_payment")),
                _to_decimal(apg.get("variance")),
                _to_decimal(apg.get("compression_pct")) / Decimal("100")
                    if apg.get("compression_pct") is not None else None,
                apg.get("peer_group"),
                apg.get("region"),
                _to_decimal(apg.get("base_rate_applied")),
                "Yes" if apg.get("discounting_applied") else "No",
                "Yes" if apg.get("u6_applied") else "No",
                "Yes" if apg.get("capital_applied") else "No",
                c.get("principal_diagnosis"),
            ]
            for j, v in enumerate(row, start=1):
                cell = ws.cell(row=i, column=j, value=v)
                cell.border = BORDER
                if j in (6, 7, 8, 9, 10, 14):
                    cell.number_format = FMT_MONEY
                elif j == 11:
                    cell.number_format = FMT_PCT
            # Conditional fill on the variance + compression columns
            comp_pct = _to_decimal(apg.get("compression_pct"))
            fill = _variance_fill(comp_pct)
            if fill is not None and apg:
                ws.cell(row=i, column=10).fill = fill
                ws.cell(row=i, column=11).fill = fill

        _autosize(ws, min_width=10)
        ws.freeze_panes = "A2"

    # -----------------------------------------------------------------
    # 835P claims sheet
    # -----------------------------------------------------------------

    def _write_claims_835p(self, ws: Worksheet, claims: list[dict]) -> None:
        headers = [
            "Claim ID", "DOS", "Patient", "NPI", "Payer",
            "Billed", "Allowed", "Paid",
            "Patient Resp", "Filing", "Status",
        ]
        _header_row(ws, 1, headers)
        for i, c in enumerate(claims, start=2):
            row = [
                c.get("claim_id"), c.get("date_of_service"),
                c.get("patient_name"), c.get("provider_npi"),
                c.get("payer_name"),
                _to_decimal(c.get("billed_amount")),
                _to_decimal(c.get("allowed_amount")),
                _to_decimal(c.get("paid_amount")),
                _to_decimal(c.get("patient_responsibility")),
                c.get("claim_filing_indicator"),
                c.get("claim_status"),
            ]
            for j, v in enumerate(row, start=1):
                cell = ws.cell(row=i, column=j, value=v)
                cell.border = BORDER
                if j in (6, 7, 8, 9):
                    cell.number_format = FMT_MONEY
        _autosize(ws, min_width=10)
        ws.freeze_panes = "A2"

    # -----------------------------------------------------------------
    # EAPG breakdown
    # -----------------------------------------------------------------

    def _write_eapg_breakdown(self, ws: Worksheet, data: dict) -> None:
        headers = ["EAPG", "Count", "Expected", "Paid", "Variance", "Avg Compression %"]
        _header_row(ws, 1, headers)
        rows = data.get("rows", []) if isinstance(data, dict) else []
        for i, r in enumerate(rows, start=2):
            comp = _to_decimal(r.get("avg_compression_pct")) / Decimal("100") \
                    if r.get("avg_compression_pct") is not None else None
            out_row = [
                r.get("bucket"),
                r.get("n"),
                _to_decimal(r.get("expected")),
                _to_decimal(r.get("paid")),
                _to_decimal(r.get("variance")),
                comp,
            ]
            for j, v in enumerate(out_row, start=1):
                cell = ws.cell(row=i, column=j, value=v)
                cell.border = BORDER
                if j in (3, 4, 5):
                    cell.number_format = FMT_MONEY
                elif j == 6:
                    cell.number_format = FMT_PCT
        _autosize(ws, min_width=12)
        ws.freeze_panes = "A2"

    # -----------------------------------------------------------------
    # Denial analysis
    # -----------------------------------------------------------------

    def _write_denials(self, ws: Worksheet, data: dict) -> None:
        headers = ["Group", "Reason (CARC)", "Count", "Total Amount", "% of Total"]
        _header_row(ws, 1, headers)
        rows = data.get("rows", []) if isinstance(data, dict) else []
        for i, r in enumerate(rows, start=2):
            pct = _to_decimal(r.get("pct_of_adjustments")) / Decimal("100") \
                    if r.get("pct_of_adjustments") is not None else None
            out_row = [
                r.get("group_code"),
                r.get("reason_code"),
                r.get("count"),
                _to_decimal(r.get("total_amount")),
                pct,
            ]
            for j, v in enumerate(out_row, start=1):
                cell = ws.cell(row=i, column=j, value=v)
                cell.border = BORDER
                if j == 4:
                    cell.number_format = FMT_MONEY
                elif j == 5:
                    cell.number_format = FMT_PCT
        _autosize(ws, min_width=10)
        ws.freeze_panes = "A2"

    # -----------------------------------------------------------------
    # Rate reference
    # -----------------------------------------------------------------

    def _write_reference(
        self, ws: Worksheet, reference_codes: list[dict], base_rates: list[dict]
    ) -> None:
        # Section 1: HCPCS codes seen across the loaded claims
        ws.cell(row=1, column=1, value="HCPCS codes observed").font = BOLD
        _header_row(ws, 2, ["HCPCS", "EAPG", "EAPG Description", "EAPG Type", "Effective"])
        r = 3
        for code in reference_codes:
            for j, v in enumerate([
                code.get("hcpcs"),
                code.get("eapg"),
                code.get("eapg_desc"),
                code.get("eapg_type"),
                code.get("effective"),
            ], start=1):
                cell = ws.cell(row=r, column=j, value=v)
                cell.border = BORDER
            r += 1

        # Section 2: Base rates applied
        r += 2
        ws.cell(row=r, column=1, value="Base rates used").font = BOLD
        r += 1
        _header_row(ws, r, ["Source", "Peer Group", "Region", "Effective Date", "Rate"])
        r += 1
        for br in base_rates:
            for j, v in enumerate([
                br.get("source"),
                br.get("peer_group"),
                br.get("region"),
                br.get("effective_date"),
                _to_decimal(br.get("rate")),
            ], start=1):
                cell = ws.cell(row=r, column=j, value=v)
                cell.border = BORDER
                if j == 5:
                    cell.number_format = FMT_MONEY
            r += 1

        _autosize(ws, min_width=12)
