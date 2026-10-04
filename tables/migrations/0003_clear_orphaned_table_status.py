from django.db import migrations


ACTIVE_ORDER_STATUSES = ["open", "sent", "preparing", "ready", "served"]


def clear_orphaned_table_status(apps, schema_editor):
    Table = apps.get_model("tables", "Table")
    Order = apps.get_model("orders", "Order")
    database = schema_editor.connection.alias
    active_table_ids = Order.objects.using(database).filter(
        is_deleted=False, status__in=ACTIVE_ORDER_STATUSES, table__isnull=False,
        items__is_deleted=False
    ).exclude(items__status="voided").values_list("table_id", flat=True).distinct()
    stale_tables = Table.objects.using(database).exclude(status="empty").exclude(pk__in=active_table_ids)
    stale_tables.update(status="empty")


class Migration(migrations.Migration):
    dependencies = [
        ("tables", "0002_persist_table_qr_codes"),
        ("orders", "0002_orderitem_fired_at_orderitem_ready_at"),
    ]

    operations = [migrations.RunPython(clear_orphaned_table_status, migrations.RunPython.noop)]
