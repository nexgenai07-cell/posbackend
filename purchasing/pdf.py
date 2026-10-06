"""
Purchase-order PDF.

Generated server-side, from the database, on purpose. The requirement is that
the PDF, the print view and the on-screen PO all show the same numbers — the
only way to guarantee that structurally is to build the document from the same
rows the API serves, rather than from whatever the browser happens to be
holding. A frontend generator can drift the moment a tab is left open.

Branding comes from the Branch record (name + logo), never a hard-coded
restaurant, so whoever runs this system gets their own letterhead.
"""

import logging
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger("purchasing.pdf")

_INK = colors.HexColor("#1f2933")
_MUTED = colors.HexColor("#6b7280")
_RULE = colors.HexColor("#d1d5db")
_HEAD_BG = colors.HexColor("#f3f4f6")


def _logo_flowable(logo_url):
    """
    The restaurant's logo, or nothing.

    Deliberately best-effort: a slow or dead logo URL must never be the reason
    a purchase order cannot be printed, so any failure degrades to a text-only
    header.
    """
    if not logo_url:
        return None
    try:
        import requests

        response = requests.get(logo_url, timeout=5)
        if not response.ok:
            return None
        image = Image(ImageReader(BytesIO(response.content)))
        image.drawHeight = 16 * mm
        image.drawWidth = 16 * mm * (image.imageWidth / image.imageHeight)
        return image
    except Exception as exc:  # noqa: BLE001 - never fail a document over a logo
        logger.warning("Could not load the branch logo for the PO PDF: %s", exc)
        return None


def build_purchase_pdf(purchase):
    """Return the PDF bytes for one purchase order."""
    branch = purchase.branch
    currency = branch.currency or ""
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Title"], fontSize=16, textColor=_INK, alignment=0)
    label = ParagraphStyle("label", parent=styles["Normal"], fontSize=8, textColor=_MUTED)
    value = ParagraphStyle("value", parent=styles["Normal"], fontSize=10, textColor=_INK)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"PO-{purchase.pk}", author=branch.name_en or "",
    )

    story = []

    logo = _logo_flowable(branch.logo_url)
    header_cells = [[logo or "", Paragraph(branch.name_en or branch.name_ar or "", title_style)]]
    header = Table(header_cells, colWidths=[20 * mm, None])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(header)
    if branch.address:
        story.append(Paragraph(branch.address, label))
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph(f"Purchase Order PO-{purchase.pk}", ParagraphStyle(
        "po", parent=styles["Heading2"], fontSize=13, textColor=_INK,
    )))
    story.append(Spacer(1, 3 * mm))

    meta = Table([
        [Paragraph("Supplier", label), Paragraph("Status", label),
         Paragraph("Ordered", label), Paragraph("Received", label)],
        [Paragraph(purchase.supplier.name_en or "", value),
         Paragraph(purchase.get_status_display(), value),
         Paragraph(purchase.ordered_at.strftime("%d %b %Y"), value),
         Paragraph(purchase.received_at.strftime("%d %b %Y") if purchase.received_at else "—", value)],
    ], colWidths=[None] * 4)
    meta.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
    ]))
    story.append(meta)
    story.append(Spacer(1, 7 * mm))

    rows = [["Ingredient", "Quantity", "Unit", "Unit cost", "Subtotal"]]
    subtotal = Decimal("0")
    for item in purchase.items.select_related("inventory_item"):
        line_total = item.quantity * item.unit_cost
        subtotal += line_total
        rows.append([
            item.inventory_item.name,
            f"{item.quantity:g}",
            item.inventory_item.unit,
            f"{item.unit_cost:,.2f}",
            f"{line_total:,.2f}",
        ])
    rows.append(["", "", "", f"Grand total ({currency})", f"{subtotal:,.2f}"])

    table = Table(rows, colWidths=[None, 24 * mm, 20 * mm, 28 * mm, 30 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
        ("TEXTCOLOR", (0, 0), (-1, -1), _INK),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -2), 0.4, _RULE),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, _INK),
        ("FONTNAME", (3, -1), (-1, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)

    doc.build(story)
    return buffer.getvalue()
