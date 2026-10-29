from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('zatca', '0005_go_live_and_refund_links'),
    ]

    operations = [
        migrations.AddField(
            model_name='zatcadocument',
            name='net_total',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='zatcadocument',
            name='vat_breakdown',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
