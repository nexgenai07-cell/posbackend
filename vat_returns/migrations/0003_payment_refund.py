import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_shift_shift_staff_clock_in'),
        ('vat_returns', '0002_filing_lock_override'),
    ]

    operations = [
        migrations.CreateModel(
            name='VatPayment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('voided', models.BooleanField(default=False)),
                ('voided_reason', models.TextField(blank=True)),
                ('voided_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('paid_on', models.DateField()),
                ('sadad_reference', models.CharField(max_length=64)),
                ('receipt_url', models.CharField(blank=True, max_length=500)),
                ('receipt_name', models.CharField(blank=True, max_length=255)),
                ('source', models.CharField(choices=[('manual', 'Entered by hand'), ('bank_feed', 'Bank feed')], default='manual', max_length=10)),
                ('bank_transaction_id', models.CharField(blank=True, max_length=100)),
                ('period', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payments', to='vat_returns.vatperiod')),
                ('recorded_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.staff')),
                ('voided_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.staff')),
            ],
            options={
                'ordering': ['paid_on', 'id'],
                'constraints': [
                    models.CheckConstraint(condition=models.Q(('amount__gt', 0)), name='vat_payment_amount_positive'),
                    models.UniqueConstraint(condition=models.Q(('bank_transaction_id', ''), _negated=True), fields=('bank_transaction_id',), name='vat_payment_unique_bank_transaction'),
                ],
            },
        ),
        migrations.CreateModel(
            name='VatRefund',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('voided', models.BooleanField(default=False)),
                ('voided_reason', models.TextField(blank=True)),
                ('voided_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('received_on', models.DateField()),
                ('reference', models.CharField(max_length=64)),
                ('attachment_url', models.CharField(blank=True, max_length=500)),
                ('attachment_name', models.CharField(blank=True, max_length=255)),
                ('period', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='refunds', to='vat_returns.vatperiod')),
                ('recorded_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.staff')),
                ('voided_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.staff')),
            ],
            options={
                'ordering': ['received_on', 'id'],
                'constraints': [
                    models.CheckConstraint(condition=models.Q(('amount__gt', 0)), name='vat_refund_amount_positive'),
                ],
            },
        ),
    ]
