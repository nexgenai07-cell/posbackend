"""
Fill net_total and vat_breakdown on documents issued before 0006, from the
signed XML each one was sent with: stored figures, never recalculated.

A document is left empty (null) when its XML can't be read, or when the
VAT the XML's subtotals add up to differs from the stored vat_total. Each
one is printed, so it can be looked at; the VAT dashboard flags them too.
Nothing else on the document is touched. Running it again only fills rows
that are still empty. Reversing it is a no-op (the fields go with 0006).
"""

from decimal import Decimal

from django.db import migrations

from zatca.breakdown import BreakdownError, from_xml, vat_of


def backfill(apps, schema_editor):
    ZatcaDocument = apps.get_model("zatca", "ZatcaDocument")
    filled, problems = 0, []
    pending = ZatcaDocument.objects.filter(net_total__isnull=True).only("id", "number", "vat_total", "signed_xml")
    for document in pending.iterator(chunk_size=500):
        try:
            net_total, breakdown = from_xml(document.signed_xml)
        except BreakdownError as exc:
            problems.append(f"{document.number}: {exc}")
            continue
        if vat_of(breakdown) != Decimal(document.vat_total):
            problems.append(f"{document.number}: XML VAT {vat_of(breakdown)} != stored vat_total {document.vat_total}")
            continue
        ZatcaDocument.objects.filter(pk=document.pk).update(net_total=net_total, vat_breakdown=breakdown)
        filled += 1

    print(f"\n  VAT breakdown backfill: {filled} documents filled, {len(problems)} left empty.")
    for problem in problems:
        print(f"    - {problem}")


class Migration(migrations.Migration):

    dependencies = [
        ('zatca', '0006_document_vat_breakdown'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
