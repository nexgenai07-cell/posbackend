# Cash Tendered & Change — Plan

Status: **All 4 milestones implemented** (backend, POS PaymentPage, BillsPage, receipts) · Date: 2026-11-10
Scope: `restaurant-backend`, `restaurant-admin` (burger_web and mobile: no changes)

## Problem

When a customer pays cash, the cashier often gets more than the bill
(e.g. bill **1200 SAR**, customer hands **1500 SAR**, change due **300 SAR**).
Right now the system does not record this anywhere:

- **POS PaymentPage** (`restaurant-admin/src/pages/pos/PaymentPage.tsx`) asks for
  amount, method (cash/card/other) and tip. It has no "cash received" field.
- **BillsPage** "Cash received" button calls `markBillPaid(bill.id, "cash")` in one
  click. The amount handed over is never asked for.
- Backend `Payment` and `Bill` (`orders/models.py`) have no tendered/change fields.
- Receipts and the ZATCA receipt (fatoora) print only "Cash" and the total.

## Decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | Is cash received mandatory for cash payments? | **Yes.** Include an "Exact" quick button so exact-cash payments still take one tap. Every cash payment gets a full record for drawer reconciliation. |
| 2 | "Keep the change" | **Yes.** One button moves the change into `tip`, which sets change to 0. Bill settle does not support tip yet, so this is added on the backend. |
| 3 | Customer website: "which note will you pay with?" | **Not now.** It adds friction for the customer, and the cashier handles the cash anyway. |
| 4 | "Total change given" in shift/cash reports | **Not now.** The data is stored from day one, so the report can be added later. |

## Rules

- `Payment.amount` stays the **bill amount** (1200). It does not become the tendered
  amount (1500). Sales, VAT and drawer reports therefore stay correct unchanged.
- `change_given = cash_tendered - (amount + tip)`, computed **on the backend** and
  stored as an audit snapshot. The admin UI shows the same value live before
  confirming, so nothing is hidden.
- `cash_tendered` is accepted **only** when method is `cash`. For card, other or
  online it must be empty (rejected otherwise).
- `cash_tendered >= amount + tip`, otherwise a validation error.
- "Mandatory" is enforced in the **admin UI** (Milestones 2–3). The API still
  accepts cash with no `cash_tendered` (stored as null), so existing callers
  and older records keep working. Null means "not recorded", never "exact".
- Bill settle with a `tip` but nothing outstanding is rejected
  (`error.tipNeedsPayment`), because there would be no Payment row to carry the tip.
- The ZATCA receipt snapshot gets `cash: {tendered, change}` (null for card).
  Split cash payments are added up. A receipt rebuilt by the retry job for a
  bill-level settle has no cash block. Invoice totals and XML are unchanged.
- **ZATCA XML / VAT: no change.** Change is not part of the tax invoice totals. It
  only appears on the printed receipt.

## Milestone 1: Backend

| File | Change |
|------|--------|
| `orders/models.py`, `Payment` | Add `cash_tendered` and `change_given`. Both are `DecimalField(10,2, null=True, blank=True)`. |
| `orders/models.py`, `Bill` | Add the same two fields, plus `tip_at_payment` for bill-level tip. |
| `orders/migrations/` | One migration for the new fields |
| `orders/serializers.py`, `RecordPaymentSerializer` | Accept `cash_tendered`. Validate cash-only and `>= amount + tip`. Compute `change_given`. Error keys: `error.cashTenderedTooLow`, `error.cashTenderedNotCash`. |
| `orders/serializers.py`, `PaymentSerializer` | Expose `cash_tendered` and `change_given` (read-only change) |
| `orders/views.py`, record payment | Save the new fields. The idempotency replay check also compares `cash_tendered`. |
| `orders/billing_serializers.py`, `SettleBillSerializer` | Accept `cash_tendered` and `tip`, with the same validation against the outstanding amount |
| `orders/billing.py`, `mark_bill_paid()` | Take `cash_tendered` and `tip`, store them on the Bill along with `change_given`, and put the tip on the Payment row(s) written by `_write_order_payments()` |
| `orders/billing_serializers.py`, `BillSerializer` | Expose `cash_tendered`, `change_given`, `tip_at_payment` |
| Tests (`orders/test_billing.py`, `orders/tests.py`) | Cover: exact cash, overpay with change, underpay rejected, tendered sent with card rejected, keep-change-as-tip, idempotent replay |

## Milestone 2: Admin POS PaymentPage

- When **Cash** is selected, show a **"Cash received"** input (required).
- Quick buttons: **Exact**, then the next round amounts (e.g. for 1200: 1500, 2000).
- Live line under the total: **"Change to return: 300.00 SAR"**.
- **"Keep change as tip"** button: tip goes up by the change, and change becomes 0.
- Confirm is disabled while tendered is less than the total.
- Update `lib/api/orders.ts` and types with the new fields.
- Done: the shared `components/pos/CashTender.tsx` (input, quick amounts, live
  change / short, keep-as-tip) is reused by Milestone 3. The done screen shows
  "Change to return", and split payments list cash received and change per payment.

## Milestone 3: Admin BillsPage

- The "Cash received" button opens a small dialog instead of settling immediately.
- The dialog has the same parts as Milestone 2: amount input, quick buttons, live change, keep-as-tip, Confirm.
- Update `lib/api/bills.ts` (`markBillPaid` sends `cash_tendered` and `tip`) and types.

## Milestone 4: Receipts / Fatoora

In `components/pos/Receipt.tsx`, `components/pos/BillThermalReceipt.tsx` and
`components/zatca/ZatcaReceipt.tsx`, print these lines under the payment line, for cash only:

```
Cash received / المبلغ المستلم     1500.00
Change        / الباقي              300.00
```

i18n: add the new keys to English and Arabic.

## Out of scope (later)

- Customer website note about which bill they will pay with
- "Total change given" in shift / cash reports
- Mobile app
