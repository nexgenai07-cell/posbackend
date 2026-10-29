"""Add claim_date to supplier documents and fill it with each document's own
date (the only rule until filed periods exist). 0008 makes it required, in
its own transaction: PostgreSQL refuses to alter a table whose rows were
just updated while deferred foreign-key checks are still pending."""

from django.db import migrations, models


def fill(apps, schema_editor):
    SupplierDocument = apps.get_model("purchasing", "SupplierDocument")
    SupplierDocument.objects.update(claim_date=models.F("issue_date"))


class Migration(migrations.Migration):

    dependencies = [
        ('purchasing', '0006_backfill_supplierdocument_vat'),
    ]

    operations = [
        migrations.AddField(
            model_name='supplierdocument',
            name='claim_date',
            field=models.DateField(null=True),
        ),
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
