"""Professional PDF report using reportlab.

Sections (per spec):
  - Cover page with firm name, report title, date range, provider info
  - Executive summary with KPI grid
  - Rate compression (top EAPGs by variance)
  - Denial analysis (top CARC codes by dollar impact)
  - Per-claim APG breakdown (paginated; cap controlled by caller)

All pages carry a footer with page number, generation timestamp, and a
CONFIDENTIAL label. The layout uses Letter size (8.5 x 11in) with 0.75in
margins. Tables are styled with the same brand navy as the UI/Excel.
"""
from __future__ import annotations

import io
from datetime import datetime
from decimal import Decimal
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageBreak, PageTemplate, Paragraph,
    Spacer, Table, TableStyle,
)

# Brand palette (match UI / Excel exporter)
BRAND_NAVY = colors.HexColor("#1a2e4a")
PRIMARY_BLUE = colors.HexColor("#2563eb")
SLATE_50 = colors.HexColor("#f8fafc")
SLATE_200 = colors.HexColor("#e2e8f0")
SLATE_500 = colors.HexColor("#64748b")
DANGER = colors.HexColor("#dc2626")


_BASE_STYLES = getSampleStyleSheet()

_H1 = ParagraphStyle("H1", parent=_BASE_STYLES["Heading1"],
                    fontName="Helvetica-Bold", fontSize=20,
                    textColor=BRAND_NAVY, spaceAfter=6)
_H2 = ParagraphStyle("H2", parent=_BASE_STYLES["Heading2"],
                    fontName="Helvetica-Bold", fontSize=13,
                    textColor=BRAND_NAVY, spaceBefore=18, spaceAfter=6)
_H3 = ParagraphStyle("H3", parent=_BASE_STYLES["Heading3"],
                    fontName="Helvetica-Bold", fontSize=11,
                    textColor=BRAND_NAVY, spaceBefore=8, spaceAfter=4)
_BODY = ParagraphStyle("Body", parent=_BASE_STYLES["BodyText"],
                       fontName="Helvetica", fontSize=10, leading=14)
_MUTED = ParagraphStyle("Muted", parent=_BODY, textColor=SLATE_500, fontSize=9)
_CELL = ParagraphStyle("Cell", parent=_BODY, fontSize=9, leading=12)
_CELL_RIGHT = ParagraphStyle("CellRight", parent=_CELL, alignment=TA_RIGHT)


# ---------------------------------------------------------------------------
# Page frame with header/footer
# ---------------------------------------------------------------------------


def _on_page(canvas, doc, *, gen_at: str):
    """Draw header + footer on every page except the cover (we trigger this for
    pages 2+). Page 1 uses a simpler frame."""
    canvas.saveState()
    w, h = letter
    # Footer
    canvas.setFillColor(SLATE_500)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(0.75 * inch, 0.5 * inch, f"Generated {gen_at}")
    canvas.drawCentredString(w / 2, 0.5 * inch, "CONFIDENTIAL — IRC Minc")
    canvas.drawRightString(w - 0.75 * inch, 0.5 * inch, f"Page {doc.page}")
    # Hairline above footer
    canvas.setStrokeColor(SLATE_200)
    canvas.setLineWidth(0.4)
    canvas.line(0.75 * inch, 0.6 * inch, w - 0.75 * inch, 0.6 * inch)
    canvas.restoreState()


def _to_decimal(v) -> Decimal:
    if v is None or v == "":
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


def _money(v) -> str:
    d = _to_decimal(v)
    return f"${d:,.2f}"


def _pct(v) -> str:
    d = _to_decimal(v)
    return f"{d:.2f}%"


# ---------------------------------------------------------------------------
# PDF builder
# ---------------------------------------------------------------------------


