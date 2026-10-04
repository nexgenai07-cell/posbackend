from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_alter_shift_options_alter_staff_options")]

    operations = [
        migrations.AlterField(
            model_name="staff",
            name="role",
            field=models.CharField(
                choices=[
                    ("owner", "Owner"),
                    ("manager", "Manager"),
                    ("cashier", "Cashier"),
                    ("waiter", "Waiter"),
                    ("kitchen", "Kitchen"),
                ],
                max_length=16,
            ),
        ),
    ]
