from django.db import migrations, models


def backfill_payment_status(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    OrderItem = apps.get_model("orders", "OrderItem")
    Payment = apps.get_model("orders", "Payment")
    for order in Order.objects.all().iterator():
        total = sum(
            (item.price_snapshot * item.quantity for item in OrderItem.objects.filter(order_id=order.pk).exclude(status="voided")),
            start=0,
        )
        paid = sum(
            (payment.amount for payment in Payment.objects.filter(order_id=order.pk)),
            start=0,
        )
        if paid and paid >= total:
            value = "paid"
        elif paid:
            value = "partial"
        else:
            value = "unpaid"
        Order.objects.filter(pk=order.pk).update(payment_status=value)


class Migration(migrations.Migration):
    dependencies = [("orders", "0003_order_workflow_and_history")]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="status",
            field=models.CharField(
                choices=[
                    ("open", "Open"),
                    ("pending_cashier", "Pending cashier review"),
                    ("awaiting_waiter", "Awaiting waiter confirmation"),
                    ("confirmed", "Confirmed"),
                    ("sent", "Sent"),
                    ("preparing", "Preparing"),
                    ("ready", "Ready"),
                    ("served", "Served"),
                    ("paid", "Paid"),
                    ("closed", "Closed"),
                    ("cancelled", "Cancelled"),
                ],
                default="open",
                max_length=16,
            ),
        ),
        migrations.RunPython(backfill_payment_status, migrations.RunPython.noop),
    ]
