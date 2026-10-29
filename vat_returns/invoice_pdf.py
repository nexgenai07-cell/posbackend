"""
A sales document (tax invoice, simplified tax invoice, credit or debit
note) as an A4 PDF, for the invoice archive and its ZIP.

Built from the same payload as the printed receipt (zatca/receipts.py), so
it shows the document as issued: the frozen seller, buyer and lines, never
today's settings. Bilingual like the receipt (ZatcaReceipt.tsx): English on
the left, Arabic on the right, the QR only where the receipt prints one.
Amounts are printed as stored; nothing is added up here.

Needs the Arabic font and shaping libraries (common/pdf_fonts.py); without
them render() raises pdf_fonts.PdfUnavailable.
"""

from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from common import pdf_fonts
from zatca import receipts

_INK = colors.HexColor("#111827")
_RULE = colors.HexColor("#d1d5db")
_HEAD_BG = colors.HexColor("#f3f4f6")
_WARN = colors.HexColor("#b45309")

# The receipt's fixed label pairs (ZatcaReceipt.tsx), English and Arabic.
L = {
    "simplified": ("Simplified Tax Invoice", "فاتورة ضريبية مبسطة"),
    "standard": ("Tax Invoice", "فاتورة ضريبية"),
    "credit_note": ("Credit Note", "إشعار دائن"),
    "debit_note": ("Debit Note", "إشعار مدين"),
    "test": ("TEST: not a legally valid invoice", "تجريبي: ليست فاتورة نظامية"),
    "vat": ("VAT No.", "الرقم الضريبي"),
    "crn": ("CR No.", "السجل التجاري"),
    "number": ("Invoice No.", "رقم الفاتورة"),
    "note_number": ("Note No.", "رقم الإشعار"),
    "date": ("Date", "التاريخ"),
    "time": ("Time", "الوقت"),
    "order_type": ("Order type", "نوع الطلب"),
    "table": ("Table", "الطاولة"),
    "order": ("Order", "الطلب"),
    "buyer": ("Buyer", "المشتري"),
    "original": ("Original invoice", "الفاتورة الأصلية"),
    "reason": ("Reason", "السبب"),
    "item": ("Item", "المنتجات"),
    "quantity": ("Qty", "الكمية"),
    "unit_net": ("Price (excl. VAT)", "السعر"),
    "line_vat": ("VAT", "ضريبة القيمة المضافة"),
    "line_total": ("Total incl. VAT", "السعر شامل ضريبة القيمة المضافة"),
    "net": ("Total taxable amount", "إجمالي المبلغ الخاضع للضريبة"),
    "vat_amount": ("VAT", "ضريبة القيمة المضافة"),
    "inclusive": ("Total incl. VAT", "المجموع مع الضريبة"),
    "rounding": ("Rounding", "فرق التقريب"),
    "payable": ("Amount due", "المبلغ المستحق"),
    "refund": ("Amount refunded", "المبلغ المسترد"),
    "tip": ("Tip (not part of tax invoice)", "إكرامية (ليست جزءاً من الفاتورة الضريبية)"),
    "currency": ("Amounts in SAR", "المبالغ بالريال السعودي"),
    "pending_clearance": ("Pending clearance by ZATCA: the QR code will be on the cleared copy",
                          "بانتظار اعتماد هيئة الزكاة والضريبة والجمارك"),
    "rejected": ("Not accepted by ZATCA", "لم تقبلها الهيئة"),
}
ORDER_TYPES = {"dine_in": ("Dine-in", "محلي"), "takeaway": ("Takeaway", "سفري"),
               "qr": ("Table QR order", "طلب من الطاولة"), "web": ("Online order", "طلب أونلاين")}
METHODS = {"cash": ("Cash", "نقداً"), "card": ("Card", "بطاقة"), "other": ("Other", "أخرى"), "mada": ("mada", "مدى"),
           "online": ("Online", "أونلاين")}
ID_SCHEMES = {"CRN": L["crn"], "MOM": ("MOMRA licence", "رخصة وزارة الشؤون البلدية"),
              "MLS": ("MLSD licence", "رخصة وزارة الموارد البشرية"), "SAG": ("SAGIA licence", "ترخيص الاستثمار"),
              "700": ("700 No.", "الرقم الموحد 700"), "OTH": ("ID No.", "رقم الهوية")}


