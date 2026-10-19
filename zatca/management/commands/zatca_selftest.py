"""
Milestone check against the branch's active device, all signed in Python:

  1. every compliance document type (/compliance/invoices)
  2. the split-line and PayableRoundingAmount cases on their own
  3. one simplified invoice issued into the chain and REPORTED, and one
     standard invoice issued into the chain and CLEARED (skip: --compliance-only)

Step 3 writes two real sandbox documents (source "selftest") and advances
the device's ICV/PIH, as any sale would. Refused outside Sandbox.
"""

from django.core.management.base import BaseCommand, CommandError

from branches.models import Branch
from zatca import compliance, documents, ubl
from zatca.client import ZatcaAuthError, ZatcaUnavailable
from zatca.environments import Environment
from zatca.models import ZatcaAuditLog, ZatcaSettings

from ._output import check_lines, document_lines


class Command(BaseCommand):
    help = "Run ZATCA compliance checks and one report + one clearance with the branch's active Sandbox device."

    def add_arguments(self, parser):
        parser.add_argument("--branch", type=int, required=True, help="Branch id")
        parser.add_argument("--compliance-only", action="store_true", help="Skip the reported/cleared chain documents")

    def handle(self, *args, **options):
        branch = Branch.objects.filter(pk=options["branch"]).first()
        if branch is None:
            raise CommandError(f"No branch with id {options['branch']}.")
        settings = ZatcaSettings.for_branch(branch)
        if settings.environment != Environment.SANDBOX:
            raise CommandError("zatca_selftest only runs in Sandbox.")
        device = settings.active_device()
        if device is None:
            raise CommandError(f"{branch} has no active device.")
        self.stdout.write(f"Device #{device.pk} {device.common_name} (Sandbox), seller VAT {settings.vat_number}")

        failures = 0
        checks = compliance.standard_checks(device.invoice_types or settings.invoice_types) + [
            (f"Simplified invoice — {sample[0]}", True, ubl.DocumentKind.INVOICE, sample)
            for sample in (compliance.SPLIT_10, compliance.SPLIT_13_99, compliance.ROUNDING)
        ] + [("Standard invoice — " + compliance.ROUNDING[0], False, ubl.DocumentKind.INVOICE, compliance.ROUNDING)]
        self.stdout.write(f"\n1-2. Compliance checks (/compliance/invoices), {len(checks)} documents:")
        try:
            results = compliance.run(device, settings, checks)
        except (ZatcaUnavailable, ZatcaAuthError) as exc:
            raise CommandError(str(exc)) from None
        for result in results:
            failures += not result.passed
            for line in check_lines(result):
                self.stdout.write(line)

        if not options["compliance_only"]:
            self.stdout.write("\n3. Issued into the chain and sent with the production CSID:")
            for simplified, sample in ((True, compliance.MIXED), (False, compliance.ROUNDING)):
                totals, names = compliance.sale(sample[1], settings.default_vat_rate)
                document = documents.issue(
                    branch, kind=ubl.DocumentKind.INVOICE, simplified=simplified, vat=totals, names=names,
                    buyer=None if simplified else compliance.SAMPLE_BUYER, source="selftest",
                )
                document = documents.submit(document)
                failures += document.status not in ("reported", "cleared")
                for line in document_lines(document):
                    self.stdout.write(line)
                self.stdout.write(f"       {sample[0]}; QR {len(document.qr)} chars"
                                  + ("; QR taken from ZATCA's cleared XML" if document.cleared_xml else ""))

        ZatcaAuditLog.objects.create(branch=branch, device=device, actor_label="manage.py zatca_selftest",
                                     action="selftest", ok=not failures,
                                     summary=f"Self-test: {failures} failure(s).")
        if failures:
            raise CommandError(f"{failures} document(s) not accepted.")
        self.stdout.write(self.style.SUCCESS("\nAll documents accepted by ZATCA."))
