from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("branches", "0003_branch_location_geofence")]

    operations = [
        migrations.AddField(
            model_name="branch",
            name="attendance_radius_m",
            field=models.DecimalField(decimal_places=2, default=100, max_digits=8),
        ),
    ]
