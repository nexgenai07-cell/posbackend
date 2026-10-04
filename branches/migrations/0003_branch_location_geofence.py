from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("branches", "0002_alter_branch_options")]

    operations = [
        migrations.AddField(
            model_name="branch",
            name="latitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="branch",
            name="longitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="branch",
            name="geofence_radius_m",
            field=models.DecimalField(decimal_places=2, default=2, max_digits=8),
        ),
    ]
