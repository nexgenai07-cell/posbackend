# Secure login, step 1 of 3 (schema): the username/password and lockout
# columns, added alongside the old pin_hash so step 2 can copy it across.
# `username` is nullable only here; step 3 makes it required and unique
# once every row has one.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_staff_is_head_chef"),
    ]

    operations = [
        migrations.AddField(
            model_name="staff",
            name="username",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="staff",
            name="password_hash",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="staff",
            name="must_change_password",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="staff",
            name="failed_login_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="staff",
            name="locked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="staff",
            name="locked_reason",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="staff",
            name="unlocked_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="+", to="accounts.staff",
            ),
        ),
        migrations.AddField(
            model_name="staff",
            name="unlocked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
