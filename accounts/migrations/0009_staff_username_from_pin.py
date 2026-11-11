# Secure login, step 2 of 3 (data). For every staff row, including
# soft-deleted ones (the historical model's manager sees all rows, and
# usernames must be unique across them):
#
#   * username      <- generated from the name ("Ali Khan" -> ali.khan, a
#                      duplicate -> ali.khan2), lowest id first so the
#                      longest-serving staff keep the plain form;
#   * password_hash <- the old pin_hash, unchanged. Both are Django
#                      make_password() hashes, so the old PIN works as the
#                      first password and nobody is locked out on upgrade day;
#   * must_change_password = True, so that first login goes straight to the
#                      change-password screen.
#
# Reversing clears the three columns; pin_hash itself is untouched until
# step 3, so a rollback from here loses nothing.

from django.db import migrations

from accounts.usernames import unique_username


def forwards(apps, schema_editor):
    Staff = apps.get_model("accounts", "Staff")
    taken = set(
        Staff.objects.exclude(username__isnull=True).exclude(username="").values_list("username", flat=True)
    )
    for staff in Staff.objects.order_by("id"):
        if not staff.username:
            staff.username = unique_username(staff.name, taken.__contains__)
            taken.add(staff.username)
        staff.password_hash = staff.pin_hash or ""
        staff.must_change_password = True
        staff.save(update_fields=["username", "password_hash", "must_change_password"])


def backwards(apps, schema_editor):
    Staff = apps.get_model("accounts", "Staff")
    Staff.objects.update(username=None, password_hash="", must_change_password=False)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_staff_password_login"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
