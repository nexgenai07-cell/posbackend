from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('vat_returns', '0003_payment_refund'),
    ]

    operations = [
        migrations.AddField(
            model_name='vatperiod',
            name='opening_credit',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='vatperiod',
            name='opening_credit_reason',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='vatperiod',
            name='correction',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='vatperiod',
            name='correction_reason',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='vatfiling',
            name='amendment_reason',
            field=models.TextField(blank=True),
        ),
    ]
