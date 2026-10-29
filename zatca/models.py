from decimal import Decimal

from django.db import models
from django.utils import timezone

from accounts.models import Staff
from branches.models import Branch

from . import crypto
from .environments import SANDBOX_IDENTITY, Environment, profile


class InvoiceTypes(models.TextChoices):
    """The CSR `title` field: which invoice kinds this branch's device issues."""

    STANDARD = "1000", "Standard (B2B) only"
    SIMPLIFIED = "0100", "Simplified (B2C) only"
    BOTH = "1100", "Standard and simplified"


class SellerIdScheme(models.TextChoices):
    """Seller identifier schemes ZATCA accepts besides the VAT number (BR-KSA-08)."""

    CRN = "CRN", "Commercial registration number"
    MOM = "MOM", "Momra license"
    MLS = "MLS", "MLSD license"
    SAG = "SAG", "Sagia license"
    SEVEN_HUNDRED = "700", "700 number"
    OTH = "OTH", "Other ID"


class VatFilingFrequency(models.TextChoices):
    """How often the VAT return is filed: quarterly by default (turnover up
    to SAR 40 million), monthly above that or by choice."""

    QUARTERLY = "quarterly", "Quarterly"
    MONTHLY = "monthly", "Monthly"


class ZatcaSettings(models.Model):
    """
    One branch's ZATCA identity: who the seller is on every invoice and which
    environment it talks to. Edited by the owner on the ZATCA Business details
    page; nothing here is hardcoded anywhere else.

    Not a BaseModel: this is the source of every tax invoice's seller block and
    must never be soft-deleted out from under the documents issued with it.

    `environment` is never written by the settings API. It changes only through
    the go-live wizard, which also creates a new device, so a certificate and
    its ICV/PIH chain can never cross from one environment to another.
    """

    branch = models.OneToOneField(Branch, on_delete=models.CASCADE, related_name="zatca_settings")
    environment = models.CharField(max_length=16, choices=Environment.CHOICES, default=Environment.SANDBOX)
    # Off = the POS behaves exactly as it did before ZATCA existed.
    enabled = models.BooleanField(default=False)
    # When it was last switched on: the retry job's backlog only invoices
    # orders paid after this, never sales from before ZATCA was on.
    enabled_at = models.DateTimeField(null=True, blank=True)

    seller_name_en = models.CharField(max_length=255)
    seller_name_ar = models.CharField(max_length=255)
    vat_number = models.CharField(max_length=15)
    other_id = models.CharField(max_length=40)
    other_id_scheme = models.CharField(max_length=3, choices=SellerIdScheme.choices, default=SellerIdScheme.CRN)

    # The Saudi national address, field for field as the invoice carries it.
    street = models.CharField(max_length=255)
    building_no = models.CharField(max_length=4)
    additional_no = models.CharField(max_length=4, blank=True)
    district = models.CharField(max_length=255)
    city = models.CharField(max_length=255)
    postal_code = models.CharField(max_length=5)
    country = models.CharField(max_length=2, default="SA")

    # CSR organisation unit: the branch name, or the 10-digit TIN for a VAT group.
    org_unit = models.CharField(max_length=255)
    business_category = models.CharField(max_length=255)
    invoice_types = models.CharField(max_length=4, choices=InvoiceTypes.choices, default=InvoiceTypes.BOTH)
    default_vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("15.00"))
    prices_include_vat = models.BooleanField(default=True)
    # Invoice IssueDate/IssueTime are Saudi time whatever Branch.timezone says.
    timezone = models.CharField(max_length=64, default="Asia/Riyadh")
    # Printed at the bottom of every tax invoice and credit note.
    receipt_footer_en = models.CharField(max_length=255, blank=True)
    receipt_footer_ar = models.CharField(max_length=255, blank=True)

    # VAT returns (vat_returns app). Edited through /api/vat/settings/, which
    # rebuilds the periods; the first period starts on the later of the two
    # dates, and there are no periods until at least one is set.
    vat_filing_frequency = models.CharField(max_length=10, choices=VatFilingFrequency.choices,
                                            default=VatFilingFrequency.QUARTERLY)
    vat_registration_date = models.DateField(null=True, blank=True)
    vat_tracking_start = models.DateField(null=True, blank=True)
    # A frequency change once a period is filed (M9): vat_filing_frequency
    # applies to periods starting on or after this date (a quarter start);
    # earlier ones keep the frequency they were created with. None: it
    # applies from the first period.
    vat_frequency_effective_from = models.DateField(null=True, blank=True)
    vat_reminder_days = models.PositiveSmallIntegerField(default=14)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Bound into the device certificate: frozen while a live device exists.
    IDENTITY_FIELDS = ("vat_number", "other_id", "other_id_scheme")

    class Meta:
        verbose_name = "ZATCA settings"
        verbose_name_plural = "ZATCA settings"

    def __str__(self):
        return f"ZATCA settings for {self.branch} ({self.environment})"

    @classmethod
    def for_branch(cls, branch):
        """This branch's settings, created on first visit pre-filled with the
        sandbox test identity and the branch's own name."""
        settings, _created = cls.objects.get_or_create(branch=branch, defaults=sandbox_defaults(branch))
        return settings

    def locked_fields(self):
        """Fields the owner cannot edit right now, because a certificate is
        bound to them: once a Simulation or Production device is Active."""
        return list(self.IDENTITY_FIELDS) if self.has_live_device() else []

    def has_live_device(self):
        return self.branch.zatca_devices.filter(
            is_active=True,
            status=DeviceStatus.ACTIVE,
            environment__in=(Environment.SIMULATION, Environment.PRODUCTION),
        ).exists()

    def active_device(self):
        """The device this branch issues with in its current environment, or None."""
        return self.branch.zatca_devices.filter(is_active=True, environment=self.environment).first()


