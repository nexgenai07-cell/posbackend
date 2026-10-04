import secrets

from django.db import migrations


def populate_table_qr_codes(apps, schema_editor):
    Table = apps.get_model("tables", "Table")
    database = schema_editor.connection.alias
    for table in Table.objects.using(database).all().iterator():
        token = table.session_token or secrets.token_urlsafe(24)
        table.session_token = token
        table.qr_code = f"/t/{token}"
        table.save(update_fields=["session_token", "qr_code"])


class Migration(migrations.Migration):
    dependencies = [("tables", "0001_initial")]

    operations = [migrations.RunPython(populate_table_qr_codes, migrations.RunPython.noop)]
