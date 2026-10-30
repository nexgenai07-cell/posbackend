from django.db import models

from branches.models import Branch
from common.models import BaseModel
from vat_returns import locks


class Supplier(BaseModel):
    """Built in Phase 8 as an InventoryItem.supplier FK dependency, endpoints
    added here in Phase 9."""

    name_en = models.CharField(max_length=255)
    name_ar = models.CharField(max_length=255)
    contact_info = models.CharField(max_length=255, blank=True)

    # VAT details, for checking its tax invoices and the input VAT report.
    # A supplier that isn't VAT-registered can't issue a tax invoice: its
    # purchases carry no VAT and none is claimed.
    vat_registered = models.BooleanField(default=True)
    vat_number = models.CharField(max_length=15, blank=True)
    crn = models.CharField(max_length=10, blank=True)
    address = models.CharField(max_length=500, blank=True)

    def __str__(self):
        return self.name_en


class PurchaseStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ORDERED = "ordered", "Ordered"
    RECEIVED = "received", "Received"


class Purchase(BaseModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="purchases")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchases")
    status = models.CharField(max_length=16, choices=PurchaseStatus.choices, default=PurchaseStatus.ORDERED)
    ordered_at = models.DateTimeField(auto_now_add=True)
    received_at = models.DateTimeField(null=True, blank=True)

    class Meta(BaseModel.Meta):
        indexes = [
            # The purchases list (by order date) and reports (by receive date).
            models.Index(fields=["branch", "ordered_at"], name="purchase_branch_ordered"),
            models.Index(fields=["branch", "received_at"], name="purchase_branch_received"),
        ]

    def __str__(self):
        return f"Purchase #{self.pk} ({self.status})"


class PurchaseItem(BaseModel):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    # String reference, not a direct import: inventory.models already imports
    # purchasing.models.Supplier, so a direct import here would be circular.
    inventory_item = models.ForeignKey(
        "inventory.InventoryItem", on_delete=models.PROTECT, related_name="purchase_items"
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.quantity} x {self.inventory_item.name} @ {self.unit_cost}"


class SupplierDocumentKind(models.TextChoices):
    INVOICE = "invoice", "Tax invoice"
    CREDIT_NOTE = "credit_note", "Credit note"


class SupplierVatCategory(models.TextChoices):
    """Which purchases box of the VAT return a document belongs to."""

    STANDARD = "standard", "Standard rated"
    ZERO = "zero", "Zero rated"
    EXEMPT = "exempt", "Exempt"


class ClaimDateMixin:
    """Which VAT period claims a tax document (supplier document or
    expense): `claim_date`. Needs `tax_branch` and `issue_date`."""

    # Set (not stored) by the owner's "claim in the filed period": the
    # document is claimed on its own date even in a filed period, which
    # then needs a lock override.
    keep_in_filed_period = False

    def _claim_date(self):
        """Its own date, unless that is in a filed VAT period: then the
        oldest open period's first day (VAT plan §7.2), since input VAT may
        be claimed in a later return. A save that doesn't change the date
        keeps the stored claim date, so a re-save never moves a document
        between periods."""
        if self.keep_in_filed_period:
            return self.issue_date
        if self.pk:
            stored = type(self).objects.filter(pk=self.pk).values_list("issue_date", "claim_date").first()
            if stored is not None and stored[0] == self.issue_date:
                return stored[1]
        return locks.claim_date_for(self.tax_branch, self.issue_date)

    def _copy_supplier(self, supplier):
        """The supplier's VAT details as they are now, kept on the document."""
        self.supplier_vat_registered = supplier.vat_registered
        self.supplier_vat_number = supplier.vat_number
        self.supplier_crn = supplier.crn
        if not self.vat and self.vat_category == SupplierVatCategory.STANDARD:
            self.vat_category = SupplierVatCategory.ZERO


