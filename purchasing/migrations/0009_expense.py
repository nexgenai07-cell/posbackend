import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """VAT dashboard M11a: business expenses with VAT, outside purchase
    orders (VAT plan §12). Schema only: there are no expenses yet."""

    dependencies = [
        ('accounts', '0005_shift_shift_staff_clock_in'),
        ('branches', '0006_branch_late_threshold_minutes_and_more'),
        ('purchasing', '0008_supplierdocument_claim_date_required'),
    ]

    operations = [
        migrations.CreateModel(
            name='Expense',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('invoice', 'Tax invoice'), ('credit_note', 'Credit note')],
                                          default='invoice', max_length=12)),
                ('number', models.CharField(max_length=64)),
                ('issue_date', models.DateField()),
                ('category', models.CharField(choices=[('rent', 'Rent'), ('utilities', 'Utilities'),
                                                       ('equipment', 'Equipment'), ('maintenance', 'Maintenance'),
                                                       ('services', 'Services'), ('marketing', 'Marketing'),
                                                       ('other', 'Other')], max_length=12)),
                ('description', models.CharField(blank=True, max_length=255)),
                ('net', models.DecimalField(decimal_places=2, max_digits=12)),
                ('vat', models.DecimalField(decimal_places=2, max_digits=12)),
                ('total', models.DecimalField(decimal_places=2, max_digits=12)),
                ('vat_category', models.CharField(choices=[('standard', 'Standard rated'), ('zero', 'Zero rated'),
                                                           ('exempt', 'Exempt')], default='standard', max_length=10)),
                ('supplier_vat_registered', models.BooleanField(default=True)),
                ('supplier_vat_number', models.CharField(blank=True, max_length=15)),
                ('supplier_crn', models.CharField(blank=True, max_length=10)),
                ('not_claimable', models.BooleanField(default=False)),
                ('not_claimable_reason', models.CharField(blank=True, choices=[
                    ('personal_use', 'Personal use'), ('entertainment', 'Entertainment / hospitality'),
                    ('gifts', 'Gifts'), ('passenger_car', 'Passenger car (non-commercial)'),
                    ('staff_benefits', 'Staff personal benefits'), ('other', 'Other')], max_length=16)),
                ('not_claimable_note', models.CharField(blank=True, max_length=255)),
                ('claimable_vat', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('capital_asset', models.BooleanField(default=False)),
                ('claim_date', models.DateField()),
                ('qr_text', models.TextField(blank=True)),
                ('qr_check', models.JSONField(blank=True, default=dict)),
                ('attachment_url', models.CharField(blank=True, max_length=500)),
                ('attachment_name', models.CharField(blank=True, max_length=255)),
                ('voided', models.BooleanField(default=False)),
                ('voided_reason', models.TextField(blank=True)),
                ('voided_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='expenses',
                                             to='branches.branch')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name='+', to='accounts.staff')),
                ('supplier', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='expenses',
                                               to='purchasing.supplier')),
                ('voided_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                                related_name='+', to='accounts.staff')),
            ],
            options={
                'ordering': ['-issue_date', '-id'],
                'indexes': [models.Index(fields=['branch', 'claim_date'], name='expense_branch_claim_date')],
            },
        ),
    ]
