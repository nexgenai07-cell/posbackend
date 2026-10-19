from django.core.management.base import BaseCommand, CommandError

from branches.models import Branch
from zatca.environments import Environment, profile
from zatca.models import ZatcaSettings
from zatca.onboarding import OnboardingError, onboard

from ._output import check_lines


class Command(BaseCommand):
    help = (
        "Onboard a new ZATCA device for a branch in its current environment: key + CSR, compliance CSID, "
        "all compliance checks, production CSID. The branch's current device keeps working unless --replace."
    )

    def add_arguments(self, parser):
        parser.add_argument("--branch", type=int, required=True, help="Branch id")
        parser.add_argument("--otp", default="", help="OTP from the Fatoora portal (Sandbox defaults to 123345)")
        parser.add_argument("--replace", action="store_true",
                            help="Make the new device the branch's device and archive the current one")
        parser.add_argument("--confirm-production", action="store_true",
                            help="Required in Production: invoices from the new device are legally real")

    def handle(self, *args, **options):
        branch = Branch.objects.filter(pk=options["branch"]).first()
        if branch is None:
            raise CommandError(f"No branch with id {options['branch']}.")
        settings = ZatcaSettings.for_branch(branch)
        environment = settings.environment
        if environment == Environment.PRODUCTION and not options["confirm_production"]:
            raise CommandError("This branch is in Production. Re-run with --confirm-production.")
        if not options["otp"] and not profile(environment).default_otp:
            raise CommandError(f"--otp is required in {settings.get_environment_display()}.")

        self.stdout.write(f"Onboarding a new {settings.get_environment_display()} device for {branch} "
                          f"(VAT {settings.vat_number}, invoice types {settings.invoice_types})...")
        try:
            result = onboard(branch, otp=options["otp"], activate=options["replace"],
                             actor_label="manage.py zatca_onboard")
        except OnboardingError as exc:
            for check in exc.checks:
                for line in check_lines(check):
                    self.stdout.write(line)
            where = f" Device #{exc.device.pk} is left at '{exc.device.get_status_display()}'." if exc.device else ""
            raise CommandError(f"{exc}{where}") from None

        for check in result.checks:
            for line in check_lines(check):
                self.stdout.write(line)
        device = result.device
        self.stdout.write(self.style.SUCCESS(
            f"Device #{device.pk} onboarded: {device.get_status_display()}, ICV starts at 1."))
        self.stdout.write(f"  Common name: {device.common_name}")
        self.stdout.write(f"  EGS serial:  {device.egs_serial}")
        if result.replaced:
            self.stdout.write(f"  Archived the previous device #{result.replaced.pk}.")
        elif not options["replace"]:
            self.stdout.write("  Not in use: the branch's current device is untouched. Run with --replace to switch.")
