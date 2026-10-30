"""
VAT returns: the tax periods the restaurant files to ZATCA (by hand, on the
e-services portal), and the audit log of everything done to them. See
docs/vat-dashboard-plan.md.

Payments and refunds are voided, never deleted.
None of these models is a BaseModel and none is ever soft- or hard-deleted
once it carries a filing. Periods without one are only placeholders for
dates: periods.rebuild() may replace them while nothing has been filed.
"""

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

from accounts.models import Staff
from branches.models import Branch
from zatca.environments import Environment
from zatca.models import VatFilingFrequency


class PeriodStatus(models.TextChoices):
    OPEN = "open", "Not filed"
    FILED = "filed", "Filed"
    PAID = "paid", "Paid"
    CREDIT = "credit", "Credit"


class VatPeriod(models.Model):
    """
    One tax period of one branch in one ZATCA environment: a calendar month
    or quarter (the first one may start part-way through). Created by
    periods.ensure() from the first period up to the one containing today,
    with no gaps or overlaps. Dates are Saudi dates (ZatcaSettings.timezone),
    end inclusive.

    Sandbox and Simulation periods are practice periods: shown with a TEST
    label while in that environment, hidden once in Production, never
    deleted after they are filed.
    """

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="vat_periods")
    environment = models.CharField(max_length=16, choices=Environment.CHOICES)
    frequency = models.CharField(max_length=10, choices=VatFilingFrequency.choices)
    start_date = models.DateField()
    end_date = models.DateField()
    # Last day of the month after the period: stored, so a later rule
    # change never moves the due date of a period already shown.
    due_date = models.DateField()
    status = models.CharField(max_length=10, choices=PeriodStatus.choices, default=PeriodStatus.OPEN)
    # True once filed: nothing dated inside may change without an owner override.
    locked = models.BooleanField(default=False)
    # Owner-entered (adjustments.py), logged, fixed once the period is filed.
    # A credit brought from before this system: first period only.
    opening_credit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    opening_credit_reason = models.TextField(blank=True)
    # Box 14: corrections of earlier periods, within SAR ±5,000. Positive
    # adds VAT due, negative lowers it.
    correction = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    correction_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_date"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "environment", "start_date"],
                                    name="vat_period_unique_start"),
            models.CheckConstraint(condition=models.Q(end_date__gte=models.F("start_date")),
                                   name="vat_period_end_after_start"),
        ]

    def __str__(self):
        return f"{self.start_date} – {self.end_date} ({self.frequency}, {self.environment})"


class VatAuditLog(models.Model):
    """Every VAT action: settings changed, periods built, and (later)
    filings, payments, refunds, overrides and exports. Never edited or
    deleted. `data` holds the before/after values of what changed."""

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="vat_audit_logs")
    # SET_NULL only for placeholder periods replaced by rebuild(); `data`
    # keeps their dates.
    period = models.ForeignKey(VatPeriod, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="audit_logs")
    actor = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="vat_actions")
    action = models.CharField(max_length=40)
    summary = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.action} at {self.created_at:%Y-%m-%d %H:%M}"


class CreditTreatment(models.TextChoices):
    CARRY_FORWARD = "carry_forward", "Carry forward"
    REFUND_REQUESTED = "refund_requested", "Refund requested"


class VatFiling(models.Model):
    """
    A VAT return the owner filed on ZATCA's portal, recorded here: when, the
    net VAT ZATCA confirmed, and a snapshot of every figure this system had
    worked out at that moment (`calculated`, from figures.compute) with the
    IDs of the documents it counted. Filing locks the period.

    Normally one per period. An amended return adds a new row that
    `supersedes` the old one, which keeps is_current = False. Nothing is
    ever overwritten or deleted.
    """

    period = models.ForeignKey(VatPeriod, on_delete=models.PROTECT, related_name="filings")
    filed_on = models.DateField()
    # Net VAT due as ZATCA confirmed it (box 16 on the portal). Negative = credit.
    zatca_amount = models.DecimalField(max_digits=12, decimal_places=2)
    calculated = models.JSONField(encoder=DjangoJSONEncoder)
    calculated_net_due = models.DecimalField(max_digits=12, decimal_places=2)
    # zatca_amount - calculated_net_due; a reason is required when not 0.
    difference = models.DecimalField(max_digits=12, decimal_places=2)
    difference_reason = models.TextField(blank=True)
    # Only when zatca_amount < 0; blank otherwise.
    credit_treatment = models.CharField(max_length=20, choices=CreditTreatment.choices, blank=True)
    attachment_url = models.CharField(max_length=500, blank=True)
    attachment_name = models.CharField(max_length=255, blank=True)
    # Open checklist warnings the owner filed anyway.
    warnings = models.JSONField(default=list, blank=True)
    supersedes = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="superseded_by")
    is_current = models.BooleanField(default=True)
    # Why the return was amended; set on a filing that supersedes another.
    amendment_reason = models.TextField(blank=True)
    filed_by = models.ForeignKey(Staff, on_delete=models.PROTECT, related_name="vat_filings")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["period"], condition=models.Q(is_current=True),
                                    name="vat_filing_one_current_per_period"),
        ]

    def __str__(self):
        return f"Filing {self.filed_on} ({self.zatca_amount})"


