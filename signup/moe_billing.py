from __future__ import annotations

import io
import zipfile
from decimal import Decimal, InvalidOperation
from html import escape as html_escape
from typing import Any
from xml.sax.saxutils import escape as xml_escape


ACTIVE_INVOICE_STATUSES = {"ISSUED", "DISPUTED", "PAID"}


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _upper(value: Any) -> str:
    return _clean(value).upper()


def _d(value: Any, default: str = "0") -> Decimal:
    raw = _clean(value).replace(",", "") or default
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _truthy(value: Any) -> bool:
    return _upper(value) in {"TRUE", "1", "YES", "Y"}


def active_invoice_entries(entries: list[dict[str, Any]], order_id: str) -> list[dict[str, Any]]:
    """Return entries billable at invoice issue time.

    Schools may cancel/withdraw entries before invoicing.  Those rows remain in
    the transaction ledger for audit but are excluded from the invoice snapshot.
    """
    target = _clean(order_id)
    out: list[dict[str, Any]] = []
    for entry in entries:
        if _clean(entry.get("ORDER_ID")) != target:
            continue
        if _upper(entry.get("STATUS")) in {"WITHDRAWN", "CANCELLED"}:
            continue
        if _truthy(entry.get("IS_DELETED")):
            continue
        out.append(entry)
    out.sort(
        key=lambda row: (
            _clean(row.get("ATHLETE_NAME")).casefold(),
            _clean(row.get("EVENT_NAME")).casefold(),
            _clean(row.get("ENTRY_ID")),
        )
    )
    return out


def build_invoice_line_rows(
    *,
    invoice_id: str,
    invoice_number: str,
    order: dict[str, Any],
    entries: list[dict[str, Any]],
    organization_name: str,
    team_code: str,
    created_at: str,
    created_by_user_id: str,
    created_by_email: str,
    currency: str = "SGD",
) -> list[dict[str, Any]]:
    """Build one immutable MOE_INVOICES row per athlete-event line."""
    rows: list[dict[str, Any]] = []
    for seq, entry in enumerate(entries, start=1):
        entry_id = _clean(entry.get("ENTRY_ID"))
        rows.append(
            {
                "INVOICE_LINE_ID": f"{invoice_id}-L{seq:04d}",
                "INVOICE_ID": invoice_id,
                "INVOICE_NUMBER": invoice_number,
                "ORDER_ID": _clean(order.get("ORDER_ID")),
                "COMPETITION_ID": _clean(order.get("COMPETITION_ID")),
                "ORGANIZATION_ID": _clean(order.get("ORGANIZATION_ID")),
                "ORGANIZATION_NAME": organization_name,
                "TEAM_CODE": team_code,
                "ENTRY_ID": entry_id,
                "REGISTRATION_ID": _clean(entry.get("REGISTRATION_ID")),
                "ATHLETE_NAME": _clean(entry.get("ATHLETE_NAME")),
                "DOB": _clean(entry.get("DOB")),
                "GENDER": _clean(entry.get("GENDER")),
                "NATIONALITY": _clean(entry.get("NATIONALITY")),
                "DIVISION": _clean(entry.get("DIVISION")),
                "EVENT_NAME": _clean(entry.get("EVENT_NAME")),
                "EVENT_CODE": _clean(entry.get("EVENT_CODE")),
                "REGISTRATION_PERIOD": _clean(entry.get("REGISTRATION_PERIOD")),
                "LINE_AMOUNT": f"{_d(entry.get('ENTRY_FEE')):.2f}",
                "CURRENCY": currency,
                "STATUS": "ISSUED",
                "CREATED_AT": created_at,
                "CREATED_BY_USER_ID": created_by_user_id,
                "CREATED_BY_EMAIL": created_by_email,
                "ISSUED_AT": created_at,
                "ISSUED_BY_USER_ID": created_by_user_id,
                "PAID_AT": "",
                "PAID_BY_USER_ID": "",
                "PAYMENT_REFERENCE": "",
                "DISPUTE_REASON": "",
                "UPDATED_AT": created_at,
            }
        )
    return rows


