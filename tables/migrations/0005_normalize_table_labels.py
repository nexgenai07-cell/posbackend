from django.db import migrations


def normalize_labels(apps, schema_editor):
    # Same rules as TableSerializer.validate(): English upper-cased, both
    # languages trimmed with inner whitespace collapsed.
    Table = apps.get_model("tables", "Table")
    for table in Table.objects.all():
        label_en = " ".join(table.label_en.split()).upper()
        label_ar = " ".join(table.label_ar.split())
        if (label_en, label_ar) != (table.label_en, table.label_ar):
            table.label_en, table.label_ar = label_en, label_ar
            table.save(update_fields=["label_en", "label_ar"])


class Migration(migrations.Migration):

    dependencies = [
        ("tables", "0004_table_seats"),
    ]

    operations = [
        migrations.RunPython(normalize_labels, migrations.RunPython.noop),
    ]
