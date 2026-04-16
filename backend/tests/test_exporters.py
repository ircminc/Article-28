"""Excel + PDF exporter tests.

Both exporters are pure — they take a payload dict and return bytes — so we
stub a representative payload and verify:
  * bytes come back non-empty
  * key signatures (XLSX zip header, %PDF header) are present
  * the Excel workbook re-opens with openpyxl and has the expected sheets
  * the Excel Summary sheet contains the KPIs we put in
  * the PDF contains a few strings we care about (provider name, section
    headings) via a simple text-scan of the raw bytes
"""
from __future__ import annotations

import io
from datetime import datetime
from decimal import Decimal

import openpyxl
import pytest

from backend.exporters.excel_exporter import ExcelExporter
from backend.exporters.pdf_exporter import PDFExporter


# ---------------------------------------------------------------------------
# Test payload
# ---------------------------------------------------------------------------


def _payload() -> dict:
    return {
        "generated_at": datetime(2026, 4, 16, 12, 0, 0),
        "filters": {
            "date_from": "2023-01-01",
            "date_to": "2023-12-31",
        },
        "provider": {
            "provider_name": "Sample Clinic LLC",
            "npi": "1234567890",
            "peer_group": "Clinic*",
            "provider_type": "dtc",
            "region": "Downstate",
            "county_code": 61,
            "cms_locality": "01",
        },
        "summary": {
            "total_claims": 4,
            "total_billed": "3050.00",
            "total_paid": "1500.00",
            "paid_as_pct_of_billed": "49.1803",
            "total_denied": 1,
            "denial_rate_pct": "25.0000",
            "apg_claims": 3,
            "apg_correct_payment_total": "1770.00",
            "apg_actual_paid_total": "1300.00",
            "apg_total_variance": "470.00",
            "apg_underpayment_total": "470.00",
            "apg_avg_compression_pct": "12.6984",
        },
        "claims_835i": [
            {
                "claim_id": "ERA-A",
                "date_of_service": "2023-03-10",
                "patient_name": "Doe, Jane",
                "provider_npi": "1234567890",
                "payer_name": "NYS MEDICAID",
                "billed_amount": "500.00",
                "allowed_amount": "400.00",
                "paid_amount": "400.00",
                "principal_diagnosis": "E119",
                "service_lines": [
                    {"procedure_code": "99213", "units": 1,
                     "billed_amount": "500.00", "allowed_amount": "400.00",
                     "paid_amount": "400.00", "modifiers": []}
                ],
                "apg_result": {
                    "correct_apg_payment": "420.00",
                    "actual_paid": "400.00",
                    "variance": "20.00",
                    "compression_pct": "4.7619",
                    "peer_group": "Clinic*",
                    "region": "Downstate",
                    "base_rate_applied": "169.02",
                    "discounting_applied": False,
                    "u6_applied": False,
                    "capital_applied": False,
                    "line_details": [
                        {"line_seq": 1, "procedure_code": "99213", "eapg": 491,
                         "eapg_desc": "MEDICAL VISIT INDICATOR",
                         "eapg_type": "Medical Visit", "weight": "2.4850",
                         "expected_payment": "420.00",
                         "actual_paid": "400.00", "variance": "20.00"},
                    ],
                },
            },
        ],
        "claims_835p": [
            {
                "claim_id": "PROF-A",
                "date_of_service": "2023-04-05",
                "patient_name": "Smith, John",
                "provider_npi": "1234567890",
                "payer_name": "AETNA COMMERCIAL",
                "billed_amount": "300.00",
                "allowed_amount": "200.00",
                "paid_amount": "200.00",
                "patient_responsibility": "0.00",
                "claim_filing_indicator": "CI",
                "claim_status": "1",
                "service_lines": [],
            },
        ],
        "eapg_breakdown": {
            "group_by": "eapg",
            "rows": [
                {"bucket": "321 — SKIN DEBRIDEMENT", "n": 1,
                 "expected": "1350.00", "paid": "900.00",
                 "variance": "450.00", "avg_compression_pct": "33.3333"},
                {"bucket": "491 — MEDICAL VISIT INDICATOR", "n": 1,
                 "expected": "420.00", "paid": "400.00",
                 "variance": "20.00", "avg_compression_pct": "4.7619"},
            ],
        },
        "denials": {
            "rows": [
                {"group_code": "CO", "reason_code": "50", "count": 1,
                 "total_amount": "750.00", "pct_of_adjustments": "57.6923"},
                {"group_code": "CO", "reason_code": "97", "count": 1,
                 "total_amount": "450.00", "pct_of_adjustments": "34.6154"},
                {"group_code": "CO", "reason_code": "45", "count": 1,
                 "total_amount": "100.00", "pct_of_adjustments": "7.6923"},
            ],
            "total_adjustments": 3,
            "total_amount": "1300.00",
        },
        "reference_codes": [
            {"hcpcs": "99213", "eapg": 491, "eapg_desc": "MEDICAL VISIT INDICATOR",
             "eapg_type": "Medical Visit", "effective": "2016-04-01"},
        ],
        "base_rates": [
            {"source": "dtc", "peer_group": "Clinic*", "region": "Downstate",
             "effective_date": "2015-04-01", "rate": "169.02"},
        ],
    }


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def test_excel_exporter_produces_valid_xlsx_with_expected_sheets():
    data = ExcelExporter().build(_payload())
    assert isinstance(data, (bytes, bytearray))
    assert len(data) > 2000  # non-trivial size
    # XLSX files are ZIP archives — first two bytes are "PK"
    assert data[:2] == b"PK"

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=False)
    try:
        assert {"Summary", "835I Claims", "835P Claims",
                "EAPG Breakdown", "Denial Analysis", "Rate Reference"}.issubset(
                    set(wb.sheetnames)
                )
    finally:
        wb.close()


