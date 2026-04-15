"""Shared EDI X12 primitives used by 835I, 835P, and 837 parsers.

This module owns the layer *below* transaction-set semantics — the things that
are true of every 837/835 file regardless of business meaning:

  * The ISA envelope and its auto-detected delimiters (element separator,
    sub-element separator, segment terminator)
  * Splitting the stream into Segment objects
  * Primitive element decoders for dates, money, and integers
  * Generic CAS triplet expansion
  * NM1 name assembly

Transaction-specific parsers (edi_835i, edi_835p, edi_837) build on top of this.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Segment
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    """One X12 segment: a tag (e.g. "CLP") and ordered element values."""
    tag: str
    elements: list[str]

    def get(self, idx: int, default: str = "") -> str:
        """1-based element accessor matching X12 naming (ISA01, CLP01, ...)."""
        if idx <= 0:
            raise ValueError("X12 element positions are 1-based")
        if idx - 1 < len(self.elements):
            return self.elements[idx - 1] or default
        return default

    def composite(self, idx: int, sub: int, default: str = "") -> str:
        """Sub-element (composite) accessor: 1-based `idx`.`sub`.
        X12 composites are ':' separated within a single element."""
        raw = self.get(idx, "")
        if not raw:
            return default
        parts = raw.split(":")
        if sub - 1 < len(parts):
            return parts[sub - 1] or default
        return default


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------


class EDILexer:
    """Tokenize a raw X12 document into Segment objects.

    Auto-detects delimiters from the ISA envelope:
      - ISA[3]  = element separator (col 4)
      - ISA[104] = sub-element separator (col 105)
      - ISA[105] = segment terminator (col 106)

    Accepts a leading BOM / whitespace before ISA. Strips intra-segment
    newlines (senders sometimes prettify).
    """

    def __init__(self, text: str):
        stripped = text.lstrip("\ufeff \t\r\n")
        if not stripped.startswith("ISA"):
            raise ValueError("Input does not start with an ISA segment (not a valid X12 interchange)")
        text = stripped

        if len(text) < 106:
            raise ValueError("Input too short to contain an ISA header")
        self.element_sep = text[3]
        self.subelement_sep = text[104]
        self.segment_sep = text[105]

        normalized = text.replace("\r", "").replace("\n", "")
        raw_segments = normalized.split(self.segment_sep)
        self.segments: list[Segment] = []
        for raw in raw_segments:
            if not raw.strip():
                continue
            parts = raw.split(self.element_sep)
            tag = parts[0].strip()
            elements = list(parts[1:])
            if tag:
                self.segments.append(Segment(tag=tag, elements=elements))

    def __iter__(self) -> Iterable[Segment]:
        return iter(self.segments)


# ---------------------------------------------------------------------------
# Element decoders
# ---------------------------------------------------------------------------


def parse_date(raw: str) -> Optional[date]:
    """Parse CCYYMMDD, CCYYMMDD-CCYYMMDD (returns start), or YYMMDD."""
    if not raw:
        return None
    raw = raw.strip()
    if "-" in raw:
        raw = raw.split("-", 1)[0].strip()
    for fmt in ("%Y%m%d", "%y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# X12 DTP segments carry a format qualifier in DTP02 that describes DTP03.
# Common values: D8=single date CCYYMMDD, RD8=range CCYYMMDD-CCYYMMDD, TM=time only.
def parse_dtp_date(seg: Segment) -> Optional[date]:
    """Decode a DTP segment (837 date) into a date. Uses DTP02 format qualifier."""
    fmt = seg.get(2).upper()
    raw = seg.get(3)
    if not raw:
        return None
    if fmt in ("D8", ""):
        return parse_date(raw)
    if fmt == "RD8":
        # Range — take the start date
        return parse_date(raw.split("-", 1)[0])
    if fmt == "DT":
        # Datetime CCYYMMDDHHMM — take just the date
        return parse_date(raw[:8])
    return parse_date(raw)


def parse_money(raw: str) -> Decimal:
    """Parse a monetary value to Decimal. Empty/invalid → 0."""
    if raw is None or raw == "":
        return Decimal("0")
    raw = raw.strip()
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except Exception:
        return Decimal("0")


def parse_int(raw: str, default: int = 0) -> int:
    if not raw:
        return default
    try:
        return int(Decimal(raw))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Segment-type helpers
# ---------------------------------------------------------------------------


def expand_cas(seg: Segment) -> list[tuple[str, str, Decimal, Optional[int]]]:
    """A CAS segment may carry up to 6 (reason, amount, quantity) triplets
    sharing one group code at CAS01. Returns list of (group, reason, amount, qty)."""
    group = seg.get(1)
    out: list[tuple[str, str, Decimal, Optional[int]]] = []
    for start in range(2, 20, 3):
        reason = seg.get(start)
        if not reason:
            continue
        amt = parse_money(seg.get(start + 1))
        qty_raw = seg.get(start + 2)
        qty = parse_int(qty_raw) if qty_raw else None
        out.append((group, reason, amt, qty))
    return out


def nm1_name(seg: Segment) -> str:
    """Assemble NM1 display name: 'First Middle Last' for persons, org name otherwise."""
    last_or_org = seg.get(3)
    first = seg.get(4)
    middle = seg.get(5)
    entity_type = seg.get(2)  # 1 = person, 2 = org
    if entity_type == "1":
        parts = [p for p in (first, middle, last_or_org) if p]
        return " ".join(parts)
    return last_or_org


def nm1_id(seg: Segment) -> Optional[str]:
    """NM1 identification code (NM109 — NPI, MI, etc.) if present."""
    v = seg.get(9)
    return v or None


# ---------------------------------------------------------------------------
# Composite decoders
# ---------------------------------------------------------------------------


def decode_hcpcs_composite(seg: Segment, element_idx: int) -> tuple[str, str, list[str], Optional[str]]:
    """Decode an X12 composite that looks like 'HC:99213:25:59' into
    (qualifier, procedure_code, modifiers, revenue_code_or_none).

    Qualifiers:
      HC = HCPCS/CPT
      NU = National Uniform Billing Committee (UB-04 revenue code)
      ZZ = Mutually defined (often HCPCS fallback)
      ER = Emergency Revenue
      N4 = National Drug Code
    """
    qual = seg.composite(element_idx, 1)
    primary = seg.composite(element_idx, 2)
    mods = [m for m in (
        seg.composite(element_idx, 3),
        seg.composite(element_idx, 4),
        seg.composite(element_idx, 5),
        seg.composite(element_idx, 6),
    ) if m]
    rev_code = None
    if qual in ("NU", "ZZ") and not primary:
        rev_code = seg.composite(element_idx, 2)
    return qual, primary, mods, rev_code


# ---------------------------------------------------------------------------
# Input normalization
# ---------------------------------------------------------------------------


def load_edi_text(text_or_path) -> str:
    """Accept either a filesystem path or a raw EDI string. Return the raw text."""
    from pathlib import Path
    if isinstance(text_or_path, Path):
        return text_or_path.read_text(encoding="utf-8", errors="replace")
    if isinstance(text_or_path, str):
        # Heuristic: if it starts with ISA or whitespace+ISA, treat as raw text.
        # Otherwise, and if short, try to interpret as a file path.
        stripped = text_or_path.lstrip("\ufeff \t\r\n")
        if stripped.startswith("ISA"):
            return text_or_path
        if len(text_or_path) < 260:
            p = Path(text_or_path)
            if p.exists() and p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")
        return text_or_path
    raise TypeError(f"Expected path or raw EDI text, got {type(text_or_path).__name__}")
