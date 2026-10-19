from django.db import models

from branches.models import Branch
from common.models import BaseModel


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


class SupplierDocument(models.Model):
    """
    A tax document the SUPPLIER issued for a purchase: its tax invoice (at
    most one per purchase) or a credit note for a return. Recorded here for
    the input VAT report and the audit trail; nothing on the purchase side is
    ever sent to ZATCA — the supplier reports its own documents.

    Amounts are as printed on the supplier's document. `qr_check` is what
    zatca.qr made of the pasted/scanned QR when it was saved (see
    purchasing/vat.py): the decoded fields and any mismatch with what was
    entered.

    Not a BaseModel: a tax record, deleted only for a keying mistake.
    """

    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="supplier_documents")
    kind = models.CharField(max_length=12, choices=SupplierDocumentKind.choices)
    number = models.CharField(max_length=64)
    issue_date = models.DateField()
    net = models.DecimalField(max_digits=12, decimal_places=2)
    vat = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
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
