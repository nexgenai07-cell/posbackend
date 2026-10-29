# VAT Dashboard: plan (no code yet)

Status: **approved 2026-10-29** with the decisions below. Progress is tracked in `docs/zatca-progress.md`.

### Decisions on approval (these win over anything below)
- Sandbox and Simulation periods are shown, labelled TEST, and hidden from history once in Production. They are never deleted (Q1).
- Per branch now. `figures.py` takes a list of branches, so adding branches together later needs no rewrite (Q2).
- "Refund received" (amount, date, reference, attachment) follows "Refund requested" (Q3). Model: `VatRefund` (§3.3a).
- A supplier document with VAT 0 defaults to zero-rated; the owner can change it (Q4).
- History PDF: a summary table, plus an optional per-period PDF with the boxes (Q5).
- Attachment backup is out of scope and listed as a follow-up (Q6).
- Accountant questions: proposed defaults are used. The list lives in the runbook (`docs/zatca-progress.md`), marked "to confirm with accountant".
- Build order: **M1 → M6, stop after each.** M7–M9 come after you've tested M6. Then M10 (Invoice archive).
- I don't run tests, migrations or other commands. Each milestone ends with the exact commands to run on your local DB and the expected output. A VPS checklist (incl. `pg_dump` of `posdb` on port 5433 before migrating) comes when the whole feature is done.

Read before writing this: `docs/ZATCA_Approval_Prompt.md`, plus the current code in `zatca/`, `purchasing/`,
`accounts/permissions.py` and the admin's `src/pages/admin/zatca/*` and `InputVatReportPage.tsx`.
**`docs/zatca-progress.md` does not exist** in either repo (I searched the whole workspace). If it is somewhere
else, send it and I'll check this plan against it.

---

## 0. What the code has today (what this plan builds on)

| Area | What exists | What it means for this plan |
|---|---|---|
| `zatca.ZatcaDocument` | `kind` (invoice / credit_note / debit_note), `simplified`, `issued_at`, `total` (payable), `vat_total`, `status` (pending / reported / cleared / rejected), `environment`, `source` ("pos" / "selftest"). Never deleted (PROTECT, not a BaseModel). | Good source for output VAT. **But it does not store net (taxable) amounts or the per-category split** (standard / zero / exempt). These are only in `signed_xml` (`cac:TaxSubtotal`) and partly in `receipt["totals"]`. The return boxes need them, so we add stored fields (M1). |
| `zatca.ZatcaSettings` | Business details, one per branch, `timezone = Asia/Riyadh`. | Add the filing frequency and the VAT registration date here, so they appear on the Business details page. |
| `zatca.ZatcaAuditLog` | Onboarding audit. | Kept for onboarding. VAT gets its own audit log with a period link and before/after data. |
| `purchasing.SupplierDocument` | Supplier invoice (at most one per purchase) or credit note: `issue_date`, `net`, `vat`, `total`, QR check, `pdf_url`. Not a BaseModel, but **can be hard-deleted** through the API. | Source for input VAT. Needs: a stored claimable amount, a supplier VAT snapshot, the claim period, and lock checks. |
| `purchasing/vat.py` | `claimable_vat()` returns 0 only for non-registered suppliers. Flags: noInvoice, noQr, qrUnreadable, qrMismatch, notRegistered, supplierVatMissing, totalsDontAdd, netDiffersFromPurchase. | Fixes in M2: claimable = 0 also when supplier VAT is missing; new flag "VAT not 15%". |
| Input VAT report | `GET /api/purchases/input-vat/` plus `InputVatReportPage.tsx`. The CSV is built in the browser by `lib/csv.ts`: no BOM, flags joined with a space, no header and no totals. | CSV moves to the server (M2). |
| Roles | `IsOwner`, `IsOwnerOrManager`; cashier can't open `/admin`. | Reused: read = owner or manager, write = owner. |
| Attachments | Cloudinary signed direct upload (`/api/uploads/signature/`), stored as a URL (like `SupplierDocument.pdf_url`). | Reused for the filing confirmation and SADAD receipts. |
| PDF | `reportlab` (`purchasing/pdf.py`). | Reused for the history PDF. |

Two problems in today's code that affect VAT figures (fixed in this plan):
1. **Soft-deleted purchases still count.** `input-vat` filters `SupplierDocument` by `purchase__branch`, and that join does not exclude `is_deleted` purchases. A deleted PO's supplier invoice is still claimed. (Fix in M2: deleting a purchase that has supplier documents is blocked until they are removed, and removal follows the lock rules.)
2. **Changing a supplier rewrites history.** Claimable VAT is worked out from `supplier.vat_registered` and `supplier.vat_number` *now*. Editing a supplier changes the input VAT of past, filed periods. (Fix in M2: snapshot both on the document when it is saved.)

---

## 1. Where it goes and what it looks like

### Menu
Sidebar, **ZATCA** group (owner and manager only, like today), new first item:

