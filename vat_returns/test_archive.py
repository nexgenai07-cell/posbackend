"""VAT dashboard M10: the invoice archive (read only), its per-document
PDF/XML downloads and the background ZIP."""

import io
import shutil
import tempfile
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock, skipUnless

from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import StaffRole
from branches.models import Branch
from common import pdf_fonts
from purchasing.models import SupplierDocument
from zatca.tests import make_staff

from . import archive_zip, invoice_pdf
from .models import ArchiveExportStatus, VatArchiveExport, VatAuditLog
from .test_figures import VatFixture, line, riyadh

D = Decimal
# Kept before ArchiveFixture patches it, for the one test that draws a real PDF.
REAL_RENDER = invoice_pdf.render


def fonts_installed():
    try:
        pdf_fonts.register()
    except pdf_fonts.PdfUnavailable:
        return False
    return True


class ArchiveFixture(VatFixture):

    def setUp(self):
        super().setUp()
        self.archive_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.archive_dir, ignore_errors=True)
        overrider = override_settings(VAT_ARCHIVE_DIR=self.archive_dir)
        overrider.enable()
        self.addCleanup(overrider.disable)
        # The tax invoice PDF needs the Arabic font; its own test is below.
        patcher = mock.patch("vat_returns.invoice_pdf.render", return_value=b"%PDF-test")
        self.render = patcher.start()
        self.addCleanup(patcher.stop)

    def listing(self, side, **params):
        response = self.api.get(f"/api/vat/archive/{side}/", params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def export(self, **body):
        response = self.api.post("/api/vat/archive/exports/", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "queued")
        # TestCase never commits, so the thread isn't started: build it here.
        return archive_zip.run(response.data["id"])


class ArchiveListTests(ArchiveFixture, TestCase):

    def test_sales_of_a_period_are_every_pos_document_in_its_window(self):
        self.q3_scenario()
        data = self.listing("sales", period=self.q3.pk)
        # Counted, pending and rejected alike; the self-test and the 1 Oct sale are not.
        self.assertEqual(data["totals"]["count"], 7)
        self.assertEqual(data["totals"]["vat"], D("59.70"))
        self.assertEqual(data["totals"]["net"], D("448.00"))
        credit = next(row for row in data["rows"] if row["kind"] == "credit_note")
        self.assertEqual((credit["net"], credit["vat"], credit["total"]), (D("-20.00"), D("-3.00"), D("-23.00")))
        self.assertTrue(data["is_test"])

    def test_sales_filters(self):
        self.q3_scenario()
        self.assertEqual(self.listing("sales", period=self.q3.pk, channel="b2b")["totals"]["count"], 1)
        self.assertEqual(self.listing("sales", period=self.q3.pk, status="pending")["totals"]["count"], 1)
        self.assertEqual(self.listing("sales", kind="debit_note")["totals"]["count"], 1)
        # Saudi days: 30 Sep 23:30 Riyadh is in, 1 Oct 00:30 is not.
        self.assertEqual(self.listing("sales", date_from="2026-09-30", date_to="2026-09-30")["totals"]["count"], 1)
        self.assertEqual(self.listing("sales", amount_min="100", amount_max="200")["totals"]["count"], 1)

    def test_purchases_of_a_period_follow_claim_date_and_skip_deleted_purchases(self):
        self.q3_scenario()
        data = self.listing("purchases", period=self.q3.pk)
        self.assertEqual(data["totals"]["count"], 4)
        self.assertEqual(data["totals"]["vat"], D("18.00"))
        self.assertEqual(self.listing("purchases", q="corner")["totals"]["count"], 1)
        self.assertEqual(self.listing("purchases", kind="credit_note")["totals"]["count"], 1)

    def test_paging(self):
        for day in range(1, 61):
            self.sale(riyadh(2026, 8, 1 + day % 28, 12), [line("S", "1.00", "0.15")])
        first = self.listing("sales")
        self.assertEqual((first["pages"], len(first["rows"]), first["totals"]["count"]), (2, 50, 60))
        self.assertEqual(len(self.listing("sales", page=2)["rows"]), 10)

    def test_bad_filters_are_named(self):
        response = self.api.get("/api/vat/archive/sales/", {"date_from": "2026-13-01", "kind": "receipt",
                                                           "period": 99999})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.data), {"date_from", "kind", "period"})
        response = self.api.get("/api/vat/archive/sales/", {"date_from": "2026-09-02", "date_to": "2026-09-01"})
        self.assertEqual(response.data, {"date_to": "error.archiveDateRange"})
        self.assertEqual(self.api.get("/api/vat/archive/payments/").status_code, 404)

    def test_access(self):
        self.q3_scenario()
        self.api.force_authenticate(self.manager)
        self.assertEqual(self.api.get("/api/vat/archive/sales/").status_code, 200)
        self.api.force_authenticate(self.cashier)
        self.assertEqual(self.api.get("/api/vat/archive/sales/").status_code, 403)
        self.assertEqual(self.api.get("/api/vat/archive/exports/").status_code, 403)
        document = self.sale(riyadh(2026, 8, 1, 12), [line("S", "100.00", "15.00")])
        other = Branch.objects.create(name_en="Other", name_ar="آخر")
        self.api.force_authenticate(make_staff(other, "Owner", StaffRole.OWNER))
        self.assertEqual(self.listing("sales")["totals"]["count"], 0)
        self.assertEqual(self.api.get(f"/api/vat/archive/sales/{document.pk}/pdf/").status_code, 404)