def invoice_total(lines: list[dict[str, Any]]) -> Decimal:
    return sum((_d(row.get("LINE_AMOUNT")) for row in lines), Decimal("0"))


def invoice_summary(lines: list[dict[str, Any]]) -> dict[str, Any]:
    if not lines:
        return {}
    first = lines[0]
    statuses = {_upper(row.get("STATUS")) for row in lines if _clean(row.get("STATUS"))}
    status = next(iter(statuses)) if len(statuses) == 1 else "MIXED"
    return {
        "INVOICE_ID": _clean(first.get("INVOICE_ID")),
        "INVOICE_NUMBER": _clean(first.get("INVOICE_NUMBER")),
        "ORDER_ID": _clean(first.get("ORDER_ID")),
        "COMPETITION_ID": _clean(first.get("COMPETITION_ID")),
        "ORGANIZATION_ID": _clean(first.get("ORGANIZATION_ID")),
        "ORGANIZATION_NAME": _clean(first.get("ORGANIZATION_NAME")),
        "TEAM_CODE": _clean(first.get("TEAM_CODE")),
        "CURRENCY": _clean(first.get("CURRENCY")) or "SGD",
        "STATUS": status,
        "ENTRY_COUNT": len(lines),
        "AMOUNT": invoice_total(lines),
        "ISSUED_AT": _clean(first.get("ISSUED_AT")),
        "PAID_AT": _clean(first.get("PAID_AT")),
        "PAYMENT_REFERENCE": _clean(first.get("PAYMENT_REFERENCE")),
        "DISPUTE_REASON": _clean(first.get("DISPUTE_REASON")),
    }


