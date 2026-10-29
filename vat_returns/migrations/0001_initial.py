import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0005_shift_shift_staff_clock_in'),
        ('branches', '0006_branch_late_threshold_minutes_and_more'),
        ('zatca', '0008_settings_vat_filing'),
    ]

    operations = [
        migrations.CreateModel(
            name='VatPeriod',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('environment', models.CharField(choices=[('sandbox', 'Sandbox'), ('simulation', 'Simulation'), ('production', 'Production')], max_length=16)),
                ('frequency', models.CharField(choices=[('quarterly', 'Quarterly'), ('monthly', 'Monthly')], max_length=10)),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('due_date', models.DateField()),
                ('status', models.CharField(choices=[('open', 'Not filed'), ('filed', 'Filed'), ('paid', 'Paid'), ('credit', 'Credit')], default='open', max_length=10)),
                ('locked', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vat_periods', to='branches.branch')),
            ],
            options={
                'ordering': ['start_date'],
                'constraints': [
                    models.UniqueConstraint(fields=('branch', 'environment', 'start_date'), name='vat_period_unique_start'),
                    models.CheckConstraint(condition=models.Q(('end_date__gte', models.F('start_date'))), name='vat_period_end_after_start'),
                ],
            },
        ),
        migrations.CreateModel(
            name='VatAuditLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(max_length=40)),
                ('summary', models.TextField(blank=True)),
                ('data', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='vat_actions', to='accounts.staff')),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vat_audit_logs', to='branches.branch')),
                ('period', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='audit_logs', to='vat_returns.vatperiod')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
    ]
