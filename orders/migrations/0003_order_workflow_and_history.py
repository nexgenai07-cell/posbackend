import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_add_waiter_staff_role"),
        ("orders", "0002_orderitem_fired_at_orderitem_ready_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="assigned_cashier",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="cashier_orders", to="accounts.staff",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="assigned_waiter",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="waiter_orders", to="accounts.staff",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="waiter_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="waiter_confirmed_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="confirmed_orders", to="accounts.staff",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="cancelled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="cancel_reason",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="order",
            name="payment_status",
            field=models.CharField(
                choices=[("unpaid", "Unpaid"), ("partial", "Partially paid"), ("paid", "Paid")],
                default="unpaid", max_length=12,
            ),
        ),
        migrations.CreateModel(
            name="OrderHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_deleted", models.BooleanField(default=False)),
                ("event", models.CharField(max_length=40)),
                ("from_status", models.CharField(blank=True, max_length=16)),
                ("to_status", models.CharField(blank=True, max_length=16)),
                ("details", models.JSONField(blank=True, default=dict)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="order_events", to="accounts.staff")),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="history", to="orders.order")),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
    ]
