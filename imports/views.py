import json

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrManager

from .importers import ACTIONS, START_LINES, RestaurantImport
from .workbook import build_workbook, mark_problems, open_upload, read_sheet

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(content, name):
    response = HttpResponse(content, content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="{name}-{timezone.localdate().isoformat()}.xlsx"'
    return response


class ImportTemplateView(APIView):
    """GET — the restaurant workbook (Stock, Menu, Recipes sheets), pre-filled
    with the branch's current data so the admin edits what they already have
    instead of starting blank."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        restaurant = RestaurantImport(request.user.branch)
        return _xlsx(build_workbook(START_LINES, restaurant.specs(), restaurant.lists()), "restaurant")


class ImportGuideView(APIView):
    """GET — what every column means, how far the branch's setup is, and a
    ready-made AI prompt, all from the same definitions the upload uses."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        return Response(RestaurantImport(request.user.branch).guide())


class ImportView(APIView):
    """POST multipart `file`. Without `commit` it only previews: every row of
    every sheet with its create / update (old -> new) / unchanged / delete /
    error, and nothing is written. With `commit=1` the same plan is applied in
    one transaction — refused outright while any row still has an error."""

    permission_classes = [IsOwnerOrManager]
    parser_classes = [MultiPartParser]

    def post(self, request):
        restaurant = RestaurantImport(request.user.branch)
        commit = str(request.data.get("commit", "")).lower() in ("1", "true", "yes")
        book, file_errors = open_upload(request.FILES.get("file"))
        parsed = {}
        if book is not None:
            for sheet in restaurant.sheets:
                parsed[sheet.key], problems = read_sheet(book, sheet.sheet_title, sheet.columns)
                file_errors += problems
            book.close()
            if not file_errors and not any(sheet.rows for sheet in parsed.values()):
                file_errors.append({"code": "noRows"})

        results = {} if file_errors else restaurant.plan(parsed)
        summary = dict.fromkeys(ACTIONS, 0)
        sheets = []
        for sheet in restaurant.sheets:
            rows = [result.as_dict() for result in results.get(sheet.key, [])]
            sheet_summary = dict.fromkeys(ACTIONS, 0)
            for row in rows:
                sheet_summary[row["action"]] += 1
                summary[row["action"]] += 1
            sheets.append({
                "key": sheet.key, "title": sheet.sheet_title,
                "columns": [{"key": column.key, "header": column.header} for column in sheet.columns],
                "rows": rows, "summary": sheet_summary,
            })
        payload = {"file_errors": file_errors, "sheets": sheets, "summary": summary, "committed": False}
        if not commit:
            return Response(payload)
        if file_errors or summary["error"]:
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            restaurant.apply()
        payload["committed"] = True
        return Response(payload)


class ImportMarkedView(APIView):
    """POST multipart `file` + `marks` (JSON list of {sheet, row, field,
    text}) — the admin's own file back with each problem cell filled red and
    its message as a cell comment. The text comes from the admin page, so it
    is in the language the admin reads the preview in."""

    permission_classes = [IsOwnerOrManager]
    parser_classes = [MultiPartParser]

    def post(self, request):
        try:
            marks = json.loads(request.data.get("marks") or "[]")
        except ValueError:
            marks = None
        if not isinstance(marks, list):
            return Response({"error": "imports.marksInvalid"}, status=status.HTTP_400_BAD_REQUEST)
        book, file_errors = open_upload(request.FILES.get("file"), read_only=False)
        if book is None:
            return Response({"file_errors": file_errors}, status=status.HTTP_400_BAD_REQUEST)

        sheets = {sheet.key: sheet for sheet in RestaurantImport(request.user.branch).sheets}
        positions = {}
        cells = []
        for mark in marks:
            sheet = sheets.get(mark.get("sheet")) if isinstance(mark, dict) else None
            row = mark.get("row") if sheet else None
            if sheet is None or not isinstance(row, int) or row < 2:
                continue
            if sheet.key not in positions:
                positions[sheet.key] = read_sheet(book, sheet.sheet_title, sheet.columns)[0].positions
            columns = positions[sheet.key]
            column = columns.get(mark.get("field")) or columns.get(sheet.name_field) or 1
            cells.append((sheet.sheet_title, row, column, str(mark.get("text", ""))[:1000]))
        return _xlsx(mark_problems(book, cells), "restaurant-to-fix")
