"""
VAT return exports: the history CSV (UTF-8 with BOM, common/csv_export.py),
the history PDF (one summary table) and a PDF for one period (cards, return
boxes, filing, payments). All built from history.rows() / the period's
detail, the same figures the page shows. Every export is audited.

PDFs are English only for now: reportlab's built-in fonts have no Arabic
glyphs, so the restaurant's English name is used and Arabic text typed by
a person (a reason, a reference) would not render. Arabic PDFs need the
font and shaping libraries planned for M10 (docs/vat-dashboard-plan.md §11).
"""

from io import BytesIO
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from common.csv_export import build_csv, money
from zatca.environments import profile

from . import boxes as return_boxes
from .history import status_text

_INK = colors.HexColor("#1f2933")
_MUTED = colors.HexColor("#6b7280")
_RULE = colors.HexColor("#d1d5db")
_HEAD_BG = colors.HexColor("#f3f4f6")
_WARN = colors.HexColor("#b45309")

BOX_NAMES = {key: name for _number, key, name, _tracked in return_boxes.BOXES}

HISTORY_COLUMNS = ["Period", "From", "To", "Frequency", "Due date", "Sales VAT", "Purchase VAT", "Credit b/f",
                   "VAT due (ours)", "ZATCA amount", "Paid", "Balance", "Filed on", "SADAD reference(s)", "Status"]


def _date(value):
    return f"{value:%Y-%m-%d}" if value else ""


def _restaurant(branch, settings):
    return settings.seller_name_en or branch.name_en or settings.seller_name_ar or branch.name_ar or ""


def _generated(settings):
    return timezone.now().astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M (Saudi time)")


def header(branch, settings, report):
    restaurant = " / ".join(name for name in (settings.seller_name_en or branch.name_en,
                                              settings.seller_name_ar or branch.name_ar) if name)
    rows = [("Restaurant", restaurant), ("VAT number", settings.vat_number), ("Report", report),
            ("Generated", _generated(settings))]
    if profile(settings.environment).is_test:
        rows.append(("Environment", f"{settings.environment} (TEST: practice periods, not real returns)"))
    return rows


# -- CSV -------------------------------------------------------------------------

def history_csv(branch, settings, rows):
    """build_csv() text for the history table, oldest period first."""
    body = [[
        row["name"], _date(row["start_date"]), _date(row["end_date"]), row["frequency"].capitalize(),
        _date(row["due_date"]), money(row["output_vat"]), money(row["input_vat"]), money(row["credit_in"]),
        money(row["vat_due"]), money(row["zatca_amount"]), money(row["paid"]), money(row["balance"]),
        _date(row["filed_on"]), "; ".join(row["sadad_references"]), status_text(row),
    ] for row in reversed(rows)]
    totals = ["TOTALS", "", "", "", "", money(sum(r["output_vat"] for r in rows)),
              money(sum(r["input_vat"] for r in rows)), "", "", "", money(sum(r["paid"] for r in rows)),
              "", "", "", ""]
    return build_csv(header=header(branch, settings, "VAT return history (credit notes subtracted; "
                                                     "credit b/f = credit carried in from the period before)"),
                     columns=HISTORY_COLUMNS, rows=body, totals=totals)


# -- PDF -------------------------------------------------------------------------

def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontSize=15, textColor=_INK, alignment=0, spaceAfter=2),
        "sub": ParagraphStyle("sub", parent=base["Normal"], fontSize=8.5, textColor=_MUTED),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=11.5, textColor=_INK, spaceBefore=8),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontSize=8, textColor=_INK, leading=10),
        "warn": ParagraphStyle("warn", parent=base["Normal"], fontSize=8.5, textColor=_WARN),
    }


def _p(text, style):
    return Paragraph(escape(str(text)), style)


