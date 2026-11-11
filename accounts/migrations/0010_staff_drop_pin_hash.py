# Secure login, step 3 of 3 (schema): username becomes required and unique,
# and the PIN column goes away. Not reversible past step 2 without data loss
# (pin_hash can't be rebuilt), which is why the copy happens in its own
# migration first.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_staff_username_from_pin"),
    ]

    operations = [
        migrations.AlterField(
            model_name="staff",
            name="username",
            field=models.CharField(max_length=64, unique=True),
        ),
        migrations.RemoveField(
            model_name="staff",
            name="pin_hash",
        ),
    ]
