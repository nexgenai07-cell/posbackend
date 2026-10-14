from django.db import transaction
from django.http import Http404, HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrManager

from .importers import IMPORTERS
from .workbook import build_template, read_sheet

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _importer(kind, request):
    importer_class = IMPORTERS.get(kind)
    if importer_class is None:
        raise Http404
    return importer_class(request.user.branch)


class ImportTemplateView(APIView):
    """GET — the Excel template, pre-filled with the branch's current data so
    the admin edits what they already have instead of starting blank."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request, kind):
        importer = _importer(kind, request)
        content = build_template(
            importer.sheet_title, importer.columns, importer.template_rows(),
            importer.lists(), importer.instructions,
        )
        response = HttpResponse(content, content_type=XLSX)
        stamp = timezone.localdate().isoformat()
        response["Content-Disposition"] = f'attachment; filename="{importer.kind}-{stamp}.xlsx"'
        return response


class ImportView(APIView):
    """POST multipart `file`. Without `commit` it only previews: every row
    with its create / update (old -> new) / unchanged / delete / error, and
    nothing is written. With `commit=1` the same plan is applied in one
    transaction — refused outright while any row still has an error."""

    permission_classes = [IsOwnerOrManager]
    parser_classes = [MultiPartParser]

    def post(self, request, kind):
        importer = _importer(kind, request)
        commit = str(request.data.get("commit", "")).lower() in ("1", "true", "yes")
        sheet = read_sheet(request.FILES.get("file"), importer.sheet_title, importer.columns)
        rows = [] if sheet.file_errors else [result.as_dict() for result in importer.plan(sheet.rows)]
        summary = {action: 0 for action in ("create", "update", "unchanged", "delete", "error")}
        for row in rows:
            summary[row["action"]] += 1
        payload = {
            "kind": importer.kind,
            "columns": [{"key": column.key, "header": column.header} for column in importer.columns],
            "file_errors": sheet.file_errors, "rows": rows, "summary": summary, "committed": False,
        }
        if not commit:
            return Response(payload)
        if sheet.file_errors or summary["error"]:
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            importer.apply()
        payload["committed"] = True
        return Response(payload)
