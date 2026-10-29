"""
Invoice archive ZIPs (plan §11), built in the background:

  <Restaurant>_<period or range>_invoices.zip
    index.csv                       UTF-8 with BOM, one row per document
    sales/INV-3-000123.pdf          the tax invoice PDF (invoice_pdf.py)
    sales/INV-3-000123.xml          the XML this POS signed
    sales/INV-3-000123_cleared.xml  ZATCA's cleared copy (B2B)
    purchases/<supplier>_<number>.pdf / .xml   the supplier's own files

A file that can't be included is written in index.csv as "missing (why)":
nothing is skipped silently. The ZIP is a copy; no document is changed.

The repo has no task queue, so like the ZATCA retry queue: request() saves
a VatArchiveExport and starts a daemon thread once the transaction commits;
`manage.py vat_archive_exports` (Task Scheduler / cron, every few minutes)
builds anything still queued, re-queues a build stuck after a crash, and
removes ready ZIPs after RETENTION. claim() makes sure only one of them
builds an export. The ZIP is written to disk entry by entry, never held in
memory as a whole.
"""

import logging
import re
import threading
import zipfile
from copy import copy
from datetime import timedelta
from pathlib import Path

import requests
from django.conf import settings as django_settings
from django.db import connection, transaction
from django.utils import timezone

from common import pdf_fonts
from common.csv_export import build_csv, money
from zatca.environments import profile
from zatca.models import DocumentStatus, ZatcaSettings

from . import archive, invoice_pdf
from . import periods as vat_periods
from .models import ArchiveExportStatus, VatArchiveExport, VatAuditLog

log = logging.getLogger("vat_returns.archive")

RETENTION = timedelta(days=7)
STUCK_AFTER = timedelta(minutes=30)
# A queued export younger than this is left to the thread that was just started.
THREAD_GRACE = timedelta(minutes=1)
CHUNK = 200
MAX_DOWNLOAD = 25 * 1024 * 1024
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')

INDEX_COLUMNS = ["Side", "Number", "Date", "Type", "B2C/B2B", "Customer / supplier", "Seller VAT", "Buyer VAT",
                 "Net", "VAT", "Total", "Status", "Claim date", "PDF file", "XML file", "Cleared XML file"]
KIND_TEXT = {"invoice": "Invoice", "credit_note": "Credit note", "debit_note": "Debit note"}


class ExportRejected(Exception):
    def __init__(self, errors):
        super().__init__(errors)
        self.errors = errors


def safe_name(text, fallback="document"):
    """A file name without / \\ : * ? " < > | or control characters; Arabic kept."""
    name = _UNSAFE.sub("_", str(text or "")).strip().strip(".")
    return name[:120] or fallback


class _Names:
    """Unique file stems per folder: a second "INV-1" becomes "INV-1_2"."""

    def __init__(self):
        self.used = set()

    def stem(self, folder, base):
        base = safe_name(base)
        candidate, n = base, 2
        while f"{folder}/{candidate}".lower() in self.used:
            candidate, n = f"{base}_{n}", n + 1
        self.used.add(f"{folder}/{candidate}".lower())
        return f"{folder}/{candidate}"


def export_dir():
    return Path(django_settings.VAT_ARCHIVE_DIR)


# -- request -----------------------------------------------------------------------


def request(branch, settings, actor, data):
    """Saves an export of `data["sides"]` (sales / purchases) matching the
    filters in `data` and starts building it. Raises ExportRejected."""
    sides = data.get("sides") or list(archive.SIDES)
    if isinstance(sides, str):
        sides = [sides]
    if not isinstance(sides, list) or not sides or any(side not in archive.SIDES for side in sides):
        raise ExportRejected({"sides": "error.archiveSidesInvalid"})
    sides = [side for side in archive.SIDES if side in sides]
    parsed, errors = {}, {}
    for side in sides:
        try:
            parsed[side] = archive.parse(side, data, branch, settings)
        except archive.FilterInvalid as exc:
            errors.update(exc.errors)
    if errors:
        raise ExportRejected(errors)
    period = next((filters["period"] for filters in parsed.values() if "period" in filters), None)
    with transaction.atomic():
        export = VatArchiveExport.objects.create(
            branch=branch, environment=settings.environment, sides=sides, period=period, requested_by=actor,
            filters={side: archive.as_json(filters) for side, filters in parsed.items()},
        )
        audit(export, actor, "archive_requested", f"Invoice archive ZIP requested ({', '.join(sides)})")
        transaction.on_commit(lambda: start(export.pk))
    return export