class ArchiveDownloadTests(ArchiveFixture, TestCase):

    def test_sale_pdf(self):
        document = self.sale(riyadh(2026, 8, 1, 12), [line("S", "100.00", "15.00")])
        response = self.api.get(f"/api/vat/archive/sales/{document.pk}/pdf/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-test")
        self.assertIn(f'filename="{document.number}.pdf"', response["Content-Disposition"])

    def test_sale_pdf_without_the_arabic_font_says_so(self):
        document = self.sale(riyadh(2026, 8, 1, 12), [line("S", "100.00", "15.00")])
        self.render.side_effect = pdf_fonts.PdfUnavailable("Arabic font not installed")
        response = self.api.get(f"/api/vat/archive/sales/{document.pk}/pdf/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"], "error.archivePdfUnavailable")

    def test_self_tests_are_not_in_the_archive(self):
        document = self.sale(riyadh(2026, 8, 1, 12), [], source="selftest")
        self.assertEqual(self.api.get(f"/api/vat/archive/sales/{document.pk}/pdf/").status_code, 404)

    def test_purchase_xml(self):
        document = self.purchase_doc(date(2026, 8, 1), "100.00", "15.00")
        url = f"/api/vat/archive/purchases/{document.pk}/xml/"
        self.assertEqual(self.api.get(url).status_code, 404)
        SupplierDocument.objects.filter(pk=document.pk).update(xml="<Invoice>supplier</Invoice>")
        response = self.api.get(url)
        self.assertEqual(response.content, b"<Invoice>supplier</Invoice>")
        self.assertIn("Fresh Meat Co_", response["Content-Disposition"])

    @skipUnless(fonts_installed(), "Arabic font or libraries not installed (common/fonts/)")
    def test_real_invoice_pdf_renders(self):
        document = self.sale(riyadh(2026, 8, 1, 12), [line("S", "100.00", "15.00")])
        document.receipt = {"seller": {"name_en": "Burger House", "name_ar": "بيت البرجر", "vat_number": "300000000000003"},
                            "items": [{"name_en": "Burger", "name_ar": "برجر", "quantity": 1, "total": "115.00",
                                       "lines": [{"quantity": 1, "unit_net": "100.00", "vat": "15.00", "rate": "15.00",
                                                  "total": "115.00"}]}],
                            "totals": {"net": "100.00", "vat": "15.00", "inclusive": "115.00", "rate": "15.00",
                                       "payable": "115.00"}}
        content = REAL_RENDER(document, self.settings)
        self.assertTrue(content.startswith(b"%PDF"))


