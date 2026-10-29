from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('zatca', '0007_backfill_vat_breakdown'),
    ]

    operations = [
        migrations.AddField(
            model_name='zatcasettings',
            name='vat_filing_frequency',
            field=models.CharField(choices=[('quarterly', 'Quarterly'), ('monthly', 'Monthly')], default='quarterly', max_length=10),
        ),
        migrations.AddField(
            model_name='zatcasettings',
            name='vat_registration_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='zatcasettings',
            name='vat_tracking_start',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='zatcasettings',
            name='vat_reminder_days',
            field=models.PositiveSmallIntegerField(default=14),
        ),
    ]
