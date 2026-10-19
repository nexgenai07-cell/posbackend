from django.core.management.base import BaseCommand, CommandError

from branches.models import Branch
from zatca.importer import CredentialImportError, import_sandbox_credentials

# Files that hold secrets in plain text once the import has encrypted them.
_SECRET_FILES = (
    r"C:\zatca\work\my_private_key.pem",
    r"C:\zatca\work\auth_compliance.txt",
    r"C:\zatca\work\auth_production.txt",
    r"C:\zatca\work\compliance_csid.json",
    r"C:\zatca\work\production_csid.json",
    r"C:\zatca\sdk\Data\Certificates\ec-secp256k1-priv-key.pem (+ .bak)",
)


class Command(BaseCommand):
    help = (
        "Import sandbox ZATCA credentials onboarded by hand with the Java SDK into a branch's "
        "device, encrypted. Prints no secret."
    )

    def add_arguments(self, parser):
        parser.add_argument("--branch", type=int, required=True, help="Branch id")
        parser.add_argument("--dir", required=True, help=r"Folder with my_private_key.pem and the CSID JSON files, e.g. C:\zatca\work")
        parser.add_argument("--replace", action="store_true", help="Archive the branch's current active device")

    def handle(self, *args, **options):
        branch = Branch.objects.filter(pk=options["branch"]).first()
        if branch is None:
            raise CommandError(f"No branch with id {options['branch']}.")
        try:
            result = import_sandbox_credentials(branch, options["dir"], replace=options["replace"])
        except CredentialImportError as exc:
            raise CommandError(str(exc)) from None

        device = result.device
        self.stdout.write(self.style.SUCCESS(f"Imported into device #{device.pk} for {branch} (Sandbox, Active)."))
        if result.replaced:
            self.stdout.write(f"Archived the previous device #{result.replaced.pk}.")
        self.stdout.write(f"  Common name: {device.common_name}")
        self.stdout.write(f"  EGS serial:  {device.egs_serial}")
        self.stdout.write(f"  Secrets stored encrypted: {', '.join(name for name in device.SECRETS if device.has_secret(name))}")
        for warning in result.warnings:
            self.stdout.write(self.style.WARNING(f"Note: {warning}"))
        self.stdout.write(
            "\nOnce milestone 3 passes in Sandbox with these credentials, delete these plain-text files "
            "(or move them into an encrypted vault):"
        )
        for path in _SECRET_FILES:
            self.stdout.write(f"  {path}")