class VatLockOverride(models.Model):
    """A change to a supplier document or expense dated in a filed (locked)
    period that the owner pushed through, with the reason and the stored
    figures before and after. Written in the same transaction as the change."""

    ACTIONS = [("create", "Create"), ("update", "Update"), ("delete", "Delete"), ("void", "Void")]

    period = models.ForeignKey(VatPeriod, on_delete=models.PROTECT, related_name="lock_overrides")
    action = models.CharField(max_length=10, choices=ACTIONS)
    object_type = models.CharField(max_length=60)
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    before = models.JSONField(encoder=DjangoJSONEncoder, null=True, blank=True)
    after = models.JSONField(encoder=DjangoJSONEncoder, null=True, blank=True)
    reason = models.TextField()
    actor = models.ForeignKey(Staff, on_delete=models.PROTECT, related_name="vat_lock_overrides")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.action} {self.object_type} {self.object_id} (override)"


class VoidableRecord(models.Model):
    """A money record that is voided, never deleted: a wrong one stays
    listed (struck through) with who voided it and why."""

    voided = models.BooleanField(default=False)
    voided_reason = models.TextField(blank=True)
    voided_by = models.ForeignKey(Staff, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    voided_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(Staff, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class PaymentSource(models.TextChoices):
    MANUAL = "manual", "Entered by hand"
    BANK_FEED = "bank_feed", "Bank feed"


class VatPayment(VoidableRecord):
    """A SADAD payment of a filed period's VAT. Several may add up to the
    amount due. `source` and `bank_transaction_id` are for a bank feed
    later (its dedupe key); today every payment is entered by hand."""

    period = models.ForeignKey(VatPeriod, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_on = models.DateField()
    sadad_reference = models.CharField(max_length=64)
    receipt_url = models.CharField(max_length=500, blank=True)
    receipt_name = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=10, choices=PaymentSource.choices, default=PaymentSource.MANUAL)
    bank_transaction_id = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["paid_on", "id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="vat_payment_amount_positive"),
            models.UniqueConstraint(fields=["bank_transaction_id"], condition=~models.Q(bank_transaction_id=""),
                                    name="vat_payment_unique_bank_transaction"),
        ]

    def __str__(self):
        return f"Payment {self.amount} on {self.paid_on}"


class VatRefund(VoidableRecord):
    """Money ZATCA paid back for a period filed with a credit and
    credit_treatment = refund_requested."""

    period = models.ForeignKey(VatPeriod, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    received_on = models.DateField()
    reference = models.CharField(max_length=64)
    attachment_url = models.CharField(max_length=500, blank=True)
    attachment_name = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["received_on", "id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="vat_refund_amount_positive"),
        ]

    def __str__(self):
        return f"Refund {self.amount} on {self.received_on}"


class ArchiveExportStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Being prepared"
    READY = "ready", "Ready to download"
    FAILED = "failed", "Failed"
    # Ready ZIPs are kept 7 days; the file is then removed, the row kept.
    EXPIRED = "expired", "Expired"


class VatArchiveExport(models.Model):
    """
    A ZIP of the invoice archive (plan §11): the sales and/or purchase
    documents matching `filters`, with PDFs, XMLs and an index.csv. Built in
    the background (archive_zip.py). The filter is stored, not a list of
    IDs; the matching IDs are frozen in `document_ids` when the build
    starts. The ZIP is a copy: the documents are never touched.
    """

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="vat_archive_exports")
    environment = models.CharField(max_length=16, choices=Environment.CHOICES)
    # "sales", "purchases" or both.
    sides = models.JSONField(default=list)
    # Parsed filters as JSON (archive.as_json): the period as its id.
    filters = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    period = models.ForeignKey(VatPeriod, on_delete=models.PROTECT, null=True, blank=True,
                               related_name="archive_exports")
    status = models.CharField(max_length=10, choices=ArchiveExportStatus.choices, default=ArchiveExportStatus.QUEUED)
    document_ids = models.JSONField(default=dict, blank=True)
    sales_count = models.PositiveIntegerField(default=0)
    purchases_count = models.PositiveIntegerField(default=0)
    # Files listed as "missing" in index.csv (no PDF, a failed download...).
    missing_count = models.PositiveIntegerField(default=0)
    file_name = models.CharField(max_length=255, blank=True)
    # Relative to settings.VAT_ARCHIVE_DIR (a private folder, never public media).
    file_path = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    error = models.TextField(blank=True)
    requested_by = models.ForeignKey(Staff, on_delete=models.PROTECT, related_name="vat_archive_exports")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["status", "created_at"], name="vat_archive_status_created")]

    def __str__(self):
        return f"Archive export {self.pk} ({self.status})"
