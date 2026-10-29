"""
The boxes of ZATCA's VAT return form, in the form's order, so the owner can
copy each figure into the e-services portal.

Box names are the ones I know from the form; they are listed "to confirm"
in docs/zatca-progress.md (A8): 2, 8, 9, 14, 15, 16 and whether a separate
5% line still exists under box 1. Renaming a box is a change here (`name`,
for the CSV/PDF) and in the admin's vat.json (`boxes.<key>`), nothing else.

`tracked = False`: the restaurant has nothing the POS records for this box
(e.g. exports, imports); it is shown as 0 so the form lines up.
"""

BOXES = [
    # (number, key, English name, tracked)
    ("1", "standardSales", "Standard rated sales", True),
    ("2", "citizenSales", "Private healthcare / private education / first house sales to citizens", False),
    ("3", "zeroRatedSales", "Zero rated domestic sales", True),
    ("4", "exports", "Exports", False),
    ("5", "exemptSales", "Exempt sales", True),
    ("6", "totalSales", "Total sales", True),
    ("7", "standardPurchases", "Standard rated domestic purchases", True),
    ("8", "importsCustoms", "Imports subject to VAT paid at customs", False),
    ("9", "importsReverseCharge", "Imports subject to VAT accounted for through reverse charge mechanism", False),
    ("10", "zeroRatedPurchases", "Zero rated purchases", True),
    ("11", "exemptPurchases", "Exempt purchases", True),
    ("12", "totalPurchases", "Total purchases", True),
    ("13", "vatDueCurrent", "Total VAT due for current period", True),
    ("14", "corrections", "Corrections from previous period (between SAR ±5,000)", True),
    ("15", "creditCarried", "VAT credit carried forward from previous period(s)", True),
    ("16", "netVatDue", "Net VAT due (or claimed)", True),
]

# Sales boxes by the UBL VAT category (zatca/vat.py VatCategory). "O" (out
# of scope) has no box: it isn't part of the VAT return.
SALES_BOX = {"S": "1", "Z": "3", "E": "5"}
# Purchases boxes by SupplierDocument.vat_category.
PURCHASES_BOX = {"standard": "7", "zero": "10", "exempt": "11"}
SALES_BOXES = ("1", "2", "3", "4", "5")
PURCHASES_BOXES = ("7", "8", "9", "10", "11")
# Boxes 13-16 are VAT only: no amount or adjustment column.
VAT_ONLY = ("13", "14", "15", "16")
