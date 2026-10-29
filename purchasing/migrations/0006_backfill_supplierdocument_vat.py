"""
Fill the new VAT fields on supplier documents saved before 0005.

This is the one place where the supplier's details as they are TODAY are
used for old documents: there is no earlier copy to take them from. After
this, each document keeps its own copy (SupplierDocument.save()).

Per document: copy the supplier's VAT-registered flag, VAT number and CR;
VAT 0 -> zero rated (the approved default), otherwise standard rated; and
claimable VAT = the VAT (negative for a credit note) only if the supplier
is VAT-registered and has a VAT number, else 0. The same rules as
purchasing/vat.py, repeated here because migrations can't call model code.

Prints how many documents were filled and how much VAT is now held back for
review (it was counted as claimable before when only the VAT number was
missing). Reversing it is a no-op (the fields go with 0005).
"""

from decimal import Decimal

from django.db import migrations


def backfill(apps, schema_editor):
    SupplierDocument = apps.get_model("purchasing", "SupplierDocument")
    filled, held_back, held_vat = 0, 0, Decimal("0.00")
    for document in SupplierDocument.objects.select_related("purchase__supplier").iterator(chunk_size=500):
        supplier = document.purchase.supplier
        sign = -1 if document.kind == "credit_note" else 1
        claimable = supplier.vat_registered and bool(supplier.vat_number)
        if not claimable and document.vat:
            held_back += 1
            held_vat += sign * document.vat
        SupplierDocument.objects.filter(pk=document.pk).update(
            supplier_vat_registered=supplier.vat_registered,
            supplier_vat_number=supplier.vat_number,
            supplier_crn=supplier.crn,
            vat_category="standard" if document.vat else "zero",
            claimable_vat=sign * document.vat if claimable else Decimal("0.00"),
        )
        filled += 1

    print(f"\n  Supplier document VAT backfill: {filled} documents filled; "
          f"{held_back} now need review (VAT {held_vat:.2f} no longer claimed until the supplier is fixed).")


class Migration(migrations.Migration):

    dependencies = [
        ('purchasing', '0005_supplierdocument_vat_snapshot'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
