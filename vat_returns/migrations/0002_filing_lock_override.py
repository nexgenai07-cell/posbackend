import django.core.serializers.json
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_shift_shift_staff_clock_in'),
        ('vat_returns', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='vatauditlog',
            name='data',
            field=models.JSONField(blank=True, default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder),
        ),
        migrations.CreateModel(
            name='VatFiling',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('filed_on', models.DateField()),
                ('zatca_amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('calculated', models.JSONField(encoder=django.core.serializers.json.DjangoJSONEncoder)),
                ('calculated_net_due', models.DecimalField(decimal_places=2, max_digits=12)),
                ('difference', models.DecimalField(decimal_places=2, max_digits=12)),
                ('difference_reason', models.TextField(blank=True)),
                ('credit_treatment', models.CharField(blank=True, choices=[('carry_forward', 'Carry forward'), ('refund_requested', 'Refund requested')], max_length=20)),
                ('attachment_url', models.CharField(blank=True, max_length=500)),
                ('attachment_name', models.CharField(blank=True, max_length=255)),
                ('warnings', models.JSONField(blank=True, default=list)),
                ('is_current', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('filed_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vat_filings', to='accounts.staff')),
                ('period', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='filings', to='vat_returns.vatperiod')),
                ('supersedes', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='superseded_by', to='vat_returns.vatfiling')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
                'constraints': [models.UniqueConstraint(condition=models.Q(('is_current', True)), fields=('period',), name='vat_filing_one_current_per_period')],
            },
        ),
        migrations.CreateModel(
            name='VatLockOverride',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(choices=[('create', 'Create'), ('update', 'Update'), ('delete', 'Delete')], max_length=10)),
                ('object_type', models.CharField(max_length=60)),
                ('object_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('before', models.JSONField(blank=True, encoder=django.core.serializers.json.DjangoJSONEncoder, null=True)),
                ('after', models.JSONField(blank=True, encoder=django.core.serializers.json.DjangoJSONEncoder, null=True)),
                ('reason', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vat_lock_overrides', to='accounts.staff')),
                ('period', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='lock_overrides', to='vat_returns.vatperiod')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
    ]