def sandbox_defaults(branch):
    """The working sandbox identity, with the restaurant's own name where it has one."""
    defaults = dict(SANDBOX_IDENTITY)
    if branch.name_en:
        defaults["seller_name_en"] = branch.name_en
    if branch.name_ar:
        defaults["seller_name_ar"] = branch.name_ar
    defaults["org_unit"] = branch.name_en or branch.name_ar or defaults["seller_name_en"]
    defaults["environment"] = Environment.SANDBOX
    return defaults


# PIH of a device's very first document: base64 of the hex SHA-256 of "0".
INITIAL_PIH = "NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOQ=="


class DeviceStatus(models.TextChoices):
    NOT_STARTED = "not_started", "Not started"
    KEYS_READY = "keys_ready", "Key and CSR ready"
    COMPLIANCE_CSID = "compliance_csid", "Compliance CSID issued"
    COMPLIANCE_PASSED = "compliance_passed", "Compliance checks passed"
    # Fully onboarded (production CSID issued) but not the branch's device yet.
    READY = "ready", "Ready, not in use"
    ACTIVE = "active", "Active"
    NEEDS_ATTENTION = "needs_attention", "Needs attention"


class ZatcaDevice(models.Model):
    """
    One EGS unit: a certificate, its private key and its own ICV/PIH chain.

    A device belongs to exactly one environment for life. Going live (or
    importing credentials) always creates a new device and archives the old
    one read-only, so a sandbox certificate or chain can never carry on into
    Simulation or Production. At most one device per branch is active.

    Secrets are stored only as Fernet ciphertext (the *_enc columns) and are
    decrypted at the moment of use through get_secret(); nothing that lists
    or displays devices ever touches them. Certificates and the CSR are
    public and stored as plain base64 DER / PEM.
    """

    SECRETS = (
        "private_key",
        "compliance_token",
        "compliance_secret",
        "compliance_request_id",
        "production_token",
        "production_secret",
    )

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="zatca_devices")
    environment = models.CharField(max_length=16, choices=Environment.CHOICES)
    is_active = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=DeviceStatus.choices, default=DeviceStatus.NOT_STARTED)

    egs_serial = models.CharField(max_length=255, blank=True)
    common_name = models.CharField(max_length=255, blank=True)
    location = models.CharField(max_length=255, blank=True)
    invoice_types = models.CharField(max_length=4, blank=True)

    csr = models.TextField(blank=True)
    compliance_cert = models.TextField(blank=True)
    production_cert = models.TextField(blank=True)

    private_key_enc = models.TextField(blank=True)
    compliance_token_enc = models.TextField(blank=True)
    compliance_secret_enc = models.TextField(blank=True)
    compliance_request_id_enc = models.TextField(blank=True)
    production_token_enc = models.TextField(blank=True)
    production_secret_enc = models.TextField(blank=True)
    # {secret name: ISO time it was stored} — what the status page shows.
    secrets_set_at = models.JSONField(default=dict, blank=True)

    # The chain. Advanced only together with a signed document, under
    # select_for_update() on this row (documents.issue).
    last_icv = models.PositiveIntegerField(default=0)
    last_hash = models.CharField(max_length=88, default=INITIAL_PIH)
    invoice_seq = models.PositiveIntegerField(default=0)
    credit_seq = models.PositiveIntegerField(default=0)
    debit_seq = models.PositiveIntegerField(default=0)

    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_active", "-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch"], condition=models.Q(is_active=True), name="one_active_zatca_device_per_branch",
            ),
        ]

    def __str__(self):
        return f"{self.common_name or 'ZATCA device'} ({self.environment}, {self.status})"

    def set_secret(self, name, value):
        if name not in self.SECRETS:
            raise ValueError(f"Unknown ZATCA secret {name!r}")
        setattr(self, f"{name}_enc", crypto.encrypt(value))
        self.secrets_set_at = {**self.secrets_set_at, name: timezone.now().isoformat() if value else None}

    def get_secret(self, name):
        if name not in self.SECRETS:
            raise ValueError(f"Unknown ZATCA secret {name!r}")
        return crypto.decrypt(getattr(self, f"{name}_enc"))

    def has_secret(self, name):
        return bool(getattr(self, f"{name}_enc"))

    @property
    def signing_cert(self):
        """The certificate invoices are signed with: the compliance one in
        Sandbox (its production CSID belongs to a different key), the
        production one everywhere else."""
        if profile(self.environment).signs_with_compliance_cert:
            return self.compliance_cert
        return self.production_cert

    def compliance_auth(self):
        return self.get_secret("compliance_token"), self.get_secret("compliance_secret")

    def production_auth(self):
        return self.get_secret("production_token"), self.get_secret("production_secret")

    def archive(self):
        self.is_active = False
        self.archived_at = timezone.now()
        self.save(update_fields=["is_active", "archived_at", "updated_at"])