```
ZATCA
  ▸ VAT returns        /admin/zatca/vat          ← new (icon: Landmark or Receipt)
    Business details   /admin/zatca/business     (+ new "VAT filing" section)
    Credentials
    Invoices
    Go-live
INVENTORY
    Input VAT          /admin/inventory/input-vat (fixed, see §6)
```

Routes:
- `/admin/zatca/vat` is the dashboard for the current or selected period (`?period=<id>`).
- `/admin/zatca/vat/history` is the history table. It could also be a tab on the same page; I recommend a tab.

A cashier never gets the link, and the API refuses them (403) too.

### Layout (selected period)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ [TEST MODE banner if not Production]                                        │
│ VAT returns                                   Period: [◀ Q3 2026 (Jul–Sep) ▶]│
│ Quarterly · Due 31 Oct 2026 · 2 days left · Status: ● Not filed             │
├─────────────────────────────────────────────────────────────────────────────┤
│ ┌VAT on sales─┐ ┌VAT on purch.┐ ┌Credit b/f───┐ ┌VAT due──────┐ ┌Paid──┐ ┌Balance┐│
│ │ 12,450.30   │ │  3,210.15   │ │    0.00     │ │  9,240.15   │ │ 0.00 │ │9,240.15││
│ │Show docs →  │ │Show docs →  │ │From Q2 →    │ │= a − b − c  │ │Show →│ │due−paid││
│ └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘ └──────┘ └───────┘│
│  Not counted: 3 pending · 1 rejected ZATCA documents  [Show]                 │
├──────────────────────────────────────┬──────────────────────────────────────┤
│ VAT return boxes (copy into portal)  │ Ready to file                         │
│  # Box            Amount Adj.  VAT   │  ⚠ 3 ZATCA documents pending  [Show]  │
│  1 Standard rated sales ...  [copy]  │  ⚠ 1 ZATCA document rejected  [Show]  │
│  3 Zero rated domestic sales         │  ⚠ 2 received POs without invoice     │
│  5 Exempt sales                      │  ⚠ 4 supplier docs need review        │
│  6 Total sales                       │  ✓ Period has ended                   │
│  7 Standard rated domestic purchases │                                       │
│ 10 Zero rated purchases              │  [Mark as filed]   (owner only)       │
│ 11 Exempt purchases                  │                                       │
│ 12 Total purchases                   │ Payments                              │
│ 13 Total VAT due for current period  │  12 Oct  5,000.00  SADAD 1234…  📎    │
│ 15 VAT credit carried forward        │  [Record payment]  (owner only)       │
│ 16 Net VAT due                       │                                       │
├──────────────────────────────────────┴──────────────────────────────────────┤
│ Filing: Filed 28 Oct 2026 by Owner · ZATCA amount 9,240.15 ✓ matches · 📎   │
│ Activity (audit log for this period)                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

**History tab:** one row per period, newest first: Period, Frequency, Sales VAT, Purchase VAT, Credit b/f,
VAT due, Paid, Balance, Filed date, SADAD ref(s), Status. Buttons: **Export CSV** and **Export PDF**.

**"Show documents"** opens a side drawer (or a filtered list) with exactly the rows behind that number: number,
date, type, B2B/B2C, net, VAT, and a link to the document. Every total in the drawer equals the card. The
"VAT due" card shows its formula on screen (`= 12,450.30 − 3,210.15 − 0.00`), so nothing is computed out of sight.

**Dashboard banner** (`/admin` home and the top of every admin page, owner and manager):
- amber, from 14 days before the due date while the period is not filed or not fully paid: "Q3 2026 VAT return is due on 31 Oct (12 days). Not filed yet." with a link.
- red, after the due date: "Q3 2026 VAT return is overdue since 31 Oct. Balance 9,240.15 SAR."

---

## 2. Settings (Business details page, new "VAT filing" section)

New fields on `ZatcaSettings` (owner-editable):

| Field | Type | Default | Notes |
|---|---|---|---|
| `vat_filing_frequency` | `quarterly` / `monthly` | `quarterly` | A change applies only from a period that hasn't started yet (§7.3). |
| `vat_frequency_effective_from` | date, nullable | null | First day of the first period under the new frequency. Must be a quarter start. |
| `vat_registration_date` | date, nullable | null | Effective date on the VAT certificate. The first period starts here (§7.4). |
| `vat_reminder_days` | int | 14 | Days before the due date when the banner starts. |

Changing them writes a VAT audit entry.

---

## 3. New models and fields

New Django app **`vat_returns`** (it keeps `zatca/models.py` from growing and keeps sending invoices separate from
filing returns). None of these are BaseModels, none are ever deleted, and every FK to them is `PROTECT`.

### 3.1 `VatPeriod`
One tax period of one branch. Created automatically (lazily, when the dashboard or reminder job looks) for every
period from the first period up to the current one. No gaps, no overlaps.

