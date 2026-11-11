from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0017_orderitem_handed_over_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='cash_tendered',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='payment',
            name='change_given',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='bill',
            name='cash_tendered',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='bill',
            name='change_given',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='bill',
            name='tip_at_payment',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
    ]
