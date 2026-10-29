"""
CSV files for the accountant: UTF-8 with a BOM and CRLF line ends, so Excel
opens Arabic names correctly on double-click. Every tax export (input VAT,
VAT return history, the invoice archive's index) is written here so they all
look the same:

    Restaurant,<name>            <- header block: (label, value) pairs
    VAT number,<vat>
    Period,2026-07-01 to 2026-09-30
    (blank line)
    Date,Type,...                <- column headers
    ...rows...
    TOTALS,,,...                 <- optional totals row

Amounts are written by the caller with money() so they always have 2 decimals.
"""

import csv
import io
from decimal import Decimal

from django.http import HttpResponse

BOM = "﻿"


def money(value):
    """2-decimal text for an amount; blank for None."""
    if value is None:
        return ""
    return f"{Decimal(value):.2f}"


def build_csv(*, header=(), columns, rows, totals=None):
    """The CSV text (BOM included). `header` is [(label, value)], `columns`
    the column titles, `rows` and `totals` lists of cell values."""
    out = io.StringIO()
    out.write(BOM)
    writer = csv.writer(out, lineterminator="\r\n")
    for label, value in header:
        writer.writerow([label, value])
    if header:
        writer.writerow([])
    writer.writerow(columns)
    writer.writerows(rows)
    if totals is not None:
        writer.writerow(totals)
    return out.getvalue()


def csv_response(filename, **kwargs):
    """An attachment download of build_csv(**kwargs)."""
    response = HttpResponse(build_csv(**kwargs).encode("utf-8"), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