class SupplierDocument(ClaimDateMixin, models.Model):
    """
    A tax document the SUPPLIER issued for a purchase: its tax invoice (at
    most one per purchase) or a credit note for a return. Recorded here for
    the input VAT report and the audit trail; nothing on the purchase side is
    ever sent to ZATCA — the supplier reports its own documents.

    Amounts are as printed on the supplier's document. `qr_check` is what
    zatca.qr made of the pasted/scanned QR when it was saved (see
    purchasing/vat.py): the decoded fields and any mismatch with what was
    entered.

    The supplier's VAT details are copied onto the document every time it is
    saved (the `supplier_*` fields), and `claimable_vat` is worked out from
    that copy, so the input VAT is a stored figure: editing a supplier later
    changes its documents only through refresh_supplier_documents().

    Not a BaseModel: a tax record, deleted only for a keying mistake.
    """

    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="supplier_documents")
    kind = models.CharField(max_length=12, choices=SupplierDocumentKind.choices)
    number = models.CharField(max_length=64)
    issue_date = models.DateField()
    net = models.DecimalField(max_digits=12, decimal_places=2)
    vat = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    # VAT 0 defaults to zero rated (save()); the owner can mark it exempt.
    vat_category = models.CharField(max_length=10, choices=SupplierVatCategory.choices,
                                    default=SupplierVatCategory.STANDARD)
    supplier_vat_registered = models.BooleanField(default=True)
    supplier_vat_number = models.CharField(max_length=15, blank=True)
    supplier_crn = models.CharField(max_length=10, blank=True)
    # Signed: negative for a credit note, 0 when not claimable (purchasing/vat.py).
    claimable_vat = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # The date that decides which VAT period claims this document: its own
    # date, or for one dated in a filed period, the start of the oldest
    # open period (save(), _claim_date()). A date rather than a period
    # link, so it survives switching environment or frequency.
    claim_date = models.DateField()
    reason = models.CharField(max_length=255, blank=True)
    qr_text = models.TextField(blank=True)
    qr_check = models.JSONField(default=dict, blank=True)
    # The supplier's e-invoice: XML as text, PDF as an uploaded file's URL.
    xml = models.TextField(blank=True)
    pdf_url = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey("accounts.Staff", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["issue_date", "id"]
        constraints = [
            models.UniqueConstraint(fields=["purchase"], condition=models.Q(kind="invoice"),
                                    name="one_supplier_invoice_per_purchase"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.number} ({self.total})"

    # Written by save() on every save, whatever else update_fields names.
    DERIVED_FIELDS = ("vat_category", "supplier_vat_registered", "supplier_vat_number", "supplier_crn",
                      "claimable_vat", "claim_date")

    @property
    def tax_branch(self):
        return self.purchase.branch

    def save(self, *args, **kwargs):
        from .vat import claimable_vat

        self._copy_supplier(self.purchase.supplier)
        self.claimable_vat = claimable_vat(self)
        self.claim_date = self._claim_date()
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = {*kwargs["update_fields"], *self.DERIVED_FIELDS, "updated_at"}
        # A document in a filed VAT period changes only with an owner override.
        with locks.guard_tax_document(self, "update" if self.pk else "create"):
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with locks.guard_tax_document(self, "delete"):
            return super().delete(*args, **kwargs)


class ExpenseCategory(models.TextChoices):
    RENT = "rent", "Rent"
    UTILITIES = "utilities", "Utilities"
    EQUIPMENT = "equipment", "Equipment"
    MAINTENANCE = "maintenance", "Maintenance"
    SERVICES = "services", "Services"
    MARKETING = "marketing", "Marketing"
    OTHER = "other", "Other"


class NotClaimableReason(models.TextChoices):
    """Why an expense's VAT is recorded but not claimed (blocked input VAT).
    A starting list; the accountant confirms which apply (runbook A10)."""

    PERSONAL_USE = "personal_use", "Personal use"
    ENTERTAINMENT = "entertainment", "Entertainment / hospitality"
    GIFTS = "gifts", "Gifts"
    PASSENGER_CAR = "passenger_car", "Passenger car (non-commercial)"
    STAFF_BENEFITS = "staff_benefits", "Staff personal benefits"
    OTHER = "other", "Other"


class Expense(ClaimDateMixin, models.Model):
    """
    A business expense with VAT paid outside a purchase order (rent,
    utilities, equipment, maintenance, services, marketing): the supplier's
    tax invoice or credit note, keyed in from paper, PDF or photo. Input VAT
    like a SupplierDocument, with the same rules (purchasing/vat.py), the
    same supplier snapshot, claim date and period lock (VAT plan §12).

    "Not claimable" keeps the VAT recorded but claims none (blocked input
    VAT: personal use, entertainment, ...). `capital_asset` marks equipment
    for the accountant.

    Never deleted: a wrong expense is voided with a reason (purchasing/
    expenses.py), stays listed and stops counting.
    """

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="expenses")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="expenses")
    kind = models.CharField(max_length=12, choices=SupplierDocumentKind.choices, default=SupplierDocumentKind.INVOICE)
    number = models.CharField(max_length=64)
    issue_date = models.DateField()
    category = models.CharField(max_length=12, choices=ExpenseCategory.choices)
    description = models.CharField(max_length=255, blank=True)
    net = models.DecimalField(max_digits=12, decimal_places=2)
    vat = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    vat_category = models.CharField(max_length=10, choices=SupplierVatCategory.choices,
                                    default=SupplierVatCategory.STANDARD)
    supplier_vat_registered = models.BooleanField(default=True)
    supplier_vat_number = models.CharField(max_length=15, blank=True)
    supplier_crn = models.CharField(max_length=10, blank=True)
    not_claimable = models.BooleanField(default=False)
    not_claimable_reason = models.CharField(max_length=16, choices=NotClaimableReason.choices, blank=True)
    not_claimable_note = models.CharField(max_length=255, blank=True)
    # Signed: negative for a credit note; 0 when not claimable or voided.
    claimable_vat = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    capital_asset = models.BooleanField(default=False)
    claim_date = models.DateField()
    qr_text = models.TextField(blank=True)
    qr_check = models.JSONField(default=dict, blank=True)
    # The supplier's PDF or a photo of the paper invoice (Cloudinary URL).
    attachment_url = models.CharField(max_length=500, blank=True)
    attachment_name = models.CharField(max_length=255, blank=True)
    voided = models.BooleanField(default=False)
    voided_reason = models.TextField(blank=True)
    voided_by = models.ForeignKey("accounts.Staff", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    voided_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey("accounts.Staff", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-issue_date", "-id"]
        indexes = [models.Index(fields=["branch", "claim_date"], name="expense_branch_claim_date")]

    def __str__(self):
        return f"Expense {self.number} ({self.total})"

    DERIVED_FIELDS = SupplierDocument.DERIVED_FIELDS

    @property
    def tax_branch(self):
        return self.branch

    def save(self, *args, **kwargs):
        from .vat import claimable_vat

        self._copy_supplier(self.supplier)
        self.claimable_vat = claimable_vat(self)
        self.claim_date = self._claim_date()
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = {*kwargs["update_fields"], *self.DERIVED_FIELDS, "updated_at"}
        action = getattr(self, "_lock_action", None) or ("update" if self.pk else "create")
        with locks.guard_tax_document(self, action):
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise models.ProtectedError("Expenses are voided, never deleted.", {self})


def refresh_supplier_documents(supplier):
    """Re-copy a supplier's edited VAT details onto its documents and
    (non-voided) expenses, so fixing a missing VAT number makes their VAT
    claimable. Documents in a filed VAT period are left as they were filed.
    Returns (refreshed, left as filed)."""
    documents = [
        *SupplierDocument.objects.filter(purchase__supplier=supplier).select_related("purchase__supplier",
                                                                                     "purchase__branch"),
        *Expense.objects.filter(supplier=supplier, voided=False).select_related("supplier", "branch"),
    ]
    refreshed = kept = 0
    for document in documents:
        try:
            document.save(update_fields=[])
        except locks.PeriodLocked:
            kept += 1
            continue
        refreshed += 1
    return refreshed, kept
