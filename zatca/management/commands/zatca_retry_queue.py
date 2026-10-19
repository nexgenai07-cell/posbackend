from django.core.management.base import BaseCommand

from zatca.queue import run_once


class Command(BaseCommand):
    help = ("Issue any missed ZATCA invoices/credit notes and send every pending document. "
            "Run every 5 minutes from any scheduler; safe to run twice at once.")

    def handle(self, *args, **options):
        result = run_once()
        for document in result.issued:
            self.stdout.write(f"Issued missed invoice {document.number}")
        for document in result.credited:
            self.stdout.write(f"Issued missed credit note {document.number}")
        for document in result.sent:
            self.stdout.write(f"{document.number} (ICV {document.icv}): {document.status}")
        for document in result.failed:
            self.stdout.write(self.style.WARNING(
                f"{document.number} (ICV {document.icv}) still pending: {document.last_error or 'no answer'}"))
        if not (result.issued or result.credited or result.sent or result.failed):
            self.stdout.write("Nothing to do.")