class ZatcaAuditLog(models.Model):
    """Who did what to a branch's ZATCA setup: every onboarding step, import
    and go-live, successful or not. Summaries never contain secrets."""

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="zatca_audit_logs")
    device = models.ForeignKey(ZatcaDevice, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs")
    actor = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="zatca_actions")
    # For actions with no staff member behind them, e.g. "manage.py zatca_import".
    actor_label = models.CharField(max_length=120, blank=True)
    action = models.CharField(max_length=40)
    ok = models.BooleanField()
    summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.action} ({'ok' if self.ok else 'failed'}) at {self.created_at:%Y-%m-%d %H:%M}"


class BusinessCustomer(models.Model):
    """
    A company buyer for standard (B2B) tax invoices, picked or entered on
    the payment screen ("Tax invoice for company"). Reused by VAT number.
    Each document also keeps a snapshot, so editing a customer never
    rewrites an issued invoice.
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="zatca_business_customers")
    legal_name = models.CharField(max_length=255)
    vat_number = models.CharField(max_length=15)
    crn = models.CharField(max_length=10, blank=True)
    street = models.CharField(max_length=255)
    building_no = models.CharField(max_length=4)
    additional_no = models.CharField(max_length=4, blank=True)
    district = models.CharField(max_length=255)
    city = models.CharField(max_length=255)
    postal_code = models.CharField(max_length=5)
    country = models.CharField(max_length=2, default="SA")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["legal_name"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "vat_number"], name="zatca_business_customer_unique_vat"),
        ]

    def __str__(self):
        return f"{self.legal_name} ({self.vat_number})"

    def snapshot(self):
        return {field: getattr(self, field) for field in (
            "legal_name", "vat_number", "crn", "street", "building_no", "additional_no", "district", "city",
            "postal_code", "country",
        )}


class DocumentStatus(models.TextChoices):
    # Signed and in the chain; ZATCA hasn't accepted it yet (not sent, no
    # answer, or unreachable). The retry job keeps sending it unchanged.
    PENDING = "pending", "Pending"
    REPORTED = "reported", "Reported"
    CLEARED = "cleared", "Cleared"
    # ZATCA refused it. It stays in the chain: its ICV is never reused.
    REJECTED = "rejected", "Rejected"


class ZatcaDocument(models.Model):
    """
    One signed invoice, credit note or debit note: a link in its device's
    ICV/PIH chain, created and chained in one transaction by documents.issue.

    Never edited after signing except for ZATCA's answer. Never deleted:
    not a BaseModel, and PROTECT towards the device.
    """

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="zatca_documents")
    device = models.ForeignKey(ZatcaDevice, on_delete=models.PROTECT, related_name="documents")
    environment = models.CharField(max_length=16, choices=Environment.CHOICES)
    kind = models.CharField(max_length=12, choices=[
        ("invoice", "Tax invoice"), ("credit_note", "Credit note"), ("debit_note", "Debit note"),
    ])
    simplified = models.BooleanField()
    number = models.CharField(max_length=40)
    uuid = models.CharField(max_length=36, unique=True)
    icv = models.PositiveIntegerField()
    pih = models.CharField(max_length=88)
    invoice_hash = models.CharField(max_length=88)
    issued_at = models.DateTimeField()
    total = models.DecimalField(max_digits=12, decimal_places=2)
    vat_total = models.DecimalField(max_digits=12, decimal_places=2)
    # The VAT return's source figures, exactly as the signed XML states them
    # (see zatca/breakdown.py): TaxExclusiveAmount, and per VAT category the
    # taxable amount and VAT. Positive for every kind. Null only on an old
    # document the backfill couldn't read; the VAT dashboard flags those.
    net_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    vat_breakdown = models.JSONField(null=True, blank=True)
    original = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="adjustments")
    reason = models.CharField(max_length=255, blank=True)
    # Where it came from: "pos", or "selftest" for the milestone checks.
    source = models.CharField(max_length=20, default="pos")
    buyer = models.ForeignKey(BusinessCustomer, on_delete=models.PROTECT, null=True, blank=True,
                              related_name="documents")
    # Everything the printed receipt shows that the XML doesn't carry, frozen
    # at issue: seller and buyer as they were, the dishes as charged (VAT
    # inclusive, before any line split), order type, table, payments, tip,
    # and the sale lines per order a credit note is built from.
    receipt = models.JSONField(default=dict, blank=True)
    # Prints so far: every print after the first says "Copy / نسخة".
    print_count = models.PositiveIntegerField(default=0)

    signed_xml = models.TextField()
    # Standard invoices: ZATCA's cleared XML, whose QR the receipt prints.
    cleared_xml = models.TextField(blank=True)
    qr = models.TextField()

    status = models.CharField(max_length=10, choices=DocumentStatus.choices, default=DocumentStatus.PENDING)
    zatca_status = models.CharField(max_length=10, blank=True)  # PASS / WARNING / ERROR
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    errors = models.JSONField(default=list, blank=True)
    response = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-icv"]
        constraints = [
            models.UniqueConstraint(fields=["device", "icv"], name="zatca_document_unique_icv"),
            models.UniqueConstraint(fields=["device", "number"], name="zatca_document_unique_number"),
        ]
        indexes = [models.Index(fields=["status", "last_attempt_at"])]

    def __str__(self):
        return f"{self.number} (ICV {self.icv}, {self.status})"

    @property
    def is_final(self):
        return self.status != DocumentStatus.PENDING


class ZatcaDocumentOrder(models.Model):
    """Which orders a document covers. An order gets at most one invoice,
    one credit note per partial refund, one for its cancellation (whatever
    the refunds left), and any number of debit notes. The unique constraints
    are what stop the payment hook and the retry job from ever issuing the
    same document twice."""

    document = models.ForeignKey(ZatcaDocument, on_delete=models.PROTECT, related_name="order_links")
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="zatca_links")
    kind = models.CharField(max_length=12)
    # Set on a partial refund's credit note; empty on the cancellation one.
    refund = models.ForeignKey("orders.OrderRefund", on_delete=models.PROTECT, null=True, blank=True,
                               related_name="zatca_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order"], condition=models.Q(kind="invoice"),
                                    name="zatca_one_invoice_per_order"),
            models.UniqueConstraint(fields=["order"], condition=models.Q(kind="credit_note", refund__isnull=True),
                                    name="zatca_one_cancellation_credit_per_order"),
            models.UniqueConstraint(fields=["refund"], condition=models.Q(refund__isnull=False),
                                    name="zatca_one_credit_note_per_refund"),
        ]


class ZatcaOrderBuyer(models.Model):
    """The company an order's tax invoice is for, chosen before payment.
    No row = a simplified (B2C) invoice."""

    order = models.OneToOneField("orders.Order", on_delete=models.CASCADE, related_name="zatca_buyer")
    buyer = models.ForeignKey(BusinessCustomer, on_delete=models.PROTECT, related_name="pending_orders")
    set_by = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)


class GoLiveStatus(models.TextChoices):
    OPEN = "open", "In progress"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class ZatcaGoLive(models.Model):
    """
    One run of the go-live wizard: onboarding a new device for an
    environment, step by step, resumable between visits.

    `details` holds the business details being onboarded. They go into the
    new device's CSR and compliance checks, but reach ZatcaSettings only on
    activation, so the device issuing today keeps its own identity until
    the very moment it is replaced. Practice runs (Sandbox) may finish
    without activating, leaving their device Ready and unused.

    Never holds an OTP or a secret.
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="zatca_go_lives")
    environment = models.CharField(max_length=16, choices=Environment.CHOICES)
    status = models.CharField(max_length=10, choices=GoLiveStatus.choices, default=GoLiveStatus.OPEN)
    details = models.JSONField(default=dict, blank=True)
    device = models.ForeignKey(ZatcaDevice, on_delete=models.SET_NULL, null=True, blank=True, related_name="go_lives")
    # The last compliance-check run, as shown on the wizard (no XML).
    checks = models.JSONField(default=list, blank=True)
    # The owner confirmed environments.LEGAL_WARNING (Production only).
    legal_confirmed_at = models.DateTimeField(null=True, blank=True)
    activated = models.BooleanField(default=False)
    started_by = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["branch"], condition=models.Q(status="open"),
                                    name="one_open_zatca_go_live_per_branch"),
        ]

    def __str__(self):
        return f"Go-live to {self.environment} ({self.status})"

    @property
    def practice(self):
        return self.environment == Environment.SANDBOX
