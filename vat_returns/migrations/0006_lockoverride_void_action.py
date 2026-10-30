from django.db import migrations, models


class Migration(migrations.Migration):
    """VAT dashboard M11a: an override can now be a void (business expenses
    are voided, never deleted). Choices only; no data changes."""

    dependencies = [
        ('vat_returns', '0005_archive_export'),
    ]

    operations = [
        migrations.AlterField(
            model_name='vatlockoverride',
            name='action',
            field=models.CharField(choices=[('create', 'Create'), ('update', 'Update'), ('delete', 'Delete'),
                                            ('void', 'Void')], max_length=10),
        ),
    ]
