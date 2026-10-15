import base64
import json
from io import BytesIO

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from openpyxl import load_workbook

from accounts.permissions import IsOwnerOrManager

from . import ai
from .importers import ACTIONS, START_LINES, RestaurantImport
from .models import AiDraft
from .workbook import ESTIMATE_FILL, build_workbook, mark_cells, open_upload, read_sheet

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
        guide = RestaurantImport(request.user.branch).guide()
        guide["ai"] = {"enabled": ai.enabled(), "left_today": _ai_left_today(request.user.branch)}
        return Response(guide)


def _ai_left_today(branch):
    used = AiDraft.objects.filter(branch=branch, created_at__date=timezone.localdate()).count()
    return max(settings.AI_DRAFTS_PER_DAY - used, 0)


class ImportAiDraftView(APIView):
    """POST {text} — the restaurant workbook with an AI draft of the admin's
    request merged in, as a file the page then previews like any upload:
    {file (base64 .xlsx), filename, summary, estimated: [{sheet, row, fields}]}.
    Nothing is written here except the AiDraft log row; saving still goes
    through the import preview and Confirm."""

    permission_classes = [IsOwnerOrManager]

    def post(self, request):
        branch = request.user.branch
        if not ai.enabled():
            return Response({"error": "imports.aiNotConfigured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        text = str(request.data.get("text") or "").strip()
        if not text:
            return Response({"error": "imports.aiTextRequired"}, status=status.HTTP_400_BAD_REQUEST)
        if len(text) > ai.MAX_TEXT:
            return Response({"error": "imports.aiTextTooLong"}, status=status.HTTP_400_BAD_REQUEST)
        if _ai_left_today(branch) == 0:
            return Response({"error": "imports.aiDailyLimit"}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Logged before the call: a request that fails still costs money and
        # still counts toward the day's allowance.
        log = AiDraft.objects.create(branch=branch, requested_by=request.user)
        restaurant = RestaurantImport(branch)
        try:
            draft, (log.prompt_tokens, log.completion_tokens) = ai.draft(restaurant, text)
        except ai.AiDraftError as error:
            log.error = error.code
            log.save(update_fields=["error"])
            return Response({"error": error.code}, status=status.HTTP_502_BAD_GATEWAY)
        log.succeeded = True
        log.save(update_fields=["succeeded", "prompt_tokens", "completion_tokens"])

        specs, estimated = restaurant.draft_specs(draft)
        book = load_workbook(BytesIO(build_workbook(START_LINES, specs, restaurant.lists())))
        sheets = {sheet.key: sheet for sheet in restaurant.sheets}
        cells = [
            (sheets[key].sheet_title, row, [c.key for c in sheets[key].columns].index(field) + 1, "AI estimate: check this value before saving.")
            for key, row, field in estimated
        ]
        content = mark_cells(book, cells, fill=ESTIMATE_FILL)
        rows = {}
        for key, row, field in estimated:
            rows.setdefault((key, row), []).append(field)
        return Response({
            "file": base64.b64encode(content).decode("ascii"),
            "filename": f"restaurant-ai-draft-{timezone.localdate().isoformat()}.xlsx",
            "summary": draft["summary"],
            "estimated": [{"sheet": key, "row": row, "fields": fields} for (key, row), fields in rows.items()],
        })


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
        return _xlsx(mark_cells(book, cells), "restaurant-to-fix")