class ArchiveZipTests(ArchiveFixture, TestCase):

    def open_zip(self, export):
        return zipfile.ZipFile(archive_zip.path_of(export))

    def test_period_zip(self):
        self.q3_scenario()
        cleared = self.sale(riyadh(2026, 8, 20, 12), [line("S", "10.00", "1.50")], status="cleared", simplified=False)
        cleared.cleared_xml = "<Invoice>cleared</Invoice>"
        cleared.save(update_fields=["cleared_xml"])
        with_pdf = self.purchase_doc(date(2026, 9, 1), "10.00", "1.50")
        SupplierDocument.objects.filter(pk=with_pdf.pk).update(pdf_url="https://files.example/invoice.pdf",
                                                               xml="<Invoice>supplier</Invoice>")
        with mock.patch("vat_returns.archive_zip._download", return_value=b"%PDF-supplier"):
            export = self.export(sides=["sales", "purchases"], period=self.q3.pk)

        self.assertEqual(export.status, ArchiveExportStatus.READY, export.error)
        self.assertEqual((export.sales_count, export.purchases_count), (8, 5))
        self.assertEqual(export.file_name, "Burger House_Q3-2026_invoices.zip")
        with self.open_zip(export) as zip_file:
            names = set(zip_file.namelist())
            self.assertIn("index.csv", names)
            self.assertIn(f"sales/{cleared.number}.pdf", names)
            self.assertIn(f"sales/{cleared.number}_cleared.xml", names)
            self.assertIn(f"purchases/Fresh Meat Co_{with_pdf.number}.pdf", names)
            self.assertEqual(zip_file.read(f"purchases/Fresh Meat Co_{with_pdf.number}.xml"),
                             b"<Invoice>supplier</Invoice>")
            index = zip_file.read("index.csv").decode("utf-8")
        self.assertTrue(index.startswith("﻿"))
        self.assertIn("missing (not uploaded)", index)
        # The other B2B sale was cleared without ZATCA's copy being stored.
        self.assertIn("missing (ZATCA's cleared copy not stored)", index)
        self.assertIn("TOTALS sales", index)
        self.assertIn("TOTALS purchases", index)
        self.assertGreater(export.missing_count, 0)
        self.assertEqual(list(VatAuditLog.objects.filter(action__startswith="archive").values_list("action", flat=True)
                              .order_by("id")), ["archive_requested", "archive_ready"])

    def test_document_ids_are_frozen_when_the_build_starts(self):
        self.q3_scenario()
        export = self.export(sides=["sales"], period=self.q3.pk)
        self.sale(riyadh(2026, 8, 21, 12), [line("S", "1.00", "0.15")])
        export.refresh_from_db()
        self.assertEqual(len(export.document_ids["sales"]), 7)

    def test_a_failed_pdf_is_listed_not_skipped(self):
        self.sale(riyadh(2026, 8, 1, 12), [line("S", "100.00", "15.00")])
        self.render.side_effect = pdf_fonts.PdfUnavailable("Arabic font not installed (Amiri-Regular.ttf)")
        export = self.export(sides=["sales"])
        self.assertEqual(export.status, ArchiveExportStatus.READY)
        with self.open_zip(export) as zip_file:
            index = zip_file.read("index.csv").decode("utf-8")
        self.assertIn("missing (Arabic font not installed (Amiri-Regular.ttf))", index)
        self.assertEqual(export.missing_count, 1)

    def test_duplicate_and_unsafe_names(self):
        self.assertEqual(archive_zip.safe_name('a/b\\c:d*e?"f<g>h|'), "a_b_c_d_e__f_g_h_")
        self.assertEqual(archive_zip.safe_name("لحوم/1"), "لحوم_1")
        names = archive_zip._Names()
        self.assertEqual([names.stem("sales", "INV-1"), names.stem("sales", "inv-1")], ["sales/INV-1", "sales/inv-1_2"])

    def test_bad_request(self):
        response = self.api.post("/api/vat/archive/exports/", {"sides": ["receipts"]}, format="json")
        self.assertEqual(response.data, {"sides": "error.archiveSidesInvalid"})
        response = self.api.post("/api/vat/archive/exports/", {"sides": ["sales"], "kind": "x"}, format="json")
        self.assertEqual(response.data, {"kind": "error.archiveKindInvalid"})
        self.assertFalse(VatArchiveExport.objects.exists())

    def test_download(self):
        self.q3_scenario()
        response = self.api.post("/api/vat/archive/exports/", {"sides": ["sales"]}, format="json")
        url = f"/api/vat/archive/exports/{response.data['id']}/download/"
        self.assertEqual(self.api.get(url).status_code, 409)
        archive_zip.run(response.data["id"])
        self.assertEqual(self.api.get(f"/api/vat/archive/exports/{response.data['id']}/").data["status"], "ready")
        response = self.api.get(url)
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content)
        self.assertIn("index.csv", zipfile.ZipFile(io.BytesIO(content)).namelist())
        self.assertTrue(VatAuditLog.objects.filter(action="archive_downloaded").exists())
        self.api.force_authenticate(make_staff(Branch.objects.create(name_en="Other", name_ar="آخر"), "O",
                                               StaffRole.OWNER))
        self.assertEqual(self.api.get(url).status_code, 404)

    def test_scheduled_run_builds_queued_requeues_stuck_and_expires_old(self):
        self.sale(riyadh(2026, 8, 1, 12), [line("S", "100.00", "15.00")])
        queued = self.api.post("/api/vat/archive/exports/", {"sides": ["sales"]}, format="json").data["id"]
        stuck = self.api.post("/api/vat/archive/exports/", {"sides": ["sales"]}, format="json").data["id"]
        long_ago = timezone.now() - timedelta(hours=1)
        VatArchiveExport.objects.filter(pk=queued).update(created_at=long_ago)
        VatArchiveExport.objects.filter(pk=stuck).update(created_at=long_ago, status=ArchiveExportStatus.RUNNING,
                                                         started_at=long_ago)
        built, expired = archive_zip.pending()
        self.assertEqual(sorted(export.pk for export in built), sorted([queued, stuck]))
        self.assertEqual(expired, 0)

        export = VatArchiveExport.objects.get(pk=queued)
        path = archive_zip.path_of(export)
        self.assertTrue(path.exists())
        VatArchiveExport.objects.filter(pk=queued).update(finished_at=timezone.now() - timedelta(days=8))
        self.assertEqual(archive_zip.expire(), 1)
        export.refresh_from_db()
        self.assertEqual((export.status, export.file_path), (ArchiveExportStatus.EXPIRED, ""))
        self.assertFalse(path.exists())
