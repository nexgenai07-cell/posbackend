from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('zatca', '0008_settings_vat_filing'),
    ]

    operations = [
        migrations.AddField(
            model_name='zatcasettings',
            name='vat_frequency_effective_from',
            field=models.DateField(blank=True, null=True),
        ),
    ]