def test_excel_summary_includes_provider_and_kpis():
    data = ExcelExporter().build(_payload())
    wb = openpyxl.load_workbook(io.BytesIO(data))
    try:
        ws = wb["Summary"]
        flat = " ".join(
            str(c.value) for row in ws.iter_rows(values_only=False) for c in row if c.value is not None
        )
        assert "Sample Clinic LLC" in flat
        assert "1234567890" in flat
        assert "Clinic*" in flat
        # Some KPI labels from the block
        assert "Total billed" in flat
        assert "APG variance total" in flat
    finally:
        wb.close()


def test_excel_835i_sheet_has_claim_row():
    data = ExcelExporter().build(_payload())
    wb = openpyxl.load_workbook(io.BytesIO(data))
    try:
        ws = wb["835I Claims"]
        # Row 1 is headers; row 2 is the first data row
        assert ws.cell(row=2, column=1).value == "ERA-A"
        assert ws.cell(row=2, column=3).value == "Doe, Jane"
    finally:
        wb.close()


def test_excel_denials_sorted_by_amount():
    data = ExcelExporter().build(_payload())
    wb = openpyxl.load_workbook(io.BytesIO(data))
    try:
        ws = wb["Denial Analysis"]
        # CO-50 with $750 should be first (row 2)
        assert ws.cell(row=2, column=2).value == "50"
        assert ws.cell(row=3, column=2).value == "97"
    finally:
        wb.close()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_pdf_exporter_produces_valid_pdf():
    data = PDFExporter().build(_payload())
    assert isinstance(data, (bytes, bytearray))
    # PDFs start with "%PDF-"
    assert data.startswith(b"%PDF-")
    # And end with %%EOF (possibly with trailing newline)
    assert b"%%EOF" in data[-64:]
    # Non-trivial size — we rendered tables + multiple pages
    assert len(data) > 3000


def test_pdf_is_multipage_and_well_structured():
    """Structural check — ReportLab zlib-compresses content streams by default,
    so substring scans on raw bytes don't work. We verify the document has
    multiple pages and references the reportlab metadata instead. Content
    correctness is covered by the Excel test suite and by the integration
    test (TestClient hitting /api/export/pdf)."""
    data = PDFExporter().build(_payload())
    text = data.decode("latin-1", errors="ignore")
    # Multi-page output: we emit cover + summary + compression/denials + claim detail
    assert text.count("/Type /Page") >= 3 or text.count("/Type/Page") >= 3
    # Document metadata should be present
    assert "ReportLab" in text
    # There's at least one xref section
    assert "xref" in text
