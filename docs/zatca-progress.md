# ZATCA progress and runbook

Updated after every milestone. Plans: `docs/ZATCA_Approval_Prompt.md` (ZATCA e-invoicing) and
`docs/vat-dashboard-plan.md` (VAT dashboard + invoice archive).

Last update: **2026-10-30: VAT dashboard M11a + M11b (business expenses) code written, waiting for your test run.**

**How to run tests** without touching Neon (`.env` points `DATABASE_URL` at Neon):
```
$env:DATABASE_URL = "sqlite:///" + ($env:TEMP -replace '\\','/') + "/zatca_test.sqlite3"
venv\Scripts\python manage.py test <apps> --noinput
Remove-Item Env:DATABASE_URL
```

---

## Working rules

1. **I don't run commands.** No tests, migrations or other shell commands. I read the code with file tools only.
2. **Each milestone ends with:** the exact commands in order (makemigrations if needed → migrate → tests) and the expected output. You run them on your **local default database** and send me the output.
3. **Stop after each milestone.** VAT dashboard: build M1 → M6, stopping after each. M7–M9 come after you've tested M6, then M10.
4. **VPS later.** When the whole VAT feature is done I'll give you a separate VPS checklist, starting with a database backup (`pg_dump`, database `posdb`, port 5433) **before** migrating, because the M1 and M2 backfills change existing data.
5. **Records are never deleted.** Filed periods, payments, refunds, attachments and ZATCA documents are kept for at least 6 years, and every action is audit-logged.
6. **Stored figures only.** The VAT return adds up stored amounts; it never recalculates VAT from prices.
7. **Proper fixes, backend changes are fine, no hidden UI logic:** derived numbers are shown with their formula.
8. This file is updated at the end of every milestone.

---

## ZATCA e-invoicing milestones (from `ZATCA_Approval_Prompt.md`)

These statuses come from **reading the code** on 2026-10-29. Please correct any you know to be different.

| # | Milestone | Status | Evidence in code |
|---|---|---|---|
| 1 | Settings page, environments module, VAT engine | ✅ code present | `zatca/environments.py`, `zatca/vat.py`, `ZatcaBusinessPage.tsx` |
| 2 | Device, encryption, credential import | ✅ code present | `ZatcaDevice` (Fernet `*_enc`), `zatca/importer.py`, `ZatcaCredentialsPage.tsx` |
| 3 | Build, sign, QR, send + `zatca_onboard` | ✅ code present | `ubl.py`, `signing.py`, `qr.py`, `management/commands/zatca_onboard.py` |
| 4 | POS hook + background reporting | ✅ code present | `zatca/pos.py`, `zatca/queue.py`, `zatca_retry_queue` |
| 5 | B2B at payment + clearance | ✅ code present | `BusinessCustomer`, `CompanyInvoicePicker.tsx` |
| 6 | Receipts + credit notes | ✅ code present | `ZatcaReceipt.tsx`, `pos.credit_cancelled_order` |
| 7 | Go-live wizard + practice mode | ✅ code present | `zatca/golive.py`, `ZatcaGoLivePage.tsx` |
| 8 | Partial refunds + debit notes | ✅ code present | `pos.credit_refund`, `pos.debit_note`, `DebitNoteView` |
| 9 | Purchasing VAT (supplier QR, Input VAT report) | ✅ code present (fixes in VAT M2) | `purchasing/vat.py`, `InputVatReportPage.tsx` |
| 10 | Simulation run + readiness | 🟡 partial | Certificate expiry warnings exist (`serializers.py`). Simulation run with a real company not confirmed; unreported-invoice banner and runbook not found |

---

## VAT dashboard milestones (`vat-dashboard-plan.md` §10)

