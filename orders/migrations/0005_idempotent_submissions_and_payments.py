from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("orders", "0004_alter_order_status")]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="idempotency_key",
            field=models.CharField(blank=True, max_length=128, null=True, unique=True),
        ),
        migrations.CreateModel(
            name="CustomerOrderSubmission",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_deleted", models.BooleanField(default=False)),
                ("idempotency_key", models.CharField(max_length=128, unique=True)),
                ("request_hash", models.CharField(max_length=64)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="customer_submissions", to="orders.order")),
            ],
            options={"ordering": ["id"]},
        ),
    ]