def start(pk):
    thread = threading.Thread(target=_run_in_thread, args=(pk,), daemon=True, name=f"vat-archive-{pk}")
    thread.start()


def _run_in_thread(pk):
    try:
        run(pk)
    finally:
        connection.close()


# -- build -------------------------------------------------------------------------


def claim(pk):
    """Queued -> running, for one caller only. True if this caller got it."""
    return bool(VatArchiveExport.objects.filter(pk=pk, status=ArchiveExportStatus.QUEUED)
                .update(status=ArchiveExportStatus.RUNNING, started_at=timezone.now()))


def run(pk):
    """Builds export `pk` if it is still queued. Returns it (None if
    someone else took it)."""
    if not claim(pk):
        return None
    export = VatArchiveExport.objects.select_related("branch", "requested_by", "period").get(pk=pk)
    try:
        _build(export)
    except Exception as exc:  # a failed build is recorded, never left "running"
        log.exception("Invoice archive export %s failed", pk)
        export.status = ArchiveExportStatus.FAILED
        export.error = str(exc)[:1000] or exc.__class__.__name__
        export.finished_at = timezone.now()
        export.save(update_fields=["status", "error", "finished_at"])
        audit(export, None, "archive_failed", f"Invoice archive ZIP failed: {export.error}")
    return export


def _settings_for(export):
    """The branch's settings as they apply to this export: in the
    environment it was requested in, even if that has changed since."""
    settings = copy(ZatcaSettings.for_branch(export.branch))
    settings.environment = export.environment
    return settings


def _build(export):
    branch = export.branch
    settings = _settings_for(export)
    filters = {side: archive.parse(side, export.filters.get(side, {}), branch, settings) for side in export.sides}
    querysets = {side: archive.QUERIES[side](branch, settings, filters[side]) for side in export.sides}
    export.document_ids = {side: list(queryset.values_list("id", flat=True)) for side, queryset in querysets.items()}
    export.sales_count = len(export.document_ids.get("sales", []))
    export.purchases_count = len(export.document_ids.get("purchases", []))
    export.file_name = _file_name(branch, settings, export)
    export.save(update_fields=["document_ids", "sales_count", "purchases_count", "file_name"])

    folder = export_dir() / str(branch.pk)
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{export.pk}.zip"
    part = folder / f"{export.pk}.zip.part"
    names, rows, missing = _Names(), [], 0
    sums = {side: {"net": archive.ZERO, "vat": archive.ZERO, "total": archive.ZERO} for side in export.sides}
    with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        if "sales" in export.sides:
            for document in _in_chunks(querysets["sales"], export.document_ids["sales"], ("issued_at", "id")):
                row, count = _add_sale(zip_file, names, document, settings)
                rows.append(row)
                missing += count
                _add_sums(sums["sales"], archive.sales_row(document))
        if "purchases" in export.sides:
            for document in _in_chunks(querysets["purchases"], export.document_ids["purchases"], ("issue_date", "id")):
                row, count = _add_purchase(zip_file, names, document, settings)
                rows.append(row)
                missing += count
                _add_sums(sums["purchases"], archive.purchase_row(document))
        for side, total in sums.items():
            rows.append([f"TOTALS {side}", "", "", "", "", "", "", "", money(total["net"]), money(total["vat"]),
                         money(total["total"]), "", "", "", "", ""])
        index = build_csv(header=_index_header(branch, settings, export), columns=INDEX_COLUMNS, rows=rows)
        zip_file.writestr("index.csv", index.encode("utf-8"))
    part.replace(final)

    export.status = ArchiveExportStatus.READY
    export.file_path = str(final.relative_to(export_dir()))
    export.size = final.stat().st_size
    export.missing_count = missing
    export.finished_at = timezone.now()
    export.save(update_fields=["status", "file_path", "size", "missing_count", "finished_at"])
    audit(export, None, "archive_ready",
           f"Invoice archive ZIP ready: {export.sales_count} sales, {export.purchases_count} purchase documents, "
           f"{missing} files missing")