| # | Milestone | Status |
|---|---|---|
| M1 | Stored VAT breakdown on ZATCA documents (+ backfill from XML) | ✅ **tests pass** (2026-10-29: `manage.py test zatca`, 98 tests OK, temp SQLite). Migrations 0006/0007 not applied yet |
| M2 | Input VAT fixes (snapshots, claimable rules, `vatNot15`, review card, deleted-purchase fix, BOM CSV) | 🟡 **code written; you said "move next" without the test output, so not confirmed yet** |
| M3 | Settings + VAT periods (frequency, registration date, due date, menu item) | 🟡 **1 test failed (2026-10-29, `test_switching_to_monthly...` read the log oldest-first); fixed in the test. The rerun output was cut off, so not confirmed yet** |
| M4 | Cards + return boxes + Show documents + per-document XML download | ✅ **tests pass** (2026-10-29, you confirmed; output not pasted). M5 changes the M4 test setup slightly, so the M5 run re-checks it |
| M5 | Checklist + Mark as filed + period lock + override log + `VatAuditLog` | 🟡 **code written; you said "move next" without the test output, so not confirmed yet** (migration `vat_returns/0002`) |
| M6 | Payments, refunds (requested / received), status, credit carried forward | 🟡 **code written; not confirmed.** You sent part of the test run (only the expected `RuntimeError: boom`) and asked to go on to M7, reporting errors if any come (migration `vat_returns/0003`) |
| M7 | History + CSV/PDF export (summary table + optional per-period PDF) | 🟡 **code written; not confirmed** (you said "move next"; you'll report errors) |
| M8 | Reminder banners | 🟡 **code written, waiting for your test run** (no migrations) |
| M9 | Late purchases, amendments, frequency switch, first period | 🟡 **code present** (`adjustments.py`, `test_late_amend.py`, migration `vat_returns/0004`); no test output recorded here |
| M10 | Invoice archive (Sales/Purchases tabs, per-document PDF/XML, background ZIP) | 🟡 **code written, waiting for your test run** (migration `vat_returns/0005`; Arabic font files to add) |
| M11a | Expenses: model, claimable rules, Expenses page, locks (plan §12) | 🟡 **code written, waiting for your test run** (migrations `purchasing/0009_expense`, `vat_returns/0006`) |
| M11b | Expenses in VAT figures/boxes, Input VAT report + CSV, invoice archive (plan §12) | 🟡 **code written, waiting for your test run** (no migrations) |

### M1: what was built
- `zatca/breakdown.py`: turns the VAT engine's result or a stored signed XML into `(net_total, vat_breakdown)`.
- `ZatcaDocument.net_total` and `ZatcaDocument.vat_breakdown`: filled by `documents.issue()` for every new document. Amounts are positive for every kind; credit notes are subtracted later, when amounts are added up.
- Migration `zatca/0006_document_vat_breakdown` adds the fields. `zatca/0007_backfill_vat_breakdown` fills existing documents from their `signed_xml`, prints a summary, and leaves a document empty (and names it) if its XML can't be read or its XML VAT ≠ stored `vat_total`.
- Shown in Django admin (Documents list) and in the Invoices API (`net_total`, `vat_breakdown`).
- Tests: `zatca/test_vat_breakdown.py` (8 tests).

### M2: what was built
- **`SupplierDocument` new fields:** `vat_category` (standard / zero / exempt), the supplier copy (`supplier_vat_registered`, `supplier_vat_number`, `supplier_crn`) and stored `claimable_vat`. `save()` writes them every time: it copies the supplier, sets VAT 0 + standard → zero rated, and sets claimable = VAT (negative for a credit note) only if the supplier was registered **and** had a VAT number.
- **Editing a supplier** through the API re-copies its details onto its documents (`refresh_supplier_documents`). Example: add the missing VAT number and the VAT becomes claimable. *M5 will limit this to unlocked periods.*
- **New flag `vatNot15`:** more than 0.05 away from 15% of net; skipped for zero rated and exempt. Warns only.
- **"VAT needing review":** documents flagged `notRegistered` or `supplierVatMissing`, with a count and the VAT not claimed. Shown as a card on the Input VAT page, with a "Fix supplier" link.
- **Deleted purchases:** they no longer count in the report, and a purchase with supplier documents can't be deleted (`409 error.purchaseHasSupplierDocuments`).
- **CSV built on the server:** `GET /api/purchases/input-vat/csv/?from=&to=`. UTF-8 with BOM, CRLF, a header block (restaurant, VAT number, period), VAT category, VAT rate %, supplier CR, 2-decimal amounts, credit notes negative, flags in English joined by "; ", and a TOTALS row. Shared writer: `common/csv_export.py` (M7 and M10 reuse it).
- The browser CSV helper (`restaurant-admin/src/lib/csv.ts`) now adds a BOM too, so every other export opens correctly in Excel with Arabic.
- `CORS_EXPOSE_HEADERS = ['Content-Disposition']`, so the admin gets the server's file name for downloads.
- **Admin:** a VAT category on the supplier document form (shown when VAT is 0: zero rated or exempt), category and claimable VAT on the document summary, VAT rate % and category per row on the report.
- **Migrations:**
  - `purchasing/0005_supplierdocument_vat_snapshot`: schema.
  - `purchasing/0006_backfill_supplierdocument_vat`: fills existing documents from the supplier **as it is today** (no earlier copy exists) and prints how much VAT is now held back for review.
- **Tests:** `purchasing/test_input_vat.py` (9 new). One existing test (`test_report_flags`) now also expects `vatNot15`, because net 90 with VAT 15 is 16.67%.
- **Different from the plan:**
  - Flags are **worked out on each request** from stored figures, not stored themselves. They're warnings, and "net differs from PO" depends on the purchase lines, which can change. Every *amount* is stored.
  - `claim_period` moves to M3, because it needs the VAT period model.

### M3: what was built
- **New app `vat_returns`:**
  - `VatPeriod`: branch, environment, frequency, start, end, due date, status, locked. Unique per branch + environment + start date.
  - `VatAuditLog`: actor, action, summary, before/after data.
  - Both are read-only in Django admin.
- **VAT filing settings** on `ZatcaSettings`: `vat_filing_frequency`, `vat_registration_date`, `vat_tracking_start`, `vat_reminder_days`.
  - Endpoint: `GET`/`PATCH /api/vat/settings/`. Owner edits, manager reads, cashier gets 403.
  - Every change is audited. A change to the frequency or a date rebuilds the periods.
  - The rebuild is refused (`409 error.vatPeriodsFiled`) once any period is locked, and the whole change is rolled back.
- **Periods** (`vat_returns/periods.py`):
  - Built lazily on `GET /api/vat/periods/`, up to the period containing today (Saudi time).
  - The first period starts on the later of the registration date and "track VAT returns from", so it can be short.
  - After that, periods are calendar months or quarters, back to back.
  - Due date = last day of the following month. `days_left` is negative once overdue.
  - Each environment has its own periods. Production never lists Sandbox or Simulation ones, which are kept.
  - `GET /api/vat/periods/<id>/` returns one period of the current environment.
- **`SupplierDocument.claim_date`:** the date that decides which period claims a document. It equals the document's date for now; the late-invoice rule comes in M5/M9.
  - Migrations: `purchasing/0007` (add + fill) and `0008` (make it required).
- **Migrations:** `zatca/0008_settings_vat_filing`, `vat_returns/0001_initial`, `purchasing/0007`, `purchasing/0008`.
- **Admin:**
  - New sidebar item ZATCA → **VAT returns** (`/admin/zatca/vat`), with a period picker (◀ select ▶), dates, frequency, due date, days left / overdue, status, TEST label and a setup prompt.
  - New "VAT filing" card at the bottom of Business details.
  - New translation namespace `vat` (en/ar).
- **Tests:** `vat_returns/tests.py` (12).
- **Different from the plan:**
  - **The first period starts on an explicit "Track VAT returns from" date** (and the registration date), not on the date ZATCA was enabled. `enabled_at` resets whenever ZATCA is switched off and on, and a restaurant registered years ago would otherwise get every quarter since then.
  - **No `previous` link between periods:** the previous period is simply the one before by date.
  - **`claim_date` instead of a `claim_period` link:** a date survives switching environment (Sandbox → Production) and switching frequency, where a link to a period row would not.
  - **The frequency-switch "effective from" rule moves to M9.** Until a period is filed, changing the frequency or the dates simply rebuilds all periods.

### M4: what was built
- **Figures service** `vat_returns/figures.py`: every card, box and list comes from it, from stored figures only.
  - **Sales:** POS ZATCA documents (not self-tests) of the period's environment, issued inside the period in **Saudi time** (midnight Riyadh is the boundary, not UTC). Reported + Cleared count: invoices and debit notes add, credit notes subtract. Pending and Rejected are listed separately ("not counted").
  - **Purchases:** supplier documents of non-deleted purchases whose `claim_date` is in the period; adds the stored `claimable_vat`. Unclaimed ones (supplier not registered / no VAT number) are "needing review".
  - **Credit carried in:** worked out from the periods before (`credit_source: "estimated"`); the first period has none. In M5 the ZATCA-filed amount replaces the estimate.
  - **VAT due** = output − input − credit in (negative = credit). **Paid** is 0 until M6. **Balance** = max(due, 0) − paid.
  - Takes a list of branches, for adding branches together later.
- **Return boxes** `vat_returns/boxes.py`: boxes 1–16 in the form's order. Sales split by VAT category from the M1 breakdown (S→1, Z→3, E→5); purchases by the M2 category (standard→7, zero→10, exempt→11), claimable documents only. Invoices go in **Amount**, debit/credit notes in **Adjustment** (A1). 6/12 = totals, 13 = 6 − 12, 14 = 0 (not tracked), 15 = credit in, 16 = 13 + 14 − 15. Boxes 2, 4, 8, 9, 14 are shown as 0 "not recorded by this system".
  - Documents whose breakdown couldn't be backfilled in M1 are counted in the cards but not the boxes; the page warns with their number.
- **Endpoints:**
  - `GET /api/vat/periods/<id>/` now includes `figures` (cards, boxes, not_counted, review, missing_breakdown).
  - `GET /api/vat/periods/<id>/documents/?list=output|not_counted|input|review&page=N`: 100 a page, signed amounts, totals over the whole list (equal to the card).
  - `GET /api/zatca/documents/<id>/xml/?which=signed|cleared`: downloads the XML (owner/manager, own branch; 404 when there is no cleared XML).
- **Admin:** VAT returns page shows 6 cards (VAT due shows its formula with the backend's numbers), a warnings box (not counted, needing review, missing breakdown), the boxes table with a copy button per figure (2 decimals, as typed in the portal), and a "Show documents" panel with load more, XML downloads and links to purchases. Shared `src/lib/api/download.ts` (the input VAT CSV now uses it too).
- **Tests:** `vat_returns/test_figures.py` (13): cards, boxes, Saudi-midnight boundary, credit carried, missing breakdown, supplier edits not changing figures, lists add up to cards, paging, access, XML download.
- **No migrations** in M4.
- **Test fix in M5:** the M4 tests patched "today" on the test methods only, not on `setUp`, which builds the periods. They now share a `VatFixture` that patches it for the whole test.

### M5: what was built
- **Models** (`vat_returns/0002_filing_lock_override`):
  - `VatFiling`: filed_on, zatca_amount, a snapshot of every figure (`calculated`, plus the IDs of the documents counted), calculated_net_due, difference + reason, credit_treatment, attachment, the warnings filed anyway, filed_by. One current filing per period (partial unique); `supersedes` is ready for amendments (M9).
  - `VatLockOverride`: period, action, object, before/after figures, reason, actor.
  - `VatAuditLog.data` now uses Django's JSON encoder (amounts and dates).
  - All read-only in Django admin.
- **Checklist** (`vat_returns/filing.py`), warnings only: pending / rejected ZATCA documents, received purchases without a supplier invoice, supplier documents with warnings (no QR, QR unreadable/mismatch, supplier VAT missing, not registered, VAT not 15%, totals don't add), documents without VAT split, previous period not filed, period not ended.
- **Mark as filed:** `POST /api/vat/periods/<id>/file/` (owner only).
  - Checks: filed_on not in the future and not before the period; ZATCA amount at most 2 decimals; a reason when it differs from ours; credit treatment when negative (default carry forward); https attachment.
  - Open warnings → `409 error.vatChecklistOpen` with the list, until `confirm_warnings: true`.
  - Filing a second time → `409 error.vatPeriodAlreadyFiled`.
  - It locks the period and sets the status (Filed, or Credit when ZATCA's amount is 0 or less). Audited as `filed`.
- **After filing:**
  - The VAT due card shows ZATCA's amount, with our figure and the difference. The balance is measured against ZATCA's amount.
  - The next period's credit carried in is ZATCA's confirmed credit (`credit_source: "filed"`), or 0 when a refund was requested.
  - "Changed since filing" compares the live figures and document IDs with the snapshot (e.g. a pending sale accepted later, or an override).
- **Locks** (`vat_returns/locks.py`):
  - Guarded: creating, changing, moving out of, or deleting a supplier document whose claim date is in a filed period → `409 error.vatPeriodLocked` (with the period dates).
  - The owner can send `lock_override_reason` (at least 10 characters). The change is then saved with a `VatLockOverride` and an audit entry in the same transaction. Anyone else gets `403 error.vatOverrideOwnerOnly`.
  - `GET /api/vat/periods/<id>/audit/` lists the period's activity.
- **Admin:**
  - Checklist card with Fix links and a "Mark as filed" button (owner). The dialog shows ZATCA amount vs ours, a reason field when they differ, the credit choice, a PDF attachment, and the open warnings with "File anyway".
  - Filed summary with "changed since filing", and an activity list.
  - Supplier tax documents: on a locked period, the owner is asked for a reason and the change is sent again with it (`useLockOverride`).
- **Tests:** `vat_returns/test_filing.py` (16).
- **Different from the plan:**
  - **The guard is inside `SupplierDocument.save()`/`delete()`** instead of view checks plus signals. One place covers the API, Django admin (locked documents are read-only there) and the shell. A bulk `queryset.delete()` would get round it; no code does that, and deleting a purchase with supplier documents is already refused.
  - **Locks follow the branch's current environment.** Filing a Sandbox practice period doesn't lock real supplier documents once in Production.
  - **Sales are never blocked:** `issue()` has no lock check. The POS dates sales "now", so a POS sale is never blocked, even if a period was filed before its end. Late changes show as "changed since filing".
  - **Fixing a supplier's VAT number leaves documents in filed periods as filed.** Their claimable VAT doesn't move into a filed return.
  - **Late supplier invoices** for a filed period currently need an override. Claiming them in the oldest open period by default is M9 (§7.2).
  - **Purchase edits aren't guarded:** they don't change a supplier document's stored figures.
  - **Not in M5:** box 14 corrections and the opening credit on the first period (§7.5). Both are moved to M9.

### M6: what was built
- **Models** (`vat_returns/0003_payment_refund`): `VatPayment` (amount > 0, paid_on, SADAD reference, receipt, `source` manual/bank_feed, `bank_transaction_id` unique when set) and `VatRefund` (amount, received_on, reference, attachment). Both are voided, never deleted (voided reason/by/at), and read-only in Django admin.
- **Endpoints (owner only):**
  - `POST /api/vat/periods/<id>/payments/`: only after filing, and only when ZATCA's amount is to pay. Otherwise `409 error.vatPeriodNotFiled` / `error.vatNothingToPay`.
  - `POST /api/vat/periods/<id>/refunds/`: only when the filing asked for a refund (`409 error.vatNoRefundRequested`).
  - `POST /api/vat/payments/<id>/void/` and `/api/vat/refunds/<id>/void/` with a reason of at least 10 characters.
  - Each returns the period and is audited (`payment_recorded`, `payment_voided`, `refund_recorded`, `refund_voided`).
- **Status** (`filing.refresh_status`), set after filing, a payment or a void:
  - Not filed.
  - Filed: ZATCA amount > 0 and payments less than that.
  - Paid: payments reach ZATCA's amount.
  - Credit: ZATCA amount 0 or less.
  - Voiding a payment can move Paid back to Filed.
- **Figures:**
  - Paid = non-voided payments. Balance = max(ZATCA amount, 0) − paid; it can go negative when overpaid.
  - A refund-requested period shows requested / received / outstanding and "Refund requested" → "Refund received".
  - Credit carried forward uses ZATCA's amount (from M5).
- **Admin:** a Payments card (summary, list with voided ones struck through, Record payment with PDF receipt, Void with reason) or a Refund card in its place. The activity list reloads after each change.
- **Tests:** `vat_returns/test_payments.py` (10).
- **Decisions:**
  - **Overpayment is allowed** and shown as "Overpaid": the owner records what the bank actually did.
  - **A payment can't be dated before the period starts; a refund can't be dated before the filing date.**

### M7: what was built
- **History** (`vat_returns/history.py`): one row per period of the current environment, newest first. Each row has sales VAT, purchase VAT, credit b/f, VAT due (ours), ZATCA amount, paid, balance, filed on, SADAD references (not voided), status, refund state and overdue.
  - Worked out in one pass with the same credit rule as the cards; a test checks every row against its period's cards.
  - In Production only Production periods are listed; practice periods stay in the database.
- **Endpoints** (owner and manager; every export is audited):
  - `GET /api/vat/history/`.
  - `GET /api/vat/history/csv/`: UTF-8 with BOM, the header block (restaurant EN / AR, VAT number, report, generated time, TEST environment line when not in Production), oldest period first, and a TOTALS row for sales VAT, purchase VAT and paid.
  - `GET /api/vat/history/pdf/`: a landscape summary table.
  - `GET /api/vat/periods/<id>/pdf/`: one period, with summary, the 16 boxes, filing, changed since filing, and payments or refunds (voided ones with their reason).
- **Admin:** VAT returns has Period / History tabs (`?tab=history`). History has a table with status, refund and overdue pills, links to each period, and Export CSV / Export PDF. The period view has a Download PDF button.
- **Tests:** `vat_returns/test_history.py` (7).
- **Limits:**
  - **The PDFs are English only.** Reportlab's built-in fonts have no Arabic glyphs, so the restaurant's English name is used. Arabic typed into a reason or reference would print as empty boxes. The proper fix (Arabic font + `arabic-reshaper` + `python-bidi`) is already planned for M10's invoice PDFs. Both will use it then.
  - The CSV has both names and opens correctly in Excel.
  - **The TOTALS row has no total for VAT due or balance.** Adding those up across periods would count each credit twice (it is carried into the next period).

### M8: what was built
- **`GET /api/vat/reminders/`** (owner and manager; `vat_returns/reminders.py`): periods of the current environment that are Not filed, or Filed but not fully paid, and due within `vat_reminder_days` (due soon) or past their due date (overdue).
  - Each has its name, due date, days left, level, status and the balance still to pay (from the same figures as History).
  - Worked out per request, with no scheduler. A quick query finds the candidates first, so most requests do almost nothing.
- **Production only** (plan §7.8). Otherwise Sandbox practice periods, never filed for real, would show red forever.
- **Admin:** `VatReminderBanner` at the top of every admin page (not on full-screen POS/KDS), for owner and manager.
  - Amber: "Q3 2026 VAT return is due on 31 Oct (2 days left). Not filed yet." / "Filed; balance X to pay."
  - Red once overdue.
  - Links to the period.
  - Cached for 5 minutes. The VAT returns page refreshes it after a filing, payment or void.
- **Tests:** `vat_returns/test_reminders.py` (7). The shared test fixture now takes the sale's environment from its device, so Production sales can be made.
- **To see it in the app** you need Production. In Sandbox the banner is hidden on purpose; the tests cover it.

### M10: what was built
- **Archive queries** (`vat_returns/archive.py`), read only:
  - Sales: POS `ZatcaDocument`s (no self-tests) of the current environment. Purchases: `SupplierDocument`s of non-deleted purchases.
  - Filters: dates (sales in Saudi time), VAT period (sales: the period window; purchases: `claim_date`, the same as the VAT returns page), type, ZATCA status, B2C/B2B, customer, supplier, number, total range, free text (number, customer/supplier name, VAT number, order number).
  - 50 a page. Count and signed totals over the whole filtered list (credit notes negative).
- **Tax invoice PDF** (`vat_returns/invoice_pdf.py`): A4, English + Arabic, drawn from the `receipt` snapshot frozen at issue, with the QR where the receipt prints one. Arabic needs `common/pdf_fonts.py`: the Amiri font in `common/fonts/` plus `arabic-reshaper` and `python-bidi`. Without them the PDF endpoint answers `503 error.archivePdfUnavailable`, and the ZIP lists the PDF as "missing (why)".
- **ZIP** (`vat_returns/archive_zip.py`, model `VatArchiveExport`, migration `vat_returns/0005_archive_export`):
  - Stores the filter, freezes the matching IDs when the build starts, and writes the ZIP to disk entry by entry.
  - Contents: `index.csv` (BOM, header block, TOTALS per side), `sales/<number>.pdf/.xml/_cleared.xml`, `purchases/<supplier>_<number>.pdf/.xml`. Names are made safe (Arabic kept, duplicates get `_2`). A file that can't be included is written as `missing (why)`, never skipped.
  - Built by a daemon thread right after the request. `manage.py vat_archive_exports` (every 5 minutes, like `zatca_retry_queue`) builds anything still queued, re-queues a build stuck for 30 minutes, and removes ZIPs 7 days after they were built (the row stays, as Expired).
  - Saved under `VAT_ARCHIVE_DIR` (default `private/vat_archives/`, git-ignored, never public media).
  - Audited in `VatAuditLog`: requested, ready / failed, every download.
- **Endpoints** (owner and manager; cashier 403):
  - `GET /api/vat/archive/sales/` and `/purchases/` (filters + `page`).
  - `GET /api/vat/archive/sales/<id>/pdf/`, `GET /api/vat/archive/purchases/<id>/xml/`. The sales XML uses the M4 endpoint `/api/zatca/documents/<id>/xml/`; a supplier PDF is its stored `pdf_url`.
  - `GET`/`POST /api/vat/archive/exports/`, `GET /api/vat/archive/exports/<id>/`, `GET /api/vat/archive/exports/<id>/download/` (409 until ready).
- **Admin:**
  - New sidebar item ZATCA → **Invoice archive** (`/admin/zatca/archive`). Sales / Purchases tabs, filters in the URL, totals, view drawer, PDF / XML per row, "Create ZIP" for the filtered list, and a ZIP downloads card that refreshes itself while one is being prepared.
  - VAT returns: **Download all invoices for this period** asks for a ZIP of both sides for the period and opens the archive on it.
  - Downloads now read the UTF-8 `filename*`, so Arabic file names come through.
- **Tests:** `vat_returns/test_archive.py` (18). The real-PDF test is skipped until the font files are in `common/fonts/`.
- **Different from the plan:**
  - The customer filter is the free-text search (name or VAT number). The backend also takes `customer=<id>`, but the page has no company picker yet.
  - The history and period PDFs (M7) still use the built-in English font. Switching them to Amiri is a small follow-up now that `common/pdf_fonts.py` exists.

### M11a: what was built (business expenses: model, rules, page, locks)
- **Model** `purchasing.Expense` (migration `purchasing/0009_expense`): branch, supplier (from the Suppliers list), invoice / credit note, number, date, category (rent, utilities, equipment, maintenance, services, marketing, other), description, net / VAT / total as printed, VAT category, supplier VAT snapshot, Not claimable + reason + note, stored `claimable_vat`, capital asset, `claim_date`, QR + check, attachment, void fields. Never deleted.
- **Rules** are the supplier documents' (`purchasing/vat.py`, shared `ClaimDateMixin`):
  - Claimable = 0 when the supplier isn't VAT-registered, has no VAT number, the expense is Not claimable, or it is voided. Credit notes negative.
  - Flags (`purchasing/expenses.py`): the supplier-document flags plus `notClaimable`, `noAttachment`, `possibleDuplicate` (same supplier + number + kind as another non-voided expense or supplier document). Warnings only.
  - `why_not_claimed` tells the form why the claimable VAT is 0.
  - Capital asset is ticked automatically for equipment when it isn't sent; it can be changed.
  - Editing a supplier re-copies its VAT details onto its expenses too, except in filed periods.
- **Locks** (`vat_returns/locks.py`): the guard is generic (`guard_tax_document`). Creating, changing, moving out of or voiding an expense in a filed period → `409 error.vatPeriodLocked`; the owner overrides with a reason → `VatLockOverride` (`object_type = purchasing.Expense`, new action `void`, migration `vat_returns/0006`). A late expense is claimed in the oldest open period, like supplier invoices.
- **API** (owner and manager; cashier 403): `GET/POST /api/expenses/`, `GET/PATCH /api/expenses/<id>/`, `POST /api/expenses/<id>/void/` (reason ≥ 10 characters, audit-logged). No PUT or DELETE (405). Filters: `from`, `to`, `category`, `supplier`, `claim=claimable|not_claimable|review`, `capital_asset=true`, `voided=true`, `search`, `ordering`.
- **Uploads:** new signature kind `expense` (PDF, JPG, PNG; Cloudinary folder `restaurant/expenses`).
- **Django admin:** Expense list; read-only once voided or in a filed period; no delete.
- **Admin page:** Inventory → **Expenses** (`/admin/inventory/expenses`): filters, list (voided rows struck through), add / edit form with supplier picker + "New supplier" inline, QR check, attachment, Not claimable + reason, Capital asset; the stored claimable VAT and why it is 0 are shown; Void with a reason; lock overrides through the same dialog as supplier documents.
- **Tests:** `purchasing/test_expenses.py` (18).
- **Different from the plan:**
  - Two more not-claimable reasons: "Passenger car (non-commercial)" and "Staff personal benefits" (you can remove them; accountant question A10).
  - The inline "New supplier" needs one name, like the Suppliers page.
  - A supplier with expenses can't be deleted (409 `error.supplierInUse`, message updated).

### M11b: what was built (expenses in the VAT figures)
- **Figures** (`vat_returns/figures.py`): input VAT = supplier documents + non-voided expenses claimed in the period. New card fields `input_vat_purchases` and `input_vat_expenses` (input_vat is the two added up). New `not_claimable` {count, VAT} for information.
- **Boxes 7 / 10 / 11:** claimable expenses by VAT category, like supplier documents. Not-claimable expenses are left out entirely (A9 default).
- **Show documents:** new lists `expenses` and `not_claimable`; `review` now lists supplier documents and expenses together. Rows carry `source` (purchase / expense).
- **Checklist:** "Supplier documents and expenses with warnings" counts expenses; new item `expensesWithoutAttachment`.
- **Filing snapshot** stores the expense IDs; "changed since filing" covers them. Filings made before M11b have no expense list, so any expense later added to such a period shows as added.
- **Period PDF:** the input VAT line shows "from purchase orders" and "from expenses".
- **Input VAT report and CSV:** expenses next to supplier documents, by date. New CSV columns Source, Expense category, Capital asset, Not claimable reason; Purchase is blank for expenses; the header says "supplier invoices and expenses, less credit notes".
- **Invoice archive:** third side `expenses` (filters as Purchases + `category`, `capital_asset`; voided expenses left out). ZIP folder `expenses/<supplier>_<number>.pdf|jpg|png`, `missing (why)` for no or failed attachment, and three new `index.csv` columns. The export's `expenses_count` comes from the frozen IDs (no new column). A ZIP request without `sides` now takes all three; "Download all invoices for this period" asks for all three.
- **Admin:** VAT returns input VAT card shows both lines, each with Show documents, and a note for not-claimable VAT; Input VAT page shows source, category, capital asset and not-claimable reason; Invoice archive has an Expenses tab.
- **Tests:** `vat_returns/test_expense_figures.py` (7); `test_figures`, `test_filing` and `purchasing/test_input_vat` updated for the new card fields, checklist item and CSV columns.

---

## Decisions (VAT dashboard, approved 2026-10-29)

- Sandbox and Simulation periods are shown, labelled TEST, and hidden from history once in Production. Never deleted.
- Per branch now; the figures service accepts a list of branches, for adding them together later.
- "Refund received" (amount, date, reference, attachment) after "Refund requested".
- Supplier documents with VAT 0 default to zero-rated; the owner can change it.
- History PDF: a summary table, plus an optional per-period PDF with the boxes.
- Credit notes count in the period they are issued. Late supplier invoices for a filed period are claimed in the oldest open period, unless the owner overrides.
- The ZATCA-confirmed amount wins over our calculated amount once filed. A difference needs a reason.
- The invoice archive is added as M10.

## Open items

- **Box names** in `vat-dashboard-plan.md` §4.5. To check against the live ZATCA portal: boxes 2, 8, 9, 14, 15, 16, and whether a 5% line still exists under box 1.
- **Arabic in VAT PDFs (M7):** English only until the M10 PDF dependencies are in; then the history and period PDFs switch to the Arabic font too.
- **M10 on the VPS:** `pip install -r requirements.txt` (`arabic-reshaper`, `python-bidi`), the Amiri font files in `common/fonts/`, and a scheduled `manage.py vat_archive_exports` every 5 minutes. The QR uses reportlab's own widget (no extra library).
- **M4 scope:** per-document XML download was moved earlier from M10 (you said "move to next" on the M4 plan that listed it). It is a small read-only endpoint; say if you'd rather drop it.
- **Follow-up, not in scope:** backup of attachments (Cloudinary filing and SADAD PDFs) for the 6-year retention.
- **ZATCA M10:** Simulation run with a real company, unreported-invoice banner, this runbook's go-live section.

## To confirm with accountant

Defaults are in use until answered.

| # | Question | Default we use |
|---|---|---|
| A1 | Credit and debit notes in the return's **Adjustment** column, or netted into Amount? | Adjustment column |
| A2 | Late supplier invoices: claim in the next open period instead of amending? | Next open period |
| A3 | Box 14 corrections limit (±5,000) and when a voluntary disclosure is needed instead | ±5,000 in box 14; above that, flagged for disclosure |
| A4 | Due date on a weekend or holiday: does it move to the next working day? | No, last day of the following month |
| A5 | Purchases from non-registered suppliers: box 11 (exempt) or left out (out of scope)? | Left out |
| A6 | Mixed-rate supplier invoices (15% + 0% on one invoice): is one net/VAT per document enough? | One net/VAT per document |
| A7 | Pre-registration input VAT and the first (short) period: any special treatment? | None |
| A8 | Confirm the return box names and numbering (§4.5) | As in the plan |
| A9 | Not-claimable (blocked) expense VAT: left out of box 7, or net in box 7 with VAT 0? | Left out |
| A10 | Which expense types are blocked in KSA (entertainment, personal use, some vehicles…)? | The reasons list is a starting point |
| A11 | Capital assets: is the tick enough, or is an asset register needed? | The tick only |