| Field | Notes |
|---|---|
| `branch` | FK Branch, PROTECT |
| `environment` | sandbox / simulation / production. Periods in a test environment are **practice periods** (TEST label). See §8 Q1. |
| `frequency` | monthly / quarterly, frozen when created |
| `start_date`, `end_date` | dates in `ZatcaSettings.timezone` (Asia/Riyadh), inclusive |
| `due_date` | last day of the month after `end_date` (stored, so a later rule change doesn't rewrite history) |
| `status` | `open` → `filed` → `paid`, or `credit` (§4.6). Kept up to date by the service; shown, never computed in the UI. |
| `locked` | bool; true once filed |
| `previous` | FK self, nullable: the period before, for credit carried in |
| constraints | unique (branch, environment, start_date); check start ≤ end |

### 3.2 `VatFiling`
A submitted return. Normally one per period. An **amended return** adds a new row with `supersedes` pointing to the
old one. Nothing is overwritten.

| Field | Notes |
|---|---|
| `period` | FK, PROTECT |
| `filed_on` | date the owner submitted on the portal |
| `zatca_amount` | the net VAT due ZATCA confirmed (SMS/email). Negative = credit. |
| `calculated` | JSON **snapshot** of every card and box at filing time, plus the IDs of the documents counted |
| `calculated_net_due` | decimal (copied from the snapshot so it can be queried) |
| `difference` | `zatca_amount − calculated_net_due`, stored |
| `difference_reason` | required when difference ≠ 0 (the warning becomes "explain why") |
| `credit_treatment` | `carry_forward` (default) / `refund_requested`, only when the amount is negative |
| `attachment_url`, `attachment_name` | optional (Cloudinary) |
| `filed_by`, `created_at` | |
| `supersedes` | FK self, nullable (amendment) |
| `is_current` | one current filing per period (partial unique constraint) |

### 3.3 `VatPayment`
| Field | Notes |
|---|---|
| `period` | FK, PROTECT |
| `amount` | > 0 |
| `paid_on` | date |
| `sadad_reference` | text; SADAD bill number or bank reference |
| `receipt_url`, `receipt_name` | optional PDF |
| `source` | `manual` now; later `bank_feed` |
| `bank_transaction_id` | blank now; unique when set (the future bank feed's dedupe key) |
| `voided`, `voided_reason`, `voided_by`, `voided_at` | **a wrong payment is voided, never deleted** |
| `recorded_by`, `created_at` | |

### 3.3a `VatRefund` (approved Q3)
Used when a filing's `credit_treatment = refund_requested`. Fields: `period` (FK, PROTECT), `amount`, `received_on`,
`reference`, `attachment_url`, `attachment_name`, `voided` + reason/by/at (never deleted), `recorded_by`,
`created_at`. The period shows "Refund requested" until the non-voided refunds add up to the requested credit, then
"Refund received".

*Bank feed later:* a separate `BankTransaction` model plus a matcher that proposes `VatPayment(source=bank_feed)`
for the owner to confirm. Nothing in this plan has to change for that. Not built now.

### 3.4 `VatLockOverride`
Every change to a locked period that the owner pushed through.

| Field | Notes |
|---|---|
| `period` | FK |
| `action` | create / update / delete |
| `object_type`, `object_id` | e.g. `purchasing.SupplierDocument`, 57 |
| `before`, `after` | JSON of the stored figures (net, VAT, date, number) |
| `reason` | required, at least 10 characters |
| `actor`, `created_at` | |

### 3.5 `VatAuditLog`
Every VAT action: period created, settings changed, filed, amended, payment recorded or voided, override, export,
attachment added. Fields: `branch`, `period` (nullable), `actor`, `action`, `summary`, `data` (JSON), `created_at`.
Read-only in Django admin.

### 3.6 Changes to existing models

**`zatca.ZatcaDocument`** (stored figures for the return boxes):
- `net_total` decimal: TaxExclusiveAmount
- `vat_breakdown` JSON: `[{category: "S"|"Z"|"E"|"O", rate, taxable, vat}]`, straight from `VatResult.categories`, the same numbers written into the XML
- Filled in `documents.issue()` from now on. **Data migration** backfills existing rows by reading `cac:TaxTotal/cac:TaxSubtotal` and `cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount` from the stored `signed_xml`. That reads stored figures, it doesn't recalculate. A row whose XML can't be parsed is reported by the migration and left null, and the dashboard flags it.

**`purchasing.SupplierDocument`:**
- `supplier_vat_registered` (bool) and `supplier_vat_number` (str): snapshot at save
- `vat_category`: `standard` / `zero` / `exempt` (default: `standard` if VAT > 0, otherwise `zero`; owner can change). Needed to split boxes 10 and 11.
- `claimable_vat` decimal, stored: signed (negative for credit notes), 0 when not claimable (§6)
- `flags` JSON, stored at save (the same codes the report shows)
- `claim_period` FK `VatPeriod`, nullable: which return claims it (§7.2)
- Deletion: only through the API, only in an open period or with an override. Deleting a purchase that has supplier documents is refused.

**`zatca.ZatcaSettings`:** the fields in §2.

### 3.7 Migrations
1. `zatca/00xx_document_vat_breakdown`: add `net_total` and `vat_breakdown` (schema)
2. `zatca/00xx_backfill_vat_breakdown`: data migration from `signed_xml` (reversible no-op)
3. `zatca/00xx_settings_vat_filing`: frequency, effective-from, registration date, reminder days
4. `purchasing/00xx_supplierdocument_vat_snapshot`: snapshot fields, `vat_category`, `claimable_vat`, `flags`
5. `purchasing/00xx_backfill_supplierdocument_vat`: fills the snapshots from the supplier as it is now (this is the one time we must use today's supplier data, written down in the migration)
6. `vat_returns/0001_initial`: VatPeriod, VatFiling, VatPayment, VatLockOverride, VatAuditLog
7. `purchasing/00xx_supplierdocument_claim_period`: FK to VatPeriod (depends on 6)

As agreed, I'll write all of them and you run `migrate` once at the end.

---

## 4. How each number is calculated

Everything is a SUM over stored columns. No VAT is recalculated from prices. One service, `vat_returns/figures.py`,
works out every number. The cards, the boxes, the drawer lists, the CSV, the PDF and the filing snapshot all call
it, so they can't disagree.

### Period boundaries
`start = start_date 00:00 Asia/Riyadh`, `end = (end_date + 1 day) 00:00 Asia/Riyadh`, half-open.
Use `ZatcaSettings.timezone`, not `Branch.timezone`, because ZATCA's IssueDate is Saudi time.

### 4.1 Output VAT (VAT collected on sales)
Table: `zatca_zatcadocument`

```
WHERE branch = B
  AND environment = period.environment
  AND source = 'pos'                       -- selftest/compliance documents never count
  AND issued_at >= start AND issued_at < end
  AND status IN ('reported', 'cleared')
```

| Part | Formula |
|---|---|
| Invoices | `+ SUM(vat_total)` where kind = invoice |
| Debit notes | `+ SUM(vat_total)` where kind = debit_note |
| Credit notes | `− SUM(vat_total)` where kind = credit_note (stored positive, subtracted here) |
| **Output VAT** | invoices + debit notes − credit notes, B2B and B2C together (split shown in the drawer) |

Pending and rejected documents in the same window are **listed separately with their amounts and not counted**.

A credit note counts in **the period it was issued**, not the period of the original invoice. That is the KSA rule
for adjustments, and it's how the POS already dates them (`issued_at = now`). See §7.1.

### 4.2 Input VAT (VAT paid to suppliers, claimable)
Table: `purchasing_supplierdocument` joined with `purchasing_purchase` (`branch = B`, `is_deleted = false`)

```
WHERE claim_period = P
```

`Input VAT = SUM(claimable_vat)`. Credit notes are stored negative, so they reduce it automatically.

`claim_period` is set when the document is saved (§7.2). It is the period containing `issue_date` if that period is
still open, otherwise the oldest open period. So the dashboard never moves a figure in or out of a filed period.

Documents with `claimable_vat = 0` because of missing supplier VAT or a non-registered supplier appear in the
"VAT needing review" list, not in the total.

### 4.3 Credit carried from the previous period
```
prev = P.previous
if prev is None:                         credit_in = 0          (first period)
elif prev has a current filing:
    if filing.zatca_amount < 0 and credit_treatment == carry_forward:
                                         credit_in = −filing.zatca_amount   ← ZATCA's confirmed figure wins
    else:                                credit_in = 0
else (prev not filed yet):               credit_in = max(0, −net_due(prev))   shown as "estimated, Q2 not filed"
```

### 4.4 VAT due, paid, balance
```
VAT due   = output − input − credit_in        (can be negative → credit, §7.5)
Paid      = SUM(VatPayment.amount) WHERE period = P AND voided = false
Balance   = max(VAT due, 0) − Paid            (negative balance = overpaid, shown as such)
```
After filing, the cards show **ZATCA's confirmed amount** as "VAT due (ZATCA)", with our calculated amount and the
difference next to it. Payments and balance are measured against ZATCA's amount, because that is what the bank bill says.

### 4.5 Return boxes

Columns as on the portal: **Amount (SAR) · Adjustment (SAR) · VAT amount (SAR)**.
Mapping (my proposal, to confirm with the accountant, §9):
- **Amount** = invoices
- **Adjustment** = debit notes (+) and credit notes (−)
- **VAT** = VAT of both

Category comes from `vat_breakdown[].category`.

| # | Box (as I know it from the ZATCA VAT return form) | Source |
|---|---|---|
| 1 | Standard rated sales | ZATCA docs, category S: taxable / VAT |
| 2 | Private healthcare / private education / first house sales to citizens | 0 (not applicable to a restaurant), shown as 0 |
| 3 | Zero rated domestic sales | category Z |
| 4 | Exports | 0 (not applicable), shown as 0 |
| 5 | Exempt sales | category E |
| 6 | Total sales | 1 + 2 + 3 + 4 + 5 |
| 7 | Standard rated domestic purchases | supplier docs with vat_category standard, claimable: net / claimable VAT |
| 8 | Imports subject to VAT paid at customs | 0 (not tracked), shown as 0 |
| 9 | Imports subject to VAT accounted for through reverse charge mechanism | 0 (not tracked), shown as 0 |
| 10 | Zero rated purchases | supplier docs, vat_category zero |
| 11 | Exempt purchases | supplier docs, vat_category exempt |
| 12 | Total purchases | 7 + 8 + 9 + 10 + 11 |
| 13 | Total VAT due for current period | box 6 VAT − box 12 VAT |
| 14 | Corrections from previous period (between SAR ±5,000) | entered by the owner when needed (§7.6), default 0 |
| 15 | VAT credit carried forward from previous period(s) | credit_in (§4.3) |
| 16 | Net VAT due (or claimed) | 13 + 14 − 15 |

You asked for "zero-rated or exempt purchases" as one line. The form has them as two boxes (10 and 11), so I show
both plus your combined line on the card.

**Box names I'm not sure about** (check against today's portal screen before M4 is final):
- **Box 2.** Exact wording ("Private Healthcare / Private Education / First house sales to citizens"), and whether it is still one combined box.
- **Box 1 split.** After the July 2020 rate change the form showed "Standard rated sales 15%" and "5%" lines for transitional supplies. I believe the 5% line has gone; not sure.
- **Boxes 8 and 9.** Exact wording of the two import boxes.
- **Box 14.** The SAR 5,000 limit and exact title.
- **Box 15.** Whether the portal now pre-fills it from ZATCA's own ledger (it may not be editable).
- **Box 16.** Whether it's titled "Net VAT due (or reclaimed)" or "(or claimed)".
- **Arabic labels.** I'll take them from the portal; I won't make them up.

The box labels go in one config list (`vat_returns/boxes.py`), so renaming a box is a one-line change.

### 4.6 Status per period
| Status | When |
|---|---|
| Not filed | no current filing |
| Filed | filed, ZATCA amount > 0, payments < ZATCA amount |
| Paid | filed, payments ≥ ZATCA amount |
| Credit | filed, ZATCA amount ≤ 0 (nothing to pay) |
| *(badge)* Overdue | not filed or not paid and today > due_date |

---

## 5. Ready-to-file checklist (warnings only, never blocks)

| Check | Query |
|---|---|
| Pending or rejected ZATCA documents | same window as §4.1, `status IN (pending, rejected)`; count + list |
| Received purchases without a supplier invoice | `Purchase.status = received`, `received_at` in the period, no SupplierDocument kind = invoice, not deleted |
| Supplier documents needing review | `claim_period = P` and `flags` contains any of: supplierVatMissing, notRegistered, vatNot15, qrMismatch, qrUnreadable, noQr, totalsDontAdd |
| Period hasn't ended | today (Riyadh) ≤ end_date |
| *(extra)* Documents missing the VAT breakdown | ZATCA docs with `vat_breakdown IS NULL` (failed backfill) |

"Mark as filed" stays enabled. If any warning is open, the filing dialog lists them and asks for one more "File anyway" click.

---

## 6. Input VAT report fixes

1. **Claimable = 0** when the supplier is not VAT-registered **or** has no VAT number (snapshot values). These rows go to a **"VAT needing review"** card on the Input VAT page and on the dashboard, with the VAT amount you'd claim once fixed.
2. **New flag `vatNot15`**: `abs(vat − round(net × 0.15, 2)) > 0.05`. Credit notes compare absolute values. Documents with `vat_category` zero or exempt are skipped. Warns only.
3. **CSV built on the server** (`GET /api/purchases/input-vat/export.csv?from=&to=`), so the format is fixed in one place:
   - UTF-8 **with BOM** (`﻿`), CRLF line ends (Excel-friendly)
   - Header block: `Restaurant,<name EN / AR>`, `VAT number,<vat>`, `Period,<from> – <to>`, `Generated,<timestamp>`, blank line
   - Columns: Date, Type, Supplier, Supplier VAT number, **Supplier CR**, Document number, Purchase, Net, **VAT rate %** (`vat / net × 100`, 2 dp, blank when net = 0), VAT, Total, Claimable VAT, Flags
   - Every amount has **2 decimals**; **credit notes negative**
   - Flags in readable English joined by `"; "` (e.g. "QR doesn't match; VAT not 15%")
   - Last row: **TOTALS** (net, VAT, total, claimable)
   - The browser `lib/csv.ts` also gets the BOM, so other exports open properly in Excel with Arabic text
4. **Shows the claim period** column (§7.2).
5. **Soft-deleted purchases excluded** (§0).

---

## 7. Period locking and edge cases

### 7.1 How locking works
- Filing sets `VatPeriod.locked = True`.
- One guard, `vat_returns.locks.check(branch, date, *, actor, override_reason=None, action, obj)`:
  - date in no locked period → OK
  - locked and no override → `409 error.vatPeriodLocked` (with the period name) → the admin offers "Override (owner)"
  - locked, actor is owner, reason given → OK + `VatLockOverride` row + audit entry, in the same transaction
  - locked, actor is not owner → 403
- **Where it's called:**
  - `SupplierDocument`: create, update, delete in `supplier_invoice` PUT/DELETE, `supplier_credit_notes` POST, `supplier_credit_note` PATCH/DELETE, and `receive()` with an invoice. It checks the old date **and** the new date (moving a document out of a locked period counts as an edit).
  - `Purchase` update/destroy, when the purchase has a supplier document whose `claim_period` is locked.
  - `zatca.documents.issue()`, when an explicit `issued_at` falls in a locked period. POS documents are always "now", so in practice this only catches odd cases; the guard is still there.
  - `ZatcaDocument` is never edited after signing, and status changes from ZATCA (pending → reported) are **not** blocked: blocking them would break the chain. They are caught instead as "changed since filing" (§7.6).
- **Backstop:** `pre_save`/`pre_delete` signals on `SupplierDocument` raise if the period is locked and no override context is active, so the Django admin or the shell can't get round it. In Django admin, VAT models and supplier documents in locked periods are read-only.

### 7.2 Purchases entered late for a filed period
Example: in November you key in a supplier invoice dated 15 Sep, and Q3 is filed.
- By default this is **not** an edit of Q3. The document keeps its real `issue_date` (15 Sep), and `claim_period` is set to the **oldest open period** (Q4). KSA rules allow input VAT to be claimed in a later return. The dialog says: "Q3 is filed. This VAT will be claimed in Q4 2026."
- If the owner wants it in Q3 (to amend the Q3 return), that is an override: `claim_period = Q3`, reason logged, and Q3 shows "changed since filing → amend the return on the portal".

### 7.3 Refunds dated in a later period
A refund issued in October on a September invoice creates a credit note with `issued_at` in October. It lowers
October's (Q4's) output VAT. Q3 is untouched. The drawer shows the original invoice number on the credit note row.
No lock is involved.

### 7.4 Switching monthly ↔ quarterly
- Existing periods never change. The new frequency has an `effective_from` date, which must be **the first day of a calendar quarter** that is **after the last created period** (or the current period, if it hasn't started).
- Example: quarterly through Q4 2026, switch effective 1 Jan 2027 → Jan, Feb, Mar… as monthly periods.
- Switching in the middle of a quarter is refused with a plain message. Gaps and overlaps are impossible because the next period always starts the day after the previous one ends.
- Credit carries across the switch as normal (`previous` is just the period before).

### 7.5 The first period
- Starts on `max(vat_registration_date, ZatcaSettings.enabled_at date)` and ends on its normal monthly or quarterly end. So it may be a **short period** (e.g. 17 Aug – 30 Sep), labelled "Q3 2026 (from 17 Aug)".
- No previous period → credit carried = 0. If you had a credit from an earlier system, there's an owner-only "Opening credit" field on the first period (logged).
- Sales before ZATCA was enabled aren't in `ZatcaDocument` at all. The page says so if the registration date is earlier than `enabled_at`.

### 7.6 Negative VAT due
- Status "Credit", nothing to pay, no payment button.
- When filing, the owner chooses **Carry forward** (default) or **Refund requested** (ZATCA allows a refund claim).
- Carry forward: the next period's credit_in = −ZATCA amount.
- Refund: credit_in = 0. The period shows "Refund requested". A later "Refund received" record (amount, date) is a small extra I'd add only if you want it.

### 7.7 Changes after filing
If a pending document turns Reported or Cleared after filing, or a locked document is overridden, the dashboard
compares live figures with the **filing snapshot** and shows a "Changed since filing: +312.40 VAT" note listing the
documents. The owner can either:
- file an **amended return** on the portal and record it as a new `VatFiling` that supersedes the old one, or
- put the difference into **box 14** of the next period (only within ±5,000; above that ZATCA expects a voluntary disclosure, which is a question for the accountant).

### 7.8 Environments
Only documents of the period's environment count. Sandbox and Simulation periods are practice: TEST banner, no
reminders by default. Going live starts the Production periods fresh (see Q1).

---

## 8. API (all under `/api/vat/`)

| Method & path | Who | What |
|---|---|---|
| `GET periods/` | owner, manager | history rows (all figures from `figures.py`) |
| `GET periods/current/` and `GET periods/<id>/` | owner, manager | cards, boxes, checklist, filing, payments, changed-since-filing |
| `GET periods/<id>/documents/?card=output\|input\|pending\|review…` | owner, manager | the drawer rows |
| `POST periods/<id>/file/` | owner | filing (+ difference reason, credit treatment, attachment URL) |
| `POST periods/<id>/amend/` | owner | new filing that supersedes |
| `POST periods/<id>/payments/` · `POST payments/<id>/void/` | owner | record / void |
| `GET periods/<id>/audit/` | owner, manager | activity |
| `GET periods/export.csv` · `GET periods/export.pdf` | owner, manager | history export (BOM CSV / reportlab PDF), logged |
| `GET reminders/` | owner, manager | banner data for the admin shell |

There are no DELETE endpoints in this app.

---

## 9. Risks and open questions

### For you
1. **Test environments.** Should Sandbox and Simulation periods be visible at all? I propose yes, labelled TEST, hidden from history once you're in Production (never deleted).
2. **Multiple branches.** Today it's one restaurant and one VAT number, and periods are per branch. If a second branch shares the VAT number, the return must add the branches together. I'll build per branch now and keep the figures service branch-list-ready. Fine?
3. **Refund received.** Do you want to record ZATCA refunds (§7.6), or is "Refund requested" enough?
4. **Supplier zero or exempt.** Is a default category (VAT 0 → zero-rated) acceptable, with the owner correcting it on the document?
5. **History PDF.** One summary table, or one page per period with the boxes? I propose a summary table plus an optional "period PDF" with the boxes.
6. **Attachments.** Filing and payment PDFs live in Cloudinary. Six-year retention depends on that account staying paid and not being cleaned up. Should we also keep a local or second copy? (I recommend a monthly export script; not in this scope unless you say so.)

### For the accountant
1. **Amount vs Adjustment columns.** Should credit and debit notes go in the "Adjustment" column (my proposal), or be netted into "Amount"?
2. **Late supplier invoices.** Confirm that claiming in the next open period (§7.2) is acceptable rather than amending.
3. **Box 14 and voluntary disclosure.** The ±5,000 corrections limit and when a voluntary disclosure is needed instead.
4. **Due date on a weekend or holiday.** Does it move to the next working day? I'll store the plain last-day-of-month until told otherwise.
5. **Non-registered suppliers.** Should their purchases appear in box 11 (exempt) or not at all (out of scope)? I propose not at all.
6. **Mixed-rate supplier invoices.** One invoice with 15% and 0% lines: we store one net and one VAT per document. Is that enough, or do we need lines?
7. **Pre-registration input VAT and the first period.** Any special treatment?
8. **Box names.** Confirm the list in §4.5 against the live portal.

### Technical risks
- **Backfill from `signed_xml`:** XML parsing must match exactly what the builder wrote. Mitigation: the migration compares its parsed VAT with `vat_total` and reports any mismatch.
- **Lock bypass:** any new code path that writes supplier documents must call the guard. Mitigation: the signal backstop plus a test that tries every endpoint against a locked period.
- **Due date vs reminders:** the reminder banner is computed per request, so there's no scheduler dependency. An email or SMS reminder would need the Task Scheduler job (not in scope).
- **Rounding:** sums of stored 2-dp values only, so the dashboard and the portal both add the same rounded numbers.

---

## 10. Milestones (stop after each so you can test)

| # | Milestone | What you'll see / how you test |
|---|---|---|
| **M1** | **Stored VAT breakdown on ZATCA documents** (fields + `issue()` + backfill from XML) | Django admin → a ZATCA document shows Net and the VAT breakdown. A new test sale fills them in. The migration prints "N documents backfilled, 0 mismatches". |
| **M2** | **Input VAT fixes**: snapshots, stored claimable, supplierVatMissing → 0, `vatNot15`, "VAT needing review" card, deleted-purchase fix, server CSV | Input VAT page: a supplier without a VAT number now claims 0 and appears in the review card. A 14% invoice shows "VAT not 15%". Export CSV opens in Excel with Arabic names correct, the header block, a TOTALS row and negative credit notes. |
| **M3** | **Settings + periods**: frequency, registration date; `VatPeriod` auto-created; due date and days left | Business details → VAT filing section. New sidebar item "VAT returns" with the period picker; switching quarter/month shows the right dates and "Due 31 Jan 2027 · 64 days left". A cashier gets no menu item and a 403. |
| **M4** | **Cards + boxes + Show documents** (read-only) | The six cards with real numbers. Make a sale, refund it, add a supplier invoice → the cards move. Every "Show documents" list adds up to its card. Boxes 1–16 with copy buttons. Pending docs listed but not counted. |
| **M5** | **Checklist + Mark as filed + lock + override log** | Checklist warnings appear. File with a different ZATCA amount → warning and a required reason. After filing, editing a supplier invoice dated in that period → "Period locked"; the owner overrides with a reason → it saves, and the override shows in the activity list. A manager can't file or override. |
| **M6** | **Payments + status + credit carried forward** | Record two partial payments → Filed → Paid. Void one → back to Filed (still listed, struck through). A negative period → Credit, and the next period's "Credit carried" card shows it. |
| **M7** | **History + CSV/PDF export** | History tab with every period. CSV opens in Excel (BOM); the PDF downloads. |
| **M8** | **Reminders banner** | Set the due date 10 days away (or move the server clock in a test) → amber banner on the dashboard; past due → red. Disappears once filed and paid. |
| **M9** | **Late purchases, amendments, frequency switch, first period** | Key an invoice dated in a filed quarter → "will be claimed in Q4". Amend a filing → old one shown as superseded. Switch to monthly from the next quarter → monthly periods after it, and a mid-quarter switch is refused. Short first period labelled correctly. |

| **M10** | **Invoice archive** (§11) | ZATCA → Invoice archive: Sales and Purchases tabs, filters, per-document view/PDF/XML, bulk ZIP built in the background with a "ready to download" link; "Download all invoices for this period" on the VAT dashboard. |

Each milestone comes with backend tests (figures, locks, permissions, CSV format) and I'll report it the way §8 of
the approval doc asks: what was built, how to test it, and anything I decided differently.

---

## 11. M10: Invoice archive

**Read-only.** The archive never changes, re-signs or deletes anything. It only reads stored documents and files.

### Page
ZATCA → **Invoice archive** (`/admin/zatca/archive`), owner and manager. A cashier gets neither the menu item nor the API (403).

| Tab | Rows |
|---|---|
| **Sales** | every `ZatcaDocument` with `source = pos` in the selected environment: B2C/B2B invoices, credit notes, debit notes |
| **Purchases** | every `SupplierDocument` (invoices + credit notes) of non-deleted purchases |

**Filters** (as query parameters, so a filtered view can be bookmarked):
- date range (sales: `issued_at` in Riyadh time; purchases: `issue_date`)
- VAT period (sales: the period's date window; purchases: `claim_period`, so it matches the dashboard exactly)
- type
- ZATCA status (sales)
- B2C/B2B (sales)
- customer (`BusinessCustomer`) or supplier
- document number
- amount range (on total)
- free-text search (number, customer/supplier name, VAT number, order number)

The list is paginated on the server, like the purchases list.

**Per document:**
- **View**, a drawer with the stored figures and status
- **PDF**:
  - sales: generated on the server, same layout as the printed tax invoice, with the QR
  - purchases: the stored `pdf_url`, or "missing"
- **XML**:
  - sales: `signed_xml`, plus `cleared_xml` for cleared B2B
  - purchases: the stored supplier `xml`, or "missing"

### Bulk ZIP
- "Select all filtered results" (or a VAT period) → **Create ZIP**. The job stores the **filter**, not a list of IDs, and freezes the matching IDs when it starts.
- Layout:
  ```
  <Restaurant>_<period-or-range>_invoices.zip
    index.csv
    sales/INV-3-000123.pdf  INV-3-000123.xml  INV-3-000123_cleared.xml
    purchases/<supplier>_<number>.pdf  <supplier>_<number>.xml
  ```
  File names are sanitised: no `/ \ : * ? " < > |`, Arabic kept, duplicates get `_2`.
- **index.csv:** UTF-8 with BOM, using the same CSV writer as M2. Columns: side (sales/purchases), number, date, type, B2C/B2B, customer or supplier, seller VAT, buyer/supplier VAT, net, VAT, total (credit notes negative), status, PDF file, XML file, cleared XML file. A file that doesn't exist is written as **`missing`** (e.g. a supplier PDF never uploaded, or an old sales document whose PDF failed to render, with the reason). Nothing is skipped silently. Last row: TOTALS.
- **Background:** the repo has no task queue (no Celery/RQ). I'll follow the ZATCA retry-queue pattern:
  - a `VatArchiveExport` row (status queued → running → ready / failed, filter, counts, file path, size, error, requested_by, timestamps)
  - a daemon thread starts it right away
  - a `vat_archive_exports` management command, run by Windows Task Scheduler every few minutes like `zatca_retry_queue`, picks up anything queued or stuck if the thread died
- The page polls the job and shows **"Ready to download"**. Files go to a private media folder (not Cloudinary) and are served only to owner/manager through an authenticated endpoint. Ready ZIPs are kept for 7 days and then cleaned up. The ZIP is a copy; the source documents are never touched.
- Supplier files stored as Cloudinary URLs are downloaded into the ZIP. A download that fails is marked `missing (download failed: <reason>)` in the index.
- **Audit:** every bulk download is logged in `VatAuditLog`: request, ready, each download, filter, count, who.
- **VAT dashboard:** a button **"Download all invoices for this period"** opens the same export with the period filter.

### Things to decide or note
- **Server-side tax invoice PDF with Arabic.** `reportlab` (already used) can't shape Arabic text by itself. It needs `arabic-reshaper` + `python-bidi` and a bundled Arabic font (e.g. Noto Naskh Arabic, OFL licence) plus a QR library (`qrcode` or reportlab's own QR widget). That means **new dependencies**, which go on the VPS checklist. The layout follows `ZatcaReceipt.tsx` and the stored `receipt` JSON, so the PDF shows the frozen snapshot, never today's settings.
- **Size:** a quarter of B2C sales can be thousands of documents. The ZIP is written to disk as it's built (streaming), never held in memory.

### Parts that fit earlier milestones
| Part | Fits in | Why |
|---|---|---|
| Per-document **XML download** (signed + cleared) | **M4** | The "Show documents" drawer lists the same documents; a download link is a small read-only endpoint |
| **BOM CSV writer** with header block + TOTALS | **M2** (already planned) | M10's `index.csv` reuses it |
| `VatAuditLog` | **M5** (already planned) | M10 logs to it |
| Sales PDF renderer, ZIP job, archive page | **M10** | New dependencies and a background job; nothing earlier needs them |

I'll add the XML download link to M4 unless you'd rather keep everything archive-related in M10.
