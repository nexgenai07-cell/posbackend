# Your ZATCA proposal is approved, with the changes below

Thank you for the proposal. It is approved with the decisions and changes in this message. Where this message differs from your proposal, this message wins. Start with milestone 1 and **stop after each milestone** so I can test it.

The two goals that shape everything below:

1. **Switching environments must be easy.** We build and test in **Sandbox** now, then move to **Simulation**, and later to **Production**, by changing only the ZATCA settings page: environment, business details and a new OTP. **No code changes, no `.env` changes, no redeploy.**
2. **The restaurant owner manages their own details.** From the sidebar, the owner can view and edit their business details, onboard, go live and fix problems without a developer.

---

## 1. My decisions on your 10 questions

| # | Question | Decision |
|---|---|---|
| 1 | Menu prices include VAT? | **Yes**, 15% included |
| 2 | Invoice unit | **One per settlement**, as proposed |
| 3 | B2B at launch? | **Yes: B2B (standard) and B2C (simplified) both at launch** (section 3) |
| 4 | Hosting and scheduler | Windows Task Scheduler locally for now; production hosting decided later. Keep the retry job scheduler-agnostic |
| 5 | Signer | **Pure Python**, with the SDK as fallback and test oracle |
| 6 | Self-billing | **No** |
| 7 | Purchase `unit_cost` | **Without VAT** |
| 8 | Roles | As proposed: managers view and retry; owners only for settings, onboarding and go-live |
| 9 | Refunds | Full cancel + credit note first; partial refunds later |
| 10 | Pre-fill from Branch | **Yes**, editable |

**Tenancy:** this POS currently serves **one restaurant** (single tenant). Keep everything branch-scoped as proposed. Don't build multi-tenant support now, but don't hardcode anything that would block it later.

**Invoice type codes:** you are right: `1000` = standard only, `0100` = simplified only, `1100` = both. My earlier brief had it wrong.

---

## 2. Environments: one codebase, switched from the settings page

Everything that differs between environments must follow the **Environment** setting automatically:

| | Sandbox | Simulation | Production |
|---|---|---|---|
| Base URL | `https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal` | `.../e-invoicing/simulation` | `.../e-invoicing/core` |
| CSR certificate template | `TSTZATCA-Code-Signing` | `PREZATCA-Code-Signing` | `ZATCA-Code-Signing` |
| OTP | Always `123345` (pre-filled) | Real OTP from the company's Fatoora Simulation portal | Real OTP from the Fatoora portal |
| Business details | Pre-filled test identity | The company's real details | The restaurant's real details |
| Signing certificate | Compliance certificate (sandbox quirk: the production certificate is a fixed demo cert) | **Production certificate** issued from our own CSR | **Production certificate** issued from our own CSR |
| VAT handling | Sandbox two-VAT fallback if needed | One real VAT everywhere | One real VAT everywhere |
| Receipt label | "TEST / تجريبي" | "TEST / تجريبي" | none |
| Invoices legally real? | No | No | **Yes** |

Rules:
- Put all environment-specific values in **one place** (e.g. `zatca/environments.py`). No `if sandbox` checks scattered through the code.
- **Never mix environments.** Each device belongs to exactly one environment. Its certificate, ICV/PIH chain and invoice numbers are never reused in another environment.
- Add an automated test that onboards a device in each of the three environments against mocked APIs, proving that the switch needs no code change.
- The Sandbox two-VAT quirk (compliance documents use the VAT inside the compliance certificate; Reporting/Clearance use `399999999900003`) is handled only in the Sandbox path. Generating the Python CSR with VAT `399999999900003` should make the two VATs match; keep the fallback anyway.

---

## 3. B2B and B2C both at launch

- Default invoice types = `1100` (both). The compliance checks run all **6** documents (standard and simplified × invoice, credit note, debit note).
- **Move B2B (standard invoices) to right after milestone 4.** Milestones 1–3 must support both types from the start: the XML builder, signer and tests cover standard and simplified, and `zatca_test_invoice` sends one of each (simplified → REPORTED, standard → CLEARED).
- **Payment screen:** B2C is the default. The cashier can switch to **"Tax invoice for company / فاتورة ضريبية لشركة"** and pick or create a business customer.
- **Business customer:** legal name, VAT number (15 digits, starts and ends with 3), CRN (optional), full structured address (building number, street, district, city, postal code, country). Validate with plain-language messages.
- Standard invoices are sent for **clearance**; the receipt uses the QR from ZATCA's `cleared_xml`. If clearance is unreachable, print "Pending clearance" and allow a reprint once cleared.

---

## 4. The owner manages their own details (sidebar "ZATCA" section)

### 4.1 Business details page
- **Sandbox:** every field pre-filled with the working test identity: VAT `399999999900003`, CRN `1010010000`, a complete test address (building `2322`, additional number `1234`, postal code `12222`, district and city Riyadh), invoice types `1100`, OTP `123345`.
- A banner on every ZATCA page while not in Production: **"TEST MODE: invoices are not legally real."**
- **Editable by the owner at any time:** seller name (AR/EN), address fields, branch / organization unit, business category, receipt texts, logo, paper width. The **next invoice** uses the new values; past invoices never change.
- **Locked once a device is Active** in Simulation or Production: VAT number, CRN and environment (the certificate is bound to them). The page explains why, and offers **"Change VAT/CRN"**, which starts the go-live wizard again with a new device.
- Validate every field with plain-language messages (as in your proposal), and show which fields are locked and why.