def _table(data, widths, *, numeric_from=1, total_rows=()):
    table = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
        ("TEXTCOLOR", (0, 0), (-1, -1), _INK),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (numeric_from, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, _RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for index in total_rows:
        style += [("FONTNAME", (0, index), (-1, index), "Helvetica-Bold"), ("BACKGROUND", (0, index), (-1, index), _HEAD_BG)]
    table.setStyle(TableStyle(style))
    return table


def _document(buffer, title, author, pagesize):
    return SimpleDocTemplate(buffer, pagesize=pagesize, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm,
                             bottomMargin=14 * mm, title=title, author=author)


def _intro(story, styles, branch, settings, title):
    story.append(_p(title, styles["title"]))
    story.append(_p(f"{_restaurant(branch, settings)} · VAT number {settings.vat_number or '-'} · generated "
                    f"{_generated(settings)}", styles["sub"]))
    if profile(settings.environment).is_test:
        story.append(_p(f"TEST: {settings.environment} practice periods, not real VAT returns.", styles["warn"]))
    story.append(Spacer(1, 4 * mm))


def history_pdf(branch, settings, rows):
    """One summary table, newest period first."""
    styles = _styles()
    buffer = BytesIO()
    doc = _document(buffer, "VAT return history", _restaurant(branch, settings), landscape(A4))
    story = []
    _intro(story, styles, branch, settings, "VAT return history")
    cell = styles["cell"]
    data = [["Period", "Due", "Sales VAT", "Purchase VAT", "Credit b/f", "VAT due", "ZATCA amount", "Paid",
             "Balance", "Filed on", "SADAD ref(s)", "Status"]]
    for row in rows:
        data.append([
            _p(row["name"], cell), _date(row["due_date"]), money(row["output_vat"]), money(row["input_vat"]),
            money(row["credit_in"]), money(row["vat_due"]), money(row["zatca_amount"]) or "-", money(row["paid"]),
            money(row["balance"]), _date(row["filed_on"]) or "-", _p(", ".join(row["sadad_references"]) or "-", cell),
            _p(status_text(row), cell),
        ])
    widths = [34 * mm, 19 * mm, 20 * mm, 22 * mm, 19 * mm, 20 * mm, 22 * mm, 19 * mm, 19 * mm, 19 * mm, 28 * mm, 28 * mm]
    table = _table(data, widths, numeric_from=2)
    table.setStyle(TableStyle([("ALIGN", (9, 0), (-1, -1), "LEFT"), ("ALIGN", (1, 0), (1, -1), "LEFT")]))
    story.append(table)
    story.append(Spacer(1, 3 * mm))
    story.append(_p("Amounts in SAR from stored documents. VAT due (ours) = sales VAT - purchase VAT - credit b/f; "
                    "once filed, the balance is measured against ZATCA's amount.", styles["sub"]))
    doc.build(story)
    return buffer.getvalue()


def period_pdf(branch, settings, detail):
    """One period: facts, cards, the VAT return boxes, filing and payments."""
    styles = _styles()
    cell = styles["cell"]
    buffer = BytesIO()
    name = detail["name"]
    doc = _document(buffer, f"VAT return {name}", _restaurant(branch, settings), A4)
    story = []
    _intro(story, styles, branch, settings, f"VAT return {name}")
    figures, cards, filing = detail["figures"], detail["figures"]["cards"], detail["filing"]
    story.append(_p(f"{_date(detail['start_date'])} to {_date(detail['end_date'])} · {detail['frequency']} · "
                    f"due {_date(detail['due_date'])} · status {status_text(detail)}", styles["sub"]))

    story.append(_p("Summary", styles["h2"]))
    summary = [["", "SAR"],
               ["Sales VAT (output)", money(cards["output_vat"])],
               ["Purchase VAT (input, claimable)", money(cards["input_vat"])],
               ["    from purchase orders", money(cards["input_vat_purchases"])],
               ["    from expenses", money(cards["input_vat_expenses"])],
               [f"Credit carried in ({cards['credit_source']})", money(cards["credit_in"])]]
    if cards["correction"]:
        summary.append([_p(f"Box 14 correction: {detail['correction_reason']}", cell), money(cards["correction"])])
    summary.append(["VAT due (ours) = output - input + correction - credit", money(cards["vat_due"])])
    if cards["zatca_amount"] is not None:
        summary.append(["VAT due (ZATCA)", money(cards["zatca_amount"])])
    summary += [["Paid", money(cards["paid"])], ["Balance", money(cards["balance"])]]
    story.append(_table(summary, [110 * mm, 40 * mm], total_rows=(len(summary) - 1,)))

    story.append(_p("VAT return boxes", styles["h2"]))
    data = [["Box", "Name", "Amount", "Adjustment", "VAT"]]
    for box in figures["boxes"]:
        name_text = BOX_NAMES[box["key"]] + ("" if box["tracked"] else " (not recorded by this system)")
        data.append([box["box"], _p(name_text, cell), money(box["amount"]), money(box["adjustment"]), money(box["vat"])])
    totals = [index + 1 for index, box in enumerate(figures["boxes"]) if box["box"] in ("6", "12", "16")]
    story.append(_table(data, [12 * mm, 90 * mm, 28 * mm, 28 * mm, 24 * mm], numeric_from=2, total_rows=totals))
    story.append(_p("Amount = invoices; Adjustment = debit notes (+) and credit notes (-).", styles["sub"]))
    if figures["missing_breakdown"]:
        story.append(_p(f"{figures['missing_breakdown']} documents have no stored VAT split: counted in the summary, "
                        "not in the boxes.", styles["warn"]))

    story.append(_p("Filing", styles["h2"]))
    if filing:
        lines = [f"Filed on {_date(filing['filed_on'])} by {filing['filed_by']}. ZATCA amount "
                 f"{money(filing['zatca_amount'])}; ours when filed {money(filing['calculated_net_due'])}; "
                 f"difference {money(filing['difference'])}."]
        if filing["difference_reason"]:
            lines.append(f"Reason: {filing['difference_reason']}")
        if filing["credit_treatment"]:
            lines.append("Credit: " + filing["credit_treatment"].replace("_", " "))
        if filing["supersedes"]:
            lines.append(f"Amended return. Reason: {filing['amendment_reason']}")
        for line in lines:
            story.append(_p(line, styles["sub"]))
        changed = figures["changed_since_filing"]
        if changed:
            story.append(_p(f"Changed since filing: VAT due {money(changed['vat_due'])}.", styles["warn"]))
    else:
        story.append(_p("Not filed yet.", styles["sub"]))

    records = detail["payments"] if detail["payments"] else detail["refunds"]
    if records:
        is_payments = bool(detail["payments"])
        story.append(_p("Payments" if is_payments else "Refunds received", styles["h2"]))
        data = [["Date", "Reference", "Amount", "Note"]]
        for record in records:
            data.append([
                _date(record["paid_on"] if is_payments else record["received_on"]),
                _p(record["sadad_reference"] if is_payments else record["reference"], cell),
                money(record["amount"]),
                _p(f"Voided: {record['voided_reason']}" if record["voided"] else "", cell),
            ])
        story.append(_table(data, [26 * mm, 60 * mm, 28 * mm, 68 * mm], numeric_from=2))
        story[-1].setStyle(TableStyle([("ALIGN", (3, 0), (3, -1), "LEFT")]))

    doc.build(story)
    return buffer.getvalue()
