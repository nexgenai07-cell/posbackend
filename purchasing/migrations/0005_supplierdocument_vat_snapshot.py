from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('purchasing', '0004_supplier_vat'),
    ]

    operations = [
        migrations.AddField(
            model_name='supplierdocument',
            name='vat_category',
            field=models.CharField(choices=[('standard', 'Standard rated'), ('zero', 'Zero rated'), ('exempt', 'Exempt')], default='standard', max_length=10),
        ),
        migrations.AddField(
            model_name='supplierdocument',
            name='supplier_vat_registered',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='supplierdocument',
            name='supplier_vat_number',
            field=models.CharField(blank=True, max_length=15),
        ),
        migrations.AddField(
            model_name='supplierdocument',
            name='supplier_crn',
            field=models.CharField(blank=True, max_length=10),
        ),
        migrations.AddField(
            model_name='supplierdocument',
            name='claimable_vat',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
    ]
