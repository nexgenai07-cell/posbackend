"""
Developer-side recovery for a locked account - in practice, the Owner, who
is the only role that can unlock from the app and so has nobody to ask.

    python manage.py unlock_staff ali.khan
    python manage.py unlock_staff ali.khan --password 'Temp-pass-2026'

With --password the account also gets that temporary password and must
change it on the next login. Without it the current password stays.
"""

from django.core.management.base import BaseCommand, CommandError

from accounts.models import Staff
from accounts.passwords import password_error_codes
from accounts.services import unlock_staff
from accounts.usernames import normalize_username


class Command(BaseCommand):
    help = "Unlock a staff account locked after repeated failed logins, optionally setting a temporary password."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument(
            "--password", dest="password", default=None,
            help="Temporary password to set as well (the staff member must change it on next login).",
        )

    def handle(self, *args, **options):
        username = normalize_username(options["username"])
        try:
            staff = Staff.objects.get(username=username)
        except Staff.DoesNotExist:
            raise CommandError(f"No staff member with username {username!r}.")

        password = options["password"]
        if password:
            codes = password_error_codes(password, staff)
            if codes:
                raise CommandError(f"Password rejected ({', '.join(codes)}): use at least 8 characters, not all digits.")

        was_locked = staff.is_locked
        unlock_staff(staff, by=None, new_password=password)

        state = "unlocked" if was_locked else "was not locked; counter reset"
        extra = " and a temporary password was set (must change on next login)" if password else ""
        self.stdout.write(self.style.SUCCESS(f"{staff.name} ({staff.username}) {state}{extra}."))
