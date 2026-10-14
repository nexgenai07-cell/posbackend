"""
Excel plumbing shared by every importer: building the downloadable template
and reading an uploaded file back into plain row dicts.

Nothing here knows what a product or an ingredient is — importers.py
describes each sheet with `Column`s and this module turns that description
into a workbook (headers, widths, dropdowns, an instructions sheet) and back.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from io import BytesIO

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

MAX_ROWS = 5000
MAX_FILE_BYTES = 5 * 1024 * 1024
YES_NO = ("Yes", "No")


@dataclass
class Column:
    key: str
    header: str
    help: str
    required: bool = False
    width: int = 18
    example: str = ""
    # Name of a list on the hidden "Lists" sheet offered as a dropdown. The
    # dropdown only suggests: typing a new value (a new category) is allowed.
    choices: str | None = None

    @property
    def label(self):
        return f"{self.header} *" if self.required else self.header


@dataclass
class ParsedRow:
    number: int  # the Excel row number the admin sees
    values: dict


@dataclass
class ParsedSheet:
    rows: list[ParsedRow] = field(default_factory=list)
    file_errors: list[dict] = field(default_factory=list)


def _normalise_header(text):
    return " ".join(str(text or "").replace("*", " ").lower().split())


def build_template(sheet_title, columns, rows, lists, instructions):
    """The workbook an admin downloads: their current data on the first sheet,
    then a sheet explaining each column, then the hidden dropdown lists."""
    book = Workbook()
    sheet = book.active
    sheet.title = sheet_title
    header_fill = PatternFill("solid", fgColor="FBE3D6")
    for index, column in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=column.label)
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
        sheet.column_dimensions[get_column_letter(index)].width = column.width
    sheet.freeze_panes = "A2"
    for row_index, row in enumerate(rows, start=2):
        for col_index, column in enumerate(columns, start=1):
            value = row.get(column.key)
            if value not in (None, ""):
                sheet.cell(row=row_index, column=col_index, value=value)

    lists_sheet = book.create_sheet("Lists")
    for list_index, (name, values) in enumerate(lists.items(), start=1):
        letter = get_column_letter(list_index)
        lists_sheet.cell(row=1, column=list_index, value=name)
        for value_index, value in enumerate(values, start=2):
            lists_sheet.cell(row=value_index, column=list_index, value=value)
        if not values:
            continue
        reference = f"Lists!${letter}$2:${letter}${len(values) + 1}"
        for col_index, column in enumerate(columns, start=1):
            if column.choices != name:
                continue
            validation = DataValidation(type="list", formula1=reference, allow_blank=True)
            # Warning, not stop: the list is a suggestion. A brand-new
            # category or unit is legitimate and is reported in the preview.
            validation.errorStyle = "warning"
            validation.showErrorMessage = False
            col_letter = get_column_letter(col_index)
            validation.add(f"{col_letter}2:{col_letter}{MAX_ROWS + 1}")
            sheet.add_data_validation(validation)
    lists_sheet.sheet_state = "hidden"

    help_sheet = book.create_sheet("Instructions", 1)
    help_sheet.column_dimensions["A"].width = 26
    help_sheet.column_dimensions["B"].width = 90
    help_sheet.column_dimensions["C"].width = 24
    for line_index, line in enumerate(instructions, start=1):
        help_sheet.cell(row=line_index, column=1, value=line).alignment = Alignment(wrap_text=False)
    start = len(instructions) + 2
    for col_index, title in enumerate(("Column", "What to enter", "Example"), start=1):
        help_sheet.cell(row=start, column=col_index, value=title).font = Font(bold=True)
    for offset, column in enumerate(columns, start=1):
        help_sheet.cell(row=start + offset, column=1, value=column.label)
        help_sheet.cell(row=start + offset, column=2, value=column.help).alignment = Alignment(wrap_text=True)
        help_sheet.cell(row=start + offset, column=3, value=column.example)
    help_sheet.cell(row=start + len(columns) + 2, column=1, value="* = required")

    output = BytesIO()
    book.save(output)
    return output.getvalue()


def read_sheet(upload, sheet_title, columns):
    """Rows of the uploaded file as {column key: raw cell value}. Problems that
    make the whole file unreadable come back as file_errors, never raised."""
    result = ParsedSheet()
    if upload is None:
        result.file_errors.append({"code": "fileRequired"})
        return result
    if upload.size > MAX_FILE_BYTES:
        result.file_errors.append({"code": "fileTooLarge", "params": {"max_mb": MAX_FILE_BYTES // (1024 * 1024)}})
        return result
    try:
        book = load_workbook(upload, read_only=True, data_only=True)
    except Exception:  # openpyxl raises a zoo of types for "not a workbook"
        result.file_errors.append({"code": "notExcel"})
        return result

    sheet = book[sheet_title] if sheet_title in book.sheetnames else book.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None) or ()
    by_header = {_normalise_header(column.header): column for column in columns}
    positions = {}
    for index, text in enumerate(header):
        column = by_header.get(_normalise_header(text))
        if column is not None and column.key not in positions:
            positions[column.key] = index
    missing = [column.label for column in columns if column.required and column.key not in positions]
    if missing:
        result.file_errors.append({"code": "missingColumns", "params": {"columns": ", ".join(missing)}})
        return result

    for number, raw in enumerate(rows, start=2):
        values = {key: (raw[index] if index < len(raw) else None) for key, index in positions.items()}
        if all(value is None or str(value).strip() == "" for value in values.values()):
            continue
        if len(result.rows) >= MAX_ROWS:
            result.file_errors.append({"code": "tooManyRows", "params": {"max": MAX_ROWS}})
            return result
        result.rows.append(ParsedRow(number=number, values=values))
    book.close()
    if not result.rows:
        result.file_errors.append({"code": "noRows"})
    return result


# ── Cell readers. Each returns (value, error) so a row collects every problem
#    at once instead of stopping at the first. ────────────────────────────────

def text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # "12" typed into a cell Excel decided was a number
    return " ".join(str(value).split())


def decimal(value, column, *, minimum=Decimal("0"), allow_zero=True):
    raw = text(value)
    if raw == "":
        return None, None
    try:
        number = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None, {"code": "numberInvalid", "params": {"column": column.header, "value": raw}}
    if not number.is_finite() or number < minimum or (not allow_zero and number == 0):
        return None, {"code": "numberTooSmall" if allow_zero else "numberPositive", "params": {"column": column.header, "value": raw}}
    return number, None


def yes_no(value, column):
    raw = text(value).lower()
    if raw == "":
        return None, None
    if raw in ("yes", "y", "true", "1", "نعم"):
        return True, None
    if raw in ("no", "n", "false", "0", "لا"):
        return False, None
    return None, {"code": "yesNoInvalid", "params": {"column": column.header, "value": text(value)}}


def show(value):
    """How a stored value is echoed back in the preview's old → new."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return YES_NO[0] if value else YES_NO[1]
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")  # 600.00 -> "600", 0.250 -> "0.25"
    return str(value)