### 4.2 Go-live wizard
A step-by-step wizard the owner can run without a developer:
1. **Choose environment:** Simulation or Production.
2. **Enter your business details:** the test values are cleared; branch name, address and logo are pre-filled from Branch. The owner enters the real legal name (AR/EN), VAT number, CRN and national address, exactly as on their VAT certificate. Show a short checklist of where to find each value.
3. **Generate key + CSR** (one click; the key never leaves the server).
4. **Enter OTP:** explain in plain words how to get it: log in to fatoora.zatca.gov.sa with the company's ZATCA (ERAD) account → (for Simulation: "FATOORA Portal Simulation") → "Onboard new solution unit/device" → copy the OTP. The OTP is valid for **1 hour**. Show a countdown hint.
5. **Get Compliance CSID → Run compliance checks → Get Production CSID**, each with a clear pass/fail result and the ZATCA messages in plain language.
6. **Make a test sale:** prompt the owner to make one B2C and one B2B test sale and show the results.

Wizard rules:
- Every go-live creates a **new device**. The previous device and its documents are archived read-only (marked "test" if from Sandbox/Simulation), and the new chain starts at ICV 1 with the initial PIH.
- **Practice mode:** the whole wizard can also run against **Sandbox** (OTP `123345`), so I can test the go-live flow without a real company.
- **Production guard:** Production can only be selected when a real (non-test) VAT number and a real OTP are entered, and the owner must confirm a warning: **"Production invoices are legally real and are reported to ZATCA under your VAT number."** Production is not used until we have a real restaurant client.
- **"Back to Sandbox":** owners only, with a warning, and never on a device that has issued Production invoices.
- After go-live, the **compliance-checks step must be repeatable** with the same compliance CSID (no new OTP needed) in case a check fails.

### 4.3 Other pages (as proposed)
Device & onboarding status, Credentials status (never shows secrets), Receipt settings with live preview, Documents log with Retry and Reprint.

### 4.4 Onboarding without the UI
In **milestone 3**, also build a `zatca_onboard` management command that runs the same onboarding code (key + CSR → OTP → compliance CSID → 6 checks → production CSID). The settings-page buttons later reuse exactly the same functions.

---

## 5. Technical notes

1. **Signer reference:** use `mabaega/ZatcaPython` on GitHub (`utilities/einvoice_signer.py`) as a working reference for C14N, SignedProperties and hashing, alongside the SDK hash comparison against my files in `C:\zatca\work`.
2. **Milestone 3 acceptance:** the Python signer must pass **all 6 compliance document types** in Sandbox, plus Reporting (simplified) and Clearance (standard), not only one invoice.
3. **VAT rounding:** with VAT-inclusive prices, when a line has quantity > 1 and the proportional net doesn't divide evenly by the quantity, `PriceAmount × InvoicedQuantity` must still equal `LineExtensionAmount`. Test cases like 3 × 10.00 and 7 × 13.99 SAR against Sandbox early. `PayableRoundingAmount` is backed by the official sample `Standard Invoice with Payable Rounding Adjustment.xml`.
4. **Currency:** `Branch.currency` defaults to USD. ZATCA documents must always use `DocumentCurrencyCode` and `TaxCurrencyCode` = **SAR**.
5. **CSR validation:** if the VAT number's 11th digit is `1` (VAT group), the organization unit must be the 10-digit TIN.
6. **Rejected documents (HTTP 400):** keep them in the ICV/PIH chain; the next document uses the rejected document's hash as PIH. Never reuse an ICV.
7. **Background reporting:** the daemon thread is fine, but the 5-minute `zatca_retry_queue` is the guarantee. Assume the thread can die on a restart.
8. **Local files:** don't delete anything in `C:\zatca\work` until milestone 3 passes in Sandbox with the imported credentials. Then delete the secret files as you listed.
9. **Security:** as proposed (Fernet-encrypted fields, no secrets in the API, logs or Django admin, audit log for every onboarding action).

---

## 6. Milestone order

| # | Milestone | Done when |
|---|---|---|
| 1 | Settings page (sandbox pre-fill, test banner, field locking rules) + environments module + VAT engine | I can edit details in the sidebar; VAT rounding tests pass |
| 2 | Device, encryption, import of my existing Sandbox credentials | Credentials page shows "Set"; no secret in any API response |
| 3 | Build, sign, QR, send (**standard + simplified**) + `zatca_onboard` command | All 6 compliance types pass in Sandbox; one REPORTED and one CLEARED; hashes match the SDK |
| 4 | POS hook + background reporting (B2C) | Paying a bill reports an invoice; ICV/PIH chain correct; offline → queued → retried |
| 5 | **B2B at payment**: business customers + clearance | A company sale returns CLEARED; receipt uses ZATCA's QR |
| 6 | Receipts (B2C and B2B, 58/80 mm, TEST label) + credit notes | Printed QR scans in the ZATCA app; cancelling a paid order reports a credit note |
| 7 | **Go-live wizard + onboarding buttons** (incl. practice mode on Sandbox) | I complete the whole wizard in practice mode without help |
| 8 | Partial refunds + debit notes | A partial refund issues a credit note for those lines only |
| 9 | Purchasing VAT (as proposed) | Supplier QR check and Input VAT report work |
| 10 | **Simulation run** with a real company + readiness (expiry warnings, unreported-invoice banner, runbook) | Onboarded in Simulation from the wizard; B2C REPORTED and B2B CLEARED with no warnings |

---

## 7. Advisor questions

Keep the list from your proposal. Don't block on them; use your defaults until I send answers.

## 8. How to report each milestone

At the end of each milestone, tell me in plain words:
- what was built and where (files, pages),
- exactly how I test it (clicks or commands),
- what ZATCA returned (status, warnings, errors),
- anything you were unsure about or decided differently from this plan.