def _styles():
    base = {"fontName": pdf_fonts.REGULAR, "fontSize": 9, "leading": 12, "textColor": _INK}
    return {
        "left": ParagraphStyle("left", **base, alignment=TA_LEFT),
        "right": ParagraphStyle("right", **base, alignment=TA_RIGHT),
        "center": ParagraphStyle("center", **base, alignment=TA_CENTER),
        "title": ParagraphStyle("title", **{**base, "fontName": pdf_fonts.BOLD, "fontSize": 14, "leading": 18},
                                alignment=TA_CENTER),
        "bold": ParagraphStyle("bold", **{**base, "fontName": pdf_fonts.BOLD}, alignment=TA_LEFT),
        "bold_right": ParagraphStyle("bold_right", **{**base, "fontName": pdf_fonts.BOLD}, alignment=TA_RIGHT),
        "warn": ParagraphStyle("warn", **{**base, "fontName": pdf_fonts.BOLD, "textColor": _WARN},
                               alignment=TA_CENTER),
    }


def _text(value):
    return escape(pdf_fonts.shape(value))


def _address(address):
    first = " ".join(part for part in (address.get("building_no"), address.get("street")) if part)
    if address.get("additional_no"):
        first += f" ({address['additional_no']})"
    second = ", ".join(part for part in (address.get("district"), address.get("city"), address.get("postal_code"))
                       if part)
    return [line for line in (first, second) if line]


def _fields(rows, styles):
    """[(pair, value)] as a three-column table: English label, value, Arabic label."""
    data = [[Paragraph(_text(pair[0]), styles["left"]), Paragraph(_text(value), styles["center"]),
             Paragraph(_text(pair[1]), styles["right"])] for pair, value in rows]
    table = Table(data, colWidths=[55 * mm, 72 * mm, 55 * mm])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 1),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    return table


def _title(data):
    if data["kind"] in ("credit_note", "debit_note"):
        return L[data["kind"]]
    return L["simplified"] if data["simplified"] else L["standard"]


def _items(data, styles):
    header = [Paragraph(f"{_text(pair[0])}<br/>{_text(pair[1])}", styles["center"])
              for pair in (L["item"], L["quantity"], L["unit_net"], L["line_vat"], L["line_total"])]
    rows = [header]
    for item in data["items"]:
        lines = item["lines"] or [{"quantity": item["quantity"], "unit_net": "-", "vat": "-", "rate": "",
                                   "total": item["total"]}]
        name_en = " ".join(part for part in (item.get("name_en"), f"({item['variant_en']})" if item.get("variant_en")
                                             else "") if part)
        name_ar = " ".join(part for part in (item.get("name_ar"), f"({item['variant_ar']})" if item.get("variant_ar")
                                             else "") if part)
        name = _text(name_en)
        if name_ar and name_ar != name_en:
            name += f"<br/>{_text(name_ar)}"
        for index, line in enumerate(lines):
            vat = str(line["vat"])
            if not data["simplified"] and line.get("rate"):
                vat += f" ({_rate(line['rate'])}%)"
            rows.append([Paragraph(name if index == 0 else "", styles["left"]), str(line["quantity"]),
                         str(line["unit_net"]), vat, str(line["total"])])
    table = Table(rows, colWidths=[70 * mm, 18 * mm, 32 * mm, 30 * mm, 32 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), pdf_fonts.REGULAR),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, _RULE),
    ]))
    return table


def _rate(rate):
    return format(float(rate), "g")


def _totals(data, styles):
    totals = data["totals"]
    rate = _rate(totals.get("rate") or 0)
    rounding = float(totals.get("rounding") or 0) != 0
    rows = [(L["net"], totals.get("net")),
            ((f"{L['vat_amount'][0]} ({rate}%)", f"{L['vat_amount'][1]} ({rate}%)"), totals.get("vat")),
            (L["inclusive"], totals.get("inclusive"))]
    if rounding:
        rows.append((L["rounding"], totals.get("rounding")))
    if rounding or data["kind"] == "credit_note":
        rows.append((L["refund"] if data["kind"] == "credit_note" else L["payable"], totals.get("payable")))
    for payment in data["payments"]:
        rows.append((METHODS.get(payment["method"], (payment["method"], payment["method"])), payment["amount"]))
    if float(data.get("tip") or 0) > 0 and data["kind"] == "invoice":
        rows.append((L["tip"], data["tip"]))
    return _fields(rows, styles)


