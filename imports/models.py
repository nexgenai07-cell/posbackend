import uuid

from django.db import models


class AiDraft(models.Model):
    """One "Fill with AI" request. Kept so a branch's daily allowance can be
    enforced (every request costs money, even one that fails) and so spend
    can be looked up later."""

    branch = models.ForeignKey("branches.Branch", on_delete=models.CASCADE, related_name="ai_drafts")
    requested_by = models.ForeignKey("accounts.Staff", on_delete=models.SET_NULL, null=True, related_name="ai_drafts")
    created_at = models.DateTimeField(auto_now_add=True)
    succeeded = models.BooleanField(default=False)
    error = models.CharField(max_length=64, blank=True)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["branch", "created_at"])]


class ImportDownload(models.Model):
    """The stock values one downloaded workbook was filled with, found again
    by the file code on its "Start here" sheet. Lets an upload tell a cell
    the admin changed from one the restaurant changed since the download
    (stock sold, a purchase received) — kept here, not in the file, so it
    works in any spreadsheet app and cannot be edited by mistake."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey("branches.Branch", on_delete=models.CASCADE, related_name="import_downloads")
    created_at = models.DateTimeField(auto_now_add=True)
    # {casefolded item name: {field: "decimal string"}}
    stock = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["branch", "created_at"])]
