"""
Backfill: give every table that currently has live orders an open Bill.

Safe on an empty database (does nothing) and on a large one (chunked bulk
operations, no per-row save, no signals fired). Fully reversible: the reverse
detaches the orders and deletes only the bills this migration created, which
are identifiable because nothing else can have created a bill before it ran.

Read the plan's note before running this on production data: take a backup
first. It is reversible, but a restore is still the faster fix if anything
about the data is unexpected.
"""

from django.db import migrations

# Orders that are finished with — they belong to no open bill.
TERMINAL = ("paid", "closed", "cancelled")

# How many orders to update per query. Keeps the statement (and the lock it
# takes) bounded on a table with a lot of history.
CHUNK = 500


def create_bills(apps, schema_editor):
    Bill = apps.get_model("orders", "Bill")
    Order = apps.get_model("orders", "Order")

    # One bill per table that has at least one live order. Grouped in Python
    # rather than SQL because there are only ever as many rows here as there
    # are occupied tables — a handful, not a scan of order history.
    table_rows = (
        Order.objects.exclude(status__in=TERMINAL)
        .exclude(table__isnull=True)
        .filter(bill__isnull=True, is_deleted=False)
        .values_list("table_id", "table__branch_id")
        .distinct()
    )
    pairs = list(table_rows)
    if not pairs:
        return

    Bill.objects.bulk_create(
        [Bill(table_id=table_id, branch_id=branch_id, status="open") for table_id, branch_id in pairs],
        batch_size=CHUNK,
    )

    bill_by_table = dict(
        Bill.objects.filter(table_id__in=[table_id for table_id, _ in pairs]).values_list("table_id", "id")
    )
    for table_id, bill_id in bill_by_table.items():
        # Chunked so one enormous table's history can't produce a single
        # unbounded UPDATE.
        while True:
            ids = list(
                Order.objects.exclude(status__in=TERMINAL)
                .filter(table_id=table_id, bill__isnull=True, is_deleted=False)
                .values_list("id", flat=True)[:CHUNK]
            )
            if not ids:
                break
            Order.objects.filter(id__in=ids).update(bill_id=bill_id)


def drop_bills(apps, schema_editor):
    """
    Reverse: detach every order from its bill, then delete every bill.

    Deleting them all is correct rather than over-broad: this is the migration
    that introduced bills, so going back past it means no bill should exist.
    Order rows themselves are never touched beyond clearing the FK.
    """
    Bill = apps.get_model("orders", "Bill")
    Order = apps.get_model("orders", "Order")

    while True:
        ids = list(Order.objects.filter(bill__isnull=False).values_list("id", flat=True)[:CHUNK])
        if not ids:
            break
        Order.objects.filter(id__in=ids).update(bill_id=None)
    Bill.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0006_bill_order_bill_moyasarpayment_and_more"),
    ]

    operations = [
        migrations.RunPython(create_bills, drop_bills),
    ]