def _in_chunks(queryset, ids, ordering):
    """The frozen documents, oldest first, a few hundred at a time."""
    for start in range(0, len(ids), CHUNK):
        yield from queryset.filter(pk__in=ids[start:start + CHUNK]).order_by(*ordering)


def _add_sums(total, row):
    for key in ("net", "vat", "total"):
        total[key] += row[key] or archive.ZERO


def _missing(reason):
    return f"missing ({reason})"


def _add_sale(zip_file, names, document, settings):
    stem = names.stem("sales", document.number)
    missing = 0
    try:
        zip_file.writestr(f"{stem}.pdf", invoice_pdf.render(document, settings))
        pdf = f"{stem}.pdf"
    except pdf_fonts.PdfUnavailable as exc:
        pdf, missing = _missing(str(exc)), missing + 1
    except Exception as exc:  # one bad document must not stop the ZIP
        log.exception("Invoice PDF for %s failed", document.number)
        pdf, missing = _missing(f"PDF failed: {exc}"), missing + 1
    if document.signed_xml:
        zip_file.writestr(f"{stem}.xml", document.signed_xml.encode("utf-8"))
        xml = f"{stem}.xml"
    else:
        xml, missing = _missing("no signed XML stored"), missing + 1
    cleared = ""
    if document.cleared_xml:
        zip_file.writestr(f"{stem}_cleared.xml", document.cleared_xml.encode("utf-8"))
        cleared = f"{stem}_cleared.xml"
    elif not document.simplified:
        reason = ("ZATCA's cleared copy not stored" if document.status == DocumentStatus.CLEARED
                  else f"not cleared by ZATCA: {DocumentStatus(document.status).label}")
        cleared, missing = _missing(reason), missing + 1
    row = archive.sales_row(document)
    issued = document.issued_at.astimezone(vat_periods.ZoneInfo(settings.timezone))
    data = [
        "sales", document.number, f"{issued:%Y-%m-%d %H:%M}", KIND_TEXT[document.kind], row["channel"].upper(),
        row["customer"], (document.receipt or {}).get("seller", {}).get("vat_number", "") or settings.vat_number,
        row["customer_vat"], money(row["net"]), money(row["vat"]), money(row["total"]),
        DocumentStatus(document.status).label, "", pdf, xml, cleared,
    ]
    return data, missing


def _download(url):
    if not url.startswith("https://"):
        raise ValueError("not an https link")
    response = requests.get(url, timeout=30, stream=True)
    response.raise_for_status()
    content = b""
    for chunk in response.iter_content(64 * 1024):
        content += chunk
        if len(content) > MAX_DOWNLOAD:
            raise ValueError("larger than 25 MB")
    return content


def _add_purchase(zip_file, names, document, settings):
    supplier = document.purchase.supplier
    stem = names.stem("purchases", f"{supplier.name_en or supplier.name_ar}_{document.number}")
    missing = 0
    if not document.pdf_url:
        pdf, missing = _missing("not uploaded"), missing + 1
    else:
        try:
            zip_file.writestr(f"{stem}.pdf", _download(document.pdf_url))
            pdf = f"{stem}.pdf"
        except (requests.RequestException, ValueError) as exc:
            pdf, missing = _missing(f"download failed: {exc}"), missing + 1
    if document.xml:
        zip_file.writestr(f"{stem}.xml", document.xml.encode("utf-8"))
        xml = f"{stem}.xml"
    else:
        xml, missing = _missing("not keyed in"), missing + 1
    row = archive.purchase_row(document)
    data = [
        "purchases", document.number, f"{document.issue_date:%Y-%m-%d}", KIND_TEXT[document.kind], "B2B",
        row["supplier"], document.supplier_vat_number, settings.vat_number, money(row["net"]), money(row["vat"]),
        money(row["total"]), "", f"{document.claim_date:%Y-%m-%d}", pdf, xml, "",
    ]
    return data, missing