class PDFExporter:
    """Builds a PDF report. Same payload shape as ExcelExporter."""

    def build(self, payload: dict, *, max_claims: int = 25) -> bytes:
        gen_at = payload.get("generated_at")
        if isinstance(gen_at, datetime):
            gen_at_str = gen_at.strftime("%Y-%m-%d %H:%M UTC")
        else:
            gen_at_str = str(gen_at or "—")

        buf = io.BytesIO()
        doc = BaseDocTemplate(
            buf, pagesize=letter,
            leftMargin=0.75 * inch, rightMargin=0.75 * inch,
            topMargin=0.75 * inch, bottomMargin=0.8 * inch,
            title="APG 835/837 Rate Analyzer Report",
            author="IRC Minc",
        )
        frame = Frame(doc.leftMargin, doc.bottomMargin,
                      doc.width, doc.height, id="main")
        doc.addPageTemplates([
            PageTemplate(
                id="content",
                frames=[frame],
                onPage=lambda canvas, doc: _on_page(canvas, doc, gen_at=gen_at_str),
            ),
        ])

        story = []
        story += self._cover(payload, gen_at_str)
        story.append(PageBreak())
        story += self._summary(payload)
        story.append(PageBreak())
        story += self._compression_section(payload)
        story += self._denial_section(payload)
        story.append(PageBreak())
        story += self._claim_detail(payload, max_claims=max_claims)

        doc.build(story)
        return buf.getvalue()

    # -----------------------------------------------------------------
    # Cover
    # -----------------------------------------------------------------

    def _cover(self, payload: dict, gen_at: str) -> list:
        provider = payload.get("provider") or {}
        filters = payload.get("filters") or {}

        parts = [
            Spacer(1, 1.5 * inch),
            Paragraph("APG 835/837 Rate Analyzer", _H1),
            Paragraph("Article 28 Reimbursement Report", _H2),
            Spacer(1, 0.8 * inch),
            Paragraph(f"<b>Provider:</b> {provider.get('provider_name') or '—'}", _BODY),
            Paragraph(f"<b>NPI:</b> {provider.get('npi') or '—'}", _BODY),
            Paragraph(
                f"<b>Peer group / Region:</b> {provider.get('peer_group') or '—'} · "
                f"{provider.get('region') or '—'}",
                _BODY,
            ),
        ]
        if filters.get("date_from") or filters.get("date_to"):
            parts.append(Paragraph(
                f"<b>Date range:</b> {filters.get('date_from', '—')} — {filters.get('date_to', '—')}",
                _BODY,
            ))
        if filters.get("payer_name"):
            parts.append(Paragraph(f"<b>Payer:</b> {filters['payer_name']}", _BODY))

        parts.append(Spacer(1, 1.5 * inch))
        parts.append(Paragraph(f"Generated {gen_at}", _MUTED))
        parts.append(Paragraph("CONFIDENTIAL — For internal use only.", _MUTED))
        return parts

    # -----------------------------------------------------------------
    # Exec summary
    # -----------------------------------------------------------------

    def _summary(self, payload: dict) -> list:
        summary = payload.get("summary") or {}
        rows = [
            ["Total claims",              str(summary.get("total_claims") or 0)],
            ["Total billed",              _money(summary.get("total_billed"))],
            ["Total paid",                _money(summary.get("total_paid"))],
            ["Paid % of billed",          _pct(summary.get("paid_as_pct_of_billed"))],
            ["Denied claims",             str(summary.get("total_denied") or 0)],
            ["Denial rate",               _pct(summary.get("denial_rate_pct"))],
            ["APG claims",                str(summary.get("apg_claims") or 0)],
            ["APG correct payment total", _money(summary.get("apg_correct_payment_total"))],
            ["APG actual paid total",     _money(summary.get("apg_actual_paid_total"))],
            ["APG variance total",        _money(summary.get("apg_total_variance"))],
            ["APG underpayment total",    _money(summary.get("apg_underpayment_total"))],
            ["APG avg compression",       _pct(summary.get("apg_avg_compression_pct"))],
        ]
        table = Table(rows, colWidths=[3.0 * inch, 2.0 * inch])
        table.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
            ("TEXTCOLOR", (0, 0), (0, -1), BRAND_NAVY),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("GRID", (0, 0), (-1, -1), 0.25, SLATE_200),
            ("BACKGROUND", (0, 0), (0, -1), SLATE_50),
        ]))
        return [
            Paragraph("Executive summary", _H2),
            table,
        ]

    # -----------------------------------------------------------------
    # Compression + denials
    # -----------------------------------------------------------------

    def _compression_section(self, payload: dict) -> list:
        data = payload.get("eapg_breakdown") or {}
        rows = data.get("rows", []) if isinstance(data, dict) else []
        if not rows:
            return [Paragraph("Rate compression — no Article 28 claims in this window.", _MUTED)]

        header = ["EAPG", "Count", "Expected", "Paid", "Variance", "Comp %"]
        body = []
        for r in rows[:15]:  # cap to keep report readable
            body.append([
                Paragraph(str(r.get("bucket") or "—"), _CELL),
                Paragraph(str(r.get("n") or 0), _CELL_RIGHT),
                Paragraph(_money(r.get("expected")), _CELL_RIGHT),
                Paragraph(_money(r.get("paid")), _CELL_RIGHT),
                Paragraph(_money(r.get("variance")), _CELL_RIGHT),
                Paragraph(_pct(r.get("avg_compression_pct")), _CELL_RIGHT),
            ])
        table = Table(
            [header] + body,
            colWidths=[2.4 * inch, 0.6 * inch, 0.9 * inch, 0.9 * inch, 0.9 * inch, 0.7 * inch],
            repeatRows=1,
        )
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
            ("GRID", (0, 0), (-1, -1), 0.25, SLATE_200),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SLATE_50]),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))

        return [
            Paragraph("Rate compression by EAPG", _H2),
            Paragraph("Ranked by absolute variance.", _MUTED),
            Spacer(1, 6),
            table,
        ]

    def _denial_section(self, payload: dict) -> list:
        data = payload.get("denials") or {}
        rows = data.get("rows", []) if isinstance(data, dict) else []
        if not rows:
            return [Spacer(1, 12),
                    Paragraph("Denial analysis — no adjustments captured in this window.", _MUTED)]

        header = ["Group", "CARC", "Count", "Amount", "% of total"]
        body = []
        for r in rows[:15]:
            body.append([
                Paragraph(r.get("group_code", "—"), _CELL),
                Paragraph(r.get("reason_code", "—"), _CELL),
                Paragraph(str(r.get("count", 0)), _CELL_RIGHT),
                Paragraph(_money(r.get("total_amount")), _CELL_RIGHT),
                Paragraph(_pct(r.get("pct_of_adjustments")), _CELL_RIGHT),
            ])
        table = Table(
            [header] + body,
            colWidths=[0.8 * inch, 0.9 * inch, 0.8 * inch, 1.3 * inch, 1.0 * inch],
            repeatRows=1,
        )
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
            ("GRID", (0, 0), (-1, -1), 0.25, SLATE_200),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SLATE_50]),
            ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))

        return [
            Spacer(1, 14),
            Paragraph("Denial analysis", _H2),
            Paragraph("Top CARC codes by dollar impact.", _MUTED),
            Spacer(1, 6),
            table,
        ]

    # -----------------------------------------------------------------
    # Per-claim detail
    # -----------------------------------------------------------------

    def _claim_detail(self, payload: dict, max_claims: int) -> list:
        claims = payload.get("claims_835i") or []
        if not claims:
            return [Paragraph("No 835I claims to detail in this window.", _MUTED)]

        story = [Paragraph("Claim detail (835I)", _H2)]
        rendered = 0
        for c in claims:
            if rendered >= max_claims:
                remaining = len(claims) - max_claims
                story.append(Spacer(1, 8))
                story.append(Paragraph(
                    f"… plus {remaining} additional claims — see the Excel report for the full list.",
                    _MUTED,
                ))
                break
            story.extend(self._one_claim(c))
            rendered += 1
        return story

    def _one_claim(self, c: dict) -> list:
        apg = c.get("apg_result") or {}
        header_txt = (
            f"<b>{c.get('claim_id') or '—'}</b> · DOS {c.get('date_of_service') or '—'} · "
            f"{c.get('payer_name') or '—'}"
        )
        pieces = [Spacer(1, 10), Paragraph(header_txt, _H3)]

        hdr_row = [
            ["Billed", _money(c.get("billed_amount"))],
            ["Paid", _money(c.get("paid_amount"))],
            ["Correct APG", _money(apg.get("correct_apg_payment"))],
            ["Variance", _money(apg.get("variance"))],
            ["Compression %", _pct(apg.get("compression_pct"))],
        ]
        kpi_table = Table(hdr_row, colWidths=[1.3 * inch, 1.2 * inch])
        kpi_table.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("TEXTCOLOR", (0, 0), (0, -1), SLATE_500),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        pieces.append(kpi_table)

        if apg.get("line_details"):
            line_hdr = ["Proc", "EAPG", "Type", "Weight", "Expected", "Paid", "Variance"]
            body = []
            for ld in apg["line_details"][:20]:
                body.append([
                    Paragraph(str(ld.get("procedure_code") or "—"), _CELL),
                    Paragraph(str(ld.get("eapg") or "—"), _CELL),
                    Paragraph(str(ld.get("eapg_type") or "—"), _CELL),
                    Paragraph(
                        f"{float(ld['weight']):.4f}" if ld.get("weight") is not None else "—",
                        _CELL_RIGHT,
                    ),
                    Paragraph(_money(ld.get("expected_payment")), _CELL_RIGHT),
                    Paragraph(_money(ld.get("actual_paid")), _CELL_RIGHT),
                    Paragraph(_money(ld.get("variance")), _CELL_RIGHT),
                ])
            table = Table(
                [line_hdr] + body,
                colWidths=[0.8*inch, 0.7*inch, 1.2*inch, 0.7*inch, 1.0*inch, 0.9*inch, 1.0*inch],
                repeatRows=1,
            )
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), SLATE_50),
                ("TEXTCOLOR", (0, 0), (-1, 0), BRAND_NAVY),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
                ("GRID", (0, 0), (-1, -1), 0.25, SLATE_200),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]))
            pieces.append(Spacer(1, 4))
            pieces.append(table)

        return pieces