def group_invoice_lines(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        invoice_id = _clean(row.get("INVOICE_ID"))
        if invoice_id:
            grouped.setdefault(invoice_id, []).append(row)
    for invoice_id in grouped:
        grouped[invoice_id].sort(key=lambda r: _clean(r.get("INVOICE_LINE_ID")))
    return grouped


def active_invoice_lines(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if _upper(row.get("STATUS")) in ACTIVE_INVOICE_STATUSES]


# ---------------------------------------------------------------------------
# XLSX export - deliberately dependency-free so the existing Streamlit image
# does not need another package solely for invoice export.
# ---------------------------------------------------------------------------


def _xlsx_col_name(index: int) -> str:
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _xlsx_cell(ref: str, value: Any, style: int = 0) -> str:
    if isinstance(value, (int, float, Decimal)):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = xml_escape(_clean(value))
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{text}</t></is></c>'


def invoice_xlsx_bytes(
    *,
    invoice_lines: list[dict[str, Any]],
    competition_name: str,
) -> bytes:
    summary = invoice_summary(invoice_lines)
    if not summary:
        raise ValueError("Invoice contains no lines.")

    table_headers = [
        "Athlete",
        "DOB",
        "Gender",
        "Event",
        "Division",
        "Registration Period",
        "Entry ID",
        "Amount (SGD)",
    ]

    rows: list[list[tuple[Any, int]]] = [
        [("Singapore Athletics - MOE Post-Event Billing", 1)],
        [("Invoice Number", 2), (summary["INVOICE_NUMBER"], 0)],
        [("Order ID", 2), (summary["ORDER_ID"], 0)],
        [("Competition", 2), (competition_name, 0)],
        [("Organisation", 2), (summary["ORGANIZATION_NAME"], 0)],
        [("Team Code", 2), (summary["TEAM_CODE"], 0)],
        [("Issued At", 2), (summary["ISSUED_AT"], 0)],
        [("Invoice Total (SGD)", 2), (summary["AMOUNT"], 3)],
        [],
        [(header, 2) for header in table_headers],
    ]

    for line in invoice_lines:
        rows.append(
            [
                (_clean(line.get("ATHLETE_NAME")), 0),
                (_clean(line.get("DOB")), 0),
                (_clean(line.get("GENDER")), 0),
                (_clean(line.get("EVENT_NAME")), 0),
                (_clean(line.get("DIVISION")), 0),
                (_clean(line.get("REGISTRATION_PERIOD")), 0),
                (_clean(line.get("ENTRY_ID")), 0),
                (_d(line.get("LINE_AMOUNT")), 3),
            ]
        )
    rows.append([])
    rows.append([("TOTAL", 2)] + [("", 0)] * 6 + [(summary["AMOUNT"], 3)])

    row_xml: list[str] = []
    for row_idx, cells in enumerate(rows, start=1):
        cell_xml = []
        for col_idx, (value, style) in enumerate(cells, start=1):
            if value == "" and style == 0:
                continue
            ref = f"{_xlsx_col_name(col_idx)}{row_idx}"
            cell_xml.append(_xlsx_cell(ref, value, style))
        row_xml.append(f'<row r="{row_idx}">{"".join(cell_xml)}</row>')

    worksheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <cols>
    <col min="1" max="1" width="24" customWidth="1"/>
    <col min="2" max="3" width="14" customWidth="1"/>
    <col min="4" max="4" width="24" customWidth="1"/>
    <col min="5" max="6" width="18" customWidth="1"/>
    <col min="7" max="7" width="22" customWidth="1"/>
    <col min="8" max="8" width="15" customWidth="1"/>
  </cols>
  <sheetData>{''.join(row_xml)}</sheetData>
</worksheet>'''

    workbook_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Invoice" sheetId="1" r:id="rId1"/></sheets>
</workbook>'''

    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="3">
    <font><sz val="11"/><name val="Calibri"/></font>
    <font><b/><sz val="16"/><name val="Calibri"/></font>
    <font><b/><sz val="11"/><name val="Calibri"/></font>
  </fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <numFmts count="1"><numFmt numFmtId="164" formatCode="0.00"/></numFmts>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
    <xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
  </cellXfs>
</styleSheet>'''

    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>'''

    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/worksheets/sheet1.xml", worksheet_xml)
    return output.getvalue()


# ---------------------------------------------------------------------------
# PDF export - small self-contained PDF writer using the standard PDF base-14
# Helvetica fonts. This keeps the Streamlit deployment dependency-free.
# ---------------------------------------------------------------------------


def _pdf_escape(text: Any) -> str:
    return (
        _clean(text)
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _pdf_text(x: float, y: float, text: Any, size: int = 9, bold: bool = False) -> str:
    font = "/F2" if bold else "/F1"
    return f"BT {font} {size} Tf {x:.1f} {y:.1f} Td ({_pdf_escape(text)}) Tj ET"


def invoice_pdf_bytes(
    *,
    invoice_lines: list[dict[str, Any]],
    competition_name: str,
) -> bytes:
    summary = invoice_summary(invoice_lines)
    if not summary:
        raise ValueError("Invoice contains no lines.")

    page_width = 595.0
    page_height = 842.0
    left = 40.0
    right = 555.0
    top = 800.0
    bottom = 55.0
    line_h = 16.0

    # Compact table suitable for A4 portrait.
    cols = [
        ("Athlete", left, 145),
        ("Event", left + 150, 115),
        ("Division", left + 270, 55),
        ("Period", left + 330, 80),
        ("SGD", left + 430, 70),
    ]

    def clipped(text: Any, max_chars: int) -> str:
        value = _clean(text)
        return value if len(value) <= max_chars else value[: max_chars - 1] + "~"

    pages: list[list[str]] = []
    commands: list[str] = []

    def page_header(first_page: bool) -> float:
        nonlocal commands
        y = top
        commands.append(_pdf_text(left, y, "Singapore Athletics", 16, True)); y -= 24
        commands.append(_pdf_text(left, y, "MOE Post-Event Billing", 13, True)); y -= 26
        if first_page:
            detail_lines = [
                ("Invoice", summary["INVOICE_NUMBER"]),
                ("Order", summary["ORDER_ID"]),
                ("Competition", competition_name),
                ("Organisation", summary["ORGANIZATION_NAME"]),
                ("Issued", summary["ISSUED_AT"]),
                ("Total", f"SGD {summary['AMOUNT']:.2f}"),
            ]
            for label, value in detail_lines:
                commands.append(_pdf_text(left, y, f"{label}: {clipped(value, 82)}", 9, label == "Total"))
                y -= line_h
            y -= 6
        else:
            commands.append(_pdf_text(left, y, f"Invoice {summary['INVOICE_NUMBER']} - continued", 9)); y -= 22
        commands.append(f"{left:.1f} {y:.1f} m {right:.1f} {y:.1f} l S")
        y -= 15
        for label, x, _ in cols:
            commands.append(_pdf_text(x, y, label, 8, True))
        y -= 8
        commands.append(f"{left:.1f} {y:.1f} m {right:.1f} {y:.1f} l S")
        y -= 15
        return y

    y = page_header(True)
    for idx, line in enumerate(invoice_lines, start=1):
        if y < bottom + 35:
            pages.append(commands)
            commands = []
            y = page_header(False)
        commands.append(_pdf_text(cols[0][1], y, clipped(line.get("ATHLETE_NAME"), 26), 8))
        commands.append(_pdf_text(cols[1][1], y, clipped(line.get("EVENT_NAME"), 20), 8))
        commands.append(_pdf_text(cols[2][1], y, clipped(line.get("DIVISION"), 8), 8))
        commands.append(_pdf_text(cols[3][1], y, clipped(line.get("REGISTRATION_PERIOD"), 12), 8))
        commands.append(_pdf_text(cols[4][1], y, f"{_d(line.get('LINE_AMOUNT')):.2f}", 8))
        y -= line_h

    if y < bottom + 45:
        pages.append(commands)
        commands = []
        y = page_header(False)
    y -= 4
    commands.append(f"{left:.1f} {y:.1f} m {right:.1f} {y:.1f} l S")
    y -= 18
    commands.append(_pdf_text(left + 360, y, "TOTAL", 10, True))
    commands.append(_pdf_text(left + 430, y, f"SGD {summary['AMOUNT']:.2f}", 10, True))
    y -= 28
    commands.append(_pdf_text(left, y, "Prepared for SA Finance / vendor@gov processing.", 8))
    pages.append(commands)

    # Object layout: catalog(1), pages(2), font1(3), font2(4), then page/content pairs.
    objects: list[bytes] = [b""]  # 1-indexed convenience
    page_object_ids: list[int] = []
    content_object_ids: list[int] = []
    next_id = 5
    for _ in pages:
        page_object_ids.append(next_id)
        content_object_ids.append(next_id + 1)
        next_id += 2

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_object_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    for commands_page, page_id, content_id in zip(pages, page_object_ids, content_object_ids):
        page_obj = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width:.0f} {page_height:.0f}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        stream = ("0.75 w\n" + "\n".join(commands_page)).encode("latin-1", "replace")
        content_obj = b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream"
        objects.append(page_obj)
        objects.append(content_obj)

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for obj_id in range(1, len(objects)):
        offsets.append(out.tell())
        out.write(f"{obj_id} 0 obj\n".encode("ascii"))
        out.write(objects[obj_id])
        out.write(b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects)}\n".encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.write(
        (
            f"trailer\n<< /Size {len(objects)} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return out.getvalue()


def invoice_email_body(*, summary: dict[str, Any], competition_name: str, paid: bool = False) -> str:
    status_text = "has been recorded as paid" if paid else "has been issued"
    return (
        "Dear School Representative,\n\n"
        f"Singapore Athletics MOE post-event invoice {summary.get('INVOICE_NUMBER', '')} {status_text}.\n\n"
        f"Competition: {competition_name}\n"
        f"Order: {summary.get('ORDER_ID', '')}\n"
        f"Entries: {summary.get('ENTRY_COUNT', 0)}\n"
        f"Amount: SGD {summary.get('AMOUNT', Decimal('0')):.2f}\n"
        f"Status: {summary.get('STATUS', '')}\n\n"
        "Please contact Singapore Athletics if any detail requires clarification.\n\n"
        "SAA"
    )