def _qr(value):
    widget = QrCodeWidget(value, barLevel="M")
    left, bottom, right, top = widget.getBounds()
    size = 34 * mm
    drawing = Drawing(size, size, transform=[size / (right - left), 0, 0, size / (top - bottom), 0, 0])
    drawing.add(widget)
    drawing.hAlign = "CENTER"
    return drawing


def render(document, settings=None):
    """The document's PDF as bytes."""
    pdf_fonts.register()
    data = receipts.payload(document, settings=settings)
    styles = _styles()
    seller, buyer = data["seller"], data["buyer"]
    is_note = data["kind"] != "invoice"
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=f"{_title(data)[0]} {data['number']}",
                            author=seller.get("name_en") or seller.get("name_ar") or "")
    story = []
    if data["is_test"]:
        story += [Paragraph(f"{_text(L['test'][0])} · {_text(L['test'][1])}", styles["warn"]), Spacer(1, 2 * mm)]

    title = _title(data)
    story.append(Paragraph(f"{_text(title[0])} · {_text(title[1])}", styles["title"]))
    story.append(Spacer(1, 2 * mm))
    for name in dict.fromkeys(n for n in (seller.get("name_ar"), seller.get("name_en")) if n):
        story.append(Paragraph(_text(name), styles["center"]))
    for line in _address(seller.get("address") or {}):
        story.append(Paragraph(_text(line), styles["center"]))
    seller_rows = [(L["vat"], seller.get("vat_number", ""))]
    if seller.get("other_id"):
        seller_rows.append((ID_SCHEMES.get(seller.get("other_id_scheme"), L["crn"]), seller["other_id"]))
    story += [Spacer(1, 2 * mm), _fields(seller_rows, styles), Spacer(1, 3 * mm)]

    facts = [(L["note_number"] if is_note else L["number"], data["number"]), (L["date"], data["issue_date"]),
             (L["time"], data["issue_time"])]
    if data["order_type"] in ORDER_TYPES:
        pair = ORDER_TYPES[data["order_type"]]
        facts.append((L["order_type"], f"{pair[0]} / {pair[1]}"))
    if data["table"]:
        facts.append((L["table"], data["table"]))
    if data["order_numbers"]:
        facts.append((L["order"], ", ".join(data["order_numbers"])))
    if is_note and data["original"]:
        facts.append((L["original"], data["original"].get("number", "")))
        if data["reason"]:
            facts.append((L["reason"], data["reason"]))
    story += [_fields(facts, styles), Spacer(1, 3 * mm)]

    if buyer:
        story.append(Paragraph(f"{_text(L['buyer'][0])} · {_text(L['buyer'][1])}", styles["bold"]))
        buyer_rows = [((" ", " "), buyer.get("legal_name", "")), (L["vat"], buyer.get("vat_number", ""))]
        if buyer.get("crn"):
            buyer_rows.append((L["crn"], buyer["crn"]))
        buyer_rows += [((" ", " "), line) for line in _address(buyer)]
        story += [_fields(buyer_rows, styles), Spacer(1, 3 * mm)]

    story += [_items(data, styles), Spacer(1, 2 * mm),
              Paragraph(f"{_text(L['currency'][0])} · {_text(L['currency'][1])}", styles["center"]),
              _totals(data, styles), Spacer(1, 4 * mm)]

    if data["qr"]:
        story.append(_qr(data["qr"]))
    elif data["rejected"]:
        story.append(Paragraph(f"{_text(L['rejected'][0])} · {_text(L['rejected'][1])}", styles["warn"]))
    elif data["pending_clearance"]:
        story.append(Paragraph(f"{_text(L['pending_clearance'][0])}<br/>{_text(L['pending_clearance'][1])}",
                               styles["warn"]))
    for footer in (data["footer_ar"], data["footer_en"]):
        if footer:
            story.append(Paragraph(_text(footer), styles["center"]))
    doc.build(story)
    return buffer.getvalue()