def describe(export):
    """("Q3 2026", "Q3-2026") or a date range, for the index header and the
    file name."""
    if export.period_id:
        text = vat_periods.label_text(export.period)
        return text, text.replace(" ", "-")
    filters = next(iter(export.filters.values()), {}) if export.filters else {}
    start, end = filters.get("date_from"), filters.get("date_to")
    if start or end:
        return f"{start or 'start'} to {end or 'today'}", f"{start or 'start'}_{end or 'today'}"
    return "all dates", "all"


def _file_name(branch, settings, export):
    restaurant = settings.seller_name_en or branch.name_en or settings.seller_name_ar or branch.name_ar
    return safe_name(f"{restaurant}_{describe(export)[1]}_invoices", "invoices") + ".zip"


def _index_header(branch, settings, export):
    restaurant = " / ".join(name for name in (settings.seller_name_en or branch.name_en,
                                              settings.seller_name_ar or branch.name_ar) if name)
    rows = [("Restaurant", restaurant), ("VAT number", settings.vat_number), ("Report", "Invoice archive"),
            ("Documents", describe(export)[0]), ("Generated", f"{timezone.now():%Y-%m-%d %H:%M} UTC")]
    if profile(settings.environment).is_test:
        rows.append(("Environment", f"{settings.environment} (TEST: not real tax documents)"))
    return rows


# -- scheduled run -----------------------------------------------------------------


def pending(now=None):
    """What the scheduled command does: re-queue stuck builds, build every
    queued export the thread hasn't taken, remove expired ZIPs. Returns
    (built exports, expired count)."""
    now = now or timezone.now()
    VatArchiveExport.objects.filter(status=ArchiveExportStatus.RUNNING, started_at__lt=now - STUCK_AFTER).update(
        status=ArchiveExportStatus.QUEUED, started_at=None)
    built = []
    for pk in (VatArchiveExport.objects.filter(status=ArchiveExportStatus.QUEUED, created_at__lt=now - THREAD_GRACE)
               .values_list("pk", flat=True)):
        export = run(pk)
        if export is not None:
            built.append(export)
    return built, expire(now)


def expire(now=None):
    """Removes ZIPs ready for longer than RETENTION; the row stays, as Expired."""
    now = now or timezone.now()
    count = 0
    for export in VatArchiveExport.objects.filter(status=ArchiveExportStatus.READY, finished_at__lt=now - RETENTION):
        path = export_dir() / export.file_path
        if export.file_path and path.exists():
            path.unlink()
        export.status = ArchiveExportStatus.EXPIRED
        export.file_path = ""
        export.save(update_fields=["status", "file_path"])
        count += 1
    return count


def path_of(export):
    return export_dir() / export.file_path


def export_dict(export):
    label = describe(export)[0]
    return {
        "id": export.pk, "status": export.status, "sides": export.sides, "filters": export.filters,
        "period": export.period_id, "description": label, "sales_count": export.sales_count,
        "purchases_count": export.purchases_count, "missing_count": export.missing_count,
        "file_name": export.file_name, "size": export.size, "error": export.error,
        "requested_by": export.requested_by.name, "created_at": export.created_at,
        "finished_at": export.finished_at,
        "expires_at": export.finished_at + RETENTION if export.status == ArchiveExportStatus.READY else None,
    }


def audit(export, actor, action, summary, **data):
    VatAuditLog.objects.create(branch=export.branch, period=export.period, actor=actor, action=action,
                               summary=summary, data={"export": export.pk, "sides": export.sides,
                                                      "filters": export.filters, **data})
