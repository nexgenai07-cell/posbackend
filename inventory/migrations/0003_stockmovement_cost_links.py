import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0002_stock_movement_stock_snapshots"),
        ("orders", "0005_idempotent_submissions_and_payments"),
        ("purchasing", "0002_purchase_purchaseitem"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockmovement",
            name="unit_cost_snapshot",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name="stockmovement",
            name="order_item",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="stock_movements", to="orders.orderitem",
            ),
        ),
        migrations.AddField(
            model_name="stockmovement",
            name="purchase",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="stock_movements", to="purchasing.purchase",
            ),
        ),
        migrations.AddField(
            model_name="stockmovement",
            name="purchase_item",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="stock_movements", to="purchasing.purchaseitem",
            ),
        ),
    ]
