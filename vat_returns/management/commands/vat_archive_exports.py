from django.core.management.base import BaseCommand

from vat_returns.archive_zip import pending


class Command(BaseCommand):
    help = ("Build invoice archive ZIPs still queued (or stuck after a crash) and remove ready ZIPs older "
            "than 7 days. Run every 5 minutes from any scheduler; safe to run twice at once.")

    def handle(self, *args, **options):
        built, expired = pending()
        for export in built:
            if export.status == "ready":
                self.stdout.write(f"Export {export.pk} ready: {export.file_name} ({export.sales_count} sales, "
                                  f"{export.purchases_count} purchase documents, {export.missing_count} files missing)")
            else:
                self.stdout.write(self.style.WARNING(f"Export {export.pk} failed: {export.error}"))
        if expired:
            self.stdout.write(f"Removed {expired} expired ZIP(s).")
        if not (built or expired):
            self.stdout.write("Nothing to do.")
