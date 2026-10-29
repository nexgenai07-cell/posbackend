import django.core.serializers.json
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_shift_shift_staff_clock_in'),
        ('branches', '0006_branch_late_threshold_minutes_and_more'),
        ('vat_returns', '0004_adjustments_amendment'),
    ]

    operations = [
        migrations.CreateModel(
            name='VatArchiveExport',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('environment', models.CharField(choices=[('sandbox', 'Sandbox'), ('simulation', 'Simulation'), ('production', 'Production')], max_length=16)),
                ('sides', models.JSONField(default=list)),
                ('filters', models.JSONField(blank=True, default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder)),
                ('status', models.CharField(choices=[('queued', 'Queued'), ('running', 'Being prepared'), ('ready', 'Ready to download'), ('failed', 'Failed'), ('expired', 'Expired')], default='queued', max_length=10)),
                ('document_ids', models.JSONField(blank=True, default=dict)),
                ('sales_count', models.PositiveIntegerField(default=0)),
                ('purchases_count', models.PositiveIntegerField(default=0)),
                ('missing_count', models.PositiveIntegerField(default=0)),
                ('file_name', models.CharField(blank=True, max_length=255)),
                ('file_path', models.CharField(blank=True, max_length=255)),
                ('size', models.PositiveBigIntegerField(default=0)),
                ('error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vat_archive_exports', to='branches.branch')),
                ('period', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='archive_exports', to='vat_returns.vatperiod')),
                ('requested_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vat_archive_exports', to='accounts.staff')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
                'indexes': [models.Index(fields=['status', 'created_at'], name='vat_archive_status_created')],
            },
        ),
    ]
