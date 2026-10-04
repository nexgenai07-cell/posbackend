# Restaurant Backend — Build Plan

Reference doc for building the Django REST backend for the restaurant system (`restaurant-admin`, `burger_web`, `restaurant-mobile`). Check items off here as they're built — this file is the source of truth for scope, not the chat history it came from.

## Priority

Build and prove out **`restaurant-admin` end-to-end first**: login → products/categories → tables → orders/POS → KDS → inventory → purchasing → reports. Public-facing endpoints (`/api/menu/`, QR table lookup, public order creation) come **last**, after restaurant-admin is fully working against the real backend. Until then, `burger_web`/`restaurant-mobile` keep using their current `qr-bridge.json`/mock workaround untouched.

## Guiding principle: keep it simple

- Plain Django/DRF patterns over clever ones. No abstraction layer added until there's a concrete second use case that needs it.
- No package added without a concrete near-term need. Real-time (Django Channels/Redis) is deferred to the phase that actually needs it (orders/KDS), not installed upfront "just in case."
- Bilingual fields are plain `_en`/`_ar` columns — no `django-modeltranslation`, no translation API integration anywhere.
- Multi-branch support means "every model has a `branch` FK and querysets filter by it" — not a multi-tenancy framework. One `Branch` row exists at launch.
- One exception to "no shared abstraction until there's a second use case": a shared `BaseModel` (below) is worth it from the start, because *every* model needs `created_at`/`updated_at`/soft-delete on day one — that's not a hypothetical future need.

---

## Final app structure

```
common/       # Shared abstract BaseModel only — not a general shared layer
accounts/     # Staff (plain model, not AUTH_USER_MODEL), PIN auth, JWT, shifts
branches/     # Branch model
catalog/      # Category, Product, Deal
tables/       # Table, QR session tokens
orders/       # Order, OrderItem, Payment
inventory/    # InventoryItem, RecipeItem, StockMovement
purchasing/   # Supplier, Purchase, PurchaseItem
customers/    # Customer (phone-keyed, no auth)
reports/      # No models — read-only aggregation views over other apps' data
realtime/     # No models — Channels consumer, JWT WS auth, signal-driven broadcasts (Phase 6)
```

No `core` app. `common` stays narrow but has grown to three files as genuine cross-app needs came up: `models.py` (`BaseModel`), `exceptions.py` (the global error-code exception handler, Phase 3), `mixins.py` (`BranchScopedQuerysetMixin`, Phase 5). Nothing gets added to it speculatively — each of these three exists because more than one app already needed it.

---

## Shared base model & global pagination

**`common/models.py` — `BaseModel`** (abstract). Every model in every app inherits from this instead of `models.Model`:
- `created_at` (auto-set on creation), `updated_at` (auto-set on every save)
- `is_deleted` (soft-delete flag) backed by a real soft-delete pattern, not just an ignored column:
  - `objects` (default manager) — automatically excludes `is_deleted=True` rows from every query, including `.filter()`/`.get()`/etc.
  - `all_objects` — unfiltered manager, for the rare case of needing soft-deleted rows (restore UI, audits).
  - `instance.delete()` sets `is_deleted=True` and saves, instead of removing the row. `instance.delete(hard=True)` or `instance.hard_delete()` does a real `DELETE` — an explicit, documented exception, never the default.
  - Bulk `SomeModel.objects.filter(...).delete()` also soft-deletes (via a custom `QuerySet`); `.hard_delete()` is available on the queryset too when a real bulk delete is genuinely needed.
- `BaseModel.Meta.ordering = ["id"]` — found while smoke-testing pagination: DRF's `PageNumberPagination` warns (`UnorderedObjectListWarning`) and can return inconsistent pages without a deterministic default ordering. Set once here so every model gets it for free; a subclass can still override `Meta.ordering` if it needs a different default sort.

**Pagination** — configured once, globally, in `REST_FRAMEWORK` (`config/settings.py`): `DEFAULT_PAGINATION_CLASS = PageNumberPagination`, `PAGE_SIZE = 25`. Every list endpoint gets `?page=`/`?page_size=` for free; no per-viewset pagination config needed anywhere.

---

## Models

### `branches`
- **Branch**: `name_en`, `name_ar`, `address`, `timezone`, `currency`. Exactly one row seeded at launch.

### `accounts`
- **Staff** (a plain model, deliberately **not** `AUTH_USER_MODEL` — see Auth section for why):
  `branch` FK → Branch, `name`, `role` (choices: `owner`/`manager`/`cashier`/`kitchen`), `pin_hash`, `is_active`.
  PINs are hashed with Django's standard salted password hashers (`make_password`/`check_password`), so uniqueness-per-branch can't be a DB constraint on the hash — it's checked in application code (`Staff.pin_taken_in_branch()`) before create/update.
- **Shift**: `staff` FK → Staff, `clock_in`, `clock_out` (nullable).

### `catalog`
- **Category**: `branch` FK, `name_en`, `name_ar`, `sort_order` (default ordering, overriding `BaseModel`'s `id` default).
- **Product**: `branch` FK, `category` FK → Category (`on_delete=PROTECT`), `name_en`, `name_ar`, `description_en`, `description_ar`, `price`, `cost_price`, `image`, `is_available`, `badge` (choices, nullable), `days` — a plain `JSONField` (list of weekday strings), **not** Postgres's `ArrayField`, so it still works against the local sqlite fallback used for `check`/`makemigrations`, not just Neon. Empty list = every day.
- **Deal**: `product` FK → Product, one-to-one, `price`. Confirmed against `restaurant-admin/src/lib/deals.ts`.
- **DealWindow**: `deal` FK → Deal, `day` (choices), `start_time`, `end_time` — a deal is a *set* of per-day windows (mirrors `Deal.windows[]` on the frontend), not a single window, so this is its own table rather than fields on `Deal`.
  **`Deal` is hard-deleted, not soft-deleted** — found while testing: `Deal.product` is a `OneToOneField`, so a soft-deleted row still occupies that unique slot and blocks creating a new deal on the same product. Documented exception to the `BaseModel` default, in `catalog/views.py`.

### `tables`
- **Table**: `branch` FK, `label_en`, `label_ar`, `qr_code`, `session_token` (nullable, unique when set — SQL treats multiple `NULL`s as distinct, so this is "unique only while a session is open"), `status` (choices: `empty`/`occupied`/`needs-bill`). `open()`/`close()`/`mark_needs_bill()` are model methods (mint/clear the session token + QR code, flip status) called from the corresponding API actions, not exposed as directly-writable serializer fields.

### `customers`
- **Customer**: `branch` FK, `phone` (unique per branch), `name` (nullable), `last_order_at` (nullable). Built in Phase 5 as an `Order.customer` FK dependency — no endpoints yet (deferred to Phase 13's public QR flow, the only thing that actually creates customers).

### `orders`
- **Order**: `branch` FK, `table` FK → Table (nullable), `customer` FK → Customer (nullable), `source` (choices: `pos`/`qr` — field exists now even though `qr` isn't wired until Phase 13), `staff` FK → Staff (nullable), `status` (choices: `open`/`sent`/`preparing`/`ready`/`served`/`closed`/`cancelled`), `opened_at`, `closed_at` (nullable).
- **OrderItem**: `order` FK, `product` FK → Product (kept for traceability/reporting only — never re-read for pricing), `name_en_snapshot`, `name_ar_snapshot`, `price_snapshot`, `quantity`, `status` (choices: `pending`/`fired`/`preparing`/`ready`/`served`/`voided`), `notes` (nullable).
  **Rule: `*_snapshot` fields are set once at creation and never editable afterward via the API** — verified by testing: changing a product's live price after an order was placed did not change that order's already-computed total.
- **Payment**: `order` FK, `amount`, `method` (choices: `cash`/`card`/`other`), `tip` (nullable), `paid_at`.
- **Update (Phase 8)**: `send-to-kitchen` now deducts recipe stock too — see `inventory` below.

### `inventory`
- **InventoryItem**: `branch` FK, `name`, `unit`, `current_stock`, `par_level`, `cost_per_unit`, `supplier` FK → Supplier (nullable). `current_stock` is settable at creation (initial count) but silently ignored on any later PATCH/PUT — after creation it only ever changes through `adjust_stock()`, so every change is backed by a `StockMovement` row.
- **RecipeItem**: `product` FK → Product, `inventory_item` FK → InventoryItem, `quantity`, `unit`.
  (No separate `Recipe` model — it would carry no fields beyond `product_id` + a list, so `RecipeItem.product` gives the same shape with one less join, same reasoning as `catalog`'s `Deal`/`DealWindow`.)
- **StockMovement**: `inventory_item` FK, `quantity_delta` (signed), `reason` (choices: `sale`/`purchase`/`waste`/`adjustment`), `order` FK → Order (nullable), `created_at`. **Append-only — never updated or deleted**, enforced by simply not exposing any update/delete endpoint for it, not a model-level restriction.
- **`inventory/services.py`** — `adjust_stock(inventory_item_id, quantity_delta, reason, order=None)` is the one place stock ever changes: `select_for_update()`s the row, updates `current_stock`, creates the `StockMovement`. Must be called inside a `transaction.atomic()` block — the lock means nothing without one. `deduct_stock_for_order_item(order_item)` loops the product's recipe and calls it per ingredient; a product with no recipe configured is a no-op, not an error. **No hard block on insufficient stock** — deduction is allowed to go negative (matching the frontend's own low-stock-via-`par_level` model, which treats negative/below-par stock as a discrepancy to reconcile, not a sale to block).
- **`send-to-kitchen` (in `orders`) now wraps the whole operation in one transaction**: `select_for_update()` on the order's pending items (so a double-submit of the same request can't double-deduct) plus, inside `adjust_stock`, `select_for_update()` on each `InventoryItem` row (so two different orders needing the same ingredient can't both read stale stock and oversell). **This is the concurrency risk the plan called out by name** — see Risks section for the one genuinely important caveat about testing it.

### `purchasing`
- **Supplier**: `name_en`, `name_ar`, `contact_info`. (No `branch` FK, matching the frontend model — not scoped to a branch, so any authenticated staff can read one, owner/manager can write.) Model built in Phase 8 as an `InventoryItem.supplier` dependency; endpoints added here in Phase 9.
- **Purchase**: `branch` FK, `supplier` FK → Supplier, `status` (choices: `draft`/`ordered`/`received`, default `ordered`), `ordered_at`, `received_at` (nullable). **Immutable once `received`** — PATCH/DELETE both check this at the view level (409 `error.purchaseAlreadyReceived`), not relying on `on_delete=PROTECT` alone (soft-delete bypasses Django's real delete collector, same reason `catalog`'s `Category.destroy()` checks explicitly rather than trusting the FK constraint).
- **PurchaseItem**: `purchase` FK → Purchase, `inventory_item` FK → InventoryItem (a **string reference**, `"inventory.InventoryItem"`, not a direct import — `inventory.models` already imports `purchasing.models.Supplier`, so a direct import back would be circular), `quantity`, `unit_cost`.
- Setting `status="received"` via a plain `PATCH` is rejected (`error.usePurchaseReceiveEndpoint`) — only `POST /api/purchases/{id}/receive/` may do that, since receiving has side effects (stock deduction, cost update) a plain field edit must not trigger silently.

---

## API endpoints

Grouped by phase — see Build Order below for what's built when. "Later" = Phase 8, not built yet.

### accounts
- `GET /api/auth/login-choices/` `?branch=` (optional) → `{branch: {...}, staff: [{id, name, role}]}` — **public** (`AllowAny`, throttled 30/min). Everything restaurant-admin's login screen needs before it holds a token. Exists because `GET /api/branches/active/` is `IsAuthenticated` and `GET /api/staff/` is owner/manager-only, while the login screen has neither. `is_active=False` staff are omitted, and no PIN material is ever included. Falls back to the lowest-id branch when `?branch=` is omitted (the single seeded one, until multi-branch terminal selection is a real flow); unknown branch id → 404.
- `POST /api/auth/pin-login/` `{staff_id, pin}` → `{access, refresh, staff}`. Wrong PIN, unknown staff id and deactivated staff all return 400 `error.invalidCredentials` — deliberately one code, so the response can't be used to enumerate staff ids.
- `POST /api/auth/refresh/`
- `POST /api/auth/logout/`
- `GET/POST /api/staff/`, `PATCH/DELETE /api/staff/{id}/`
- `GET /api/staff/{id}/shifts/`, `POST /api/staff/{id}/clock-in/`, `POST /api/staff/{id}/clock-out/`

Note on error bodies: view-level errors are `{"error": "<code>"}` with the code as a plain string, but a *serializer*-level `ValidationError({"error": "..."})` comes back as `{"error": ["<code>"]}` because DRF's error-detail collector wraps it in a list. Both shapes are real; restaurant-admin's `src/lib/api/client.ts` normalises them.

### branches
- `GET /api/branches/active/`, `PATCH /api/branches/{id}/`

### catalog
- `GET/POST /api/categories/`, `PATCH/DELETE /api/categories/{id}/` (409 `error.categoryInUse` if products still reference it). Read = any authenticated staff; write = owner/manager only.
- `GET/POST /api/products/` (`?category=&available=`), `PATCH/DELETE /api/products/{id}/`. Same read/write split as categories — deliberately not "admin-only" for reads, since POS/KDS will need the product list once Phase 5 lands, and there's no reason to gate that behind a later rework.
- `PUT/DELETE /api/products/{id}/deal/` — owner/manager only. Product responses nest the deal (`{..., "deal": {"price", "windows": [...]}}`) rather than requiring a second call.
- *(Later)* `GET /api/menu/` — public, unauthenticated

### tables
- `GET/POST /api/tables/`, `PATCH/DELETE /api/tables/{id}/` — CRUD is owner/manager only (admin Tables page); read is any authenticated staff. 409 `error.tableNotEmpty` deleting a table that isn't `empty` or still has a session token.
- `POST /api/tables/{id}/open/`, `/close/`, `/needs-bill/` — owner/manager/**cashier** (POS-area, matches `rbac.ts`'s `AREA_ROLES.pos`, not admin-only).
- `GET /api/tables/active-sessions/` — any authenticated staff (read parity with `GET /api/tables/`: `session_token` is already in that response). Open tables only, each with its live `session_token` and `/t/<token>` `qr_code`. Answers "what code do I type into restaurant-mobile?" without opening Django admin or a shell, e.g. to reprint a lost QR label. A closed table's code is absent by construction (`Table.close()` nulls the token).
- *(Later)* `GET /api/tables/by-session/{token}/` — public
- **QR payload format**: `Table.open()` sets `qr_code = "/t/{session_token}"`, where `session_token` is `secrets.token_urlsafe(24)` (32 chars of base64url: `A–Z a–z 0–9 - _`). A scan therefore yields `/t/<token>`; manual entry uses the bare `<token>`. `tables/services.py`'s `parse_qr_payload()` normalises either form (plus a full URL, and stray pasted whitespace), so `POST /api/orders/qr/`'s `session_token` **body** field takes the raw scan unmodified. The by-session **URL** takes the bare token only — a `/` can't survive as a path segment — where the same helper just trims/rejects junk into a clean 404.
- **Public QR flow (restaurant-mobile)** — scan se order tak, chaar AllowAny calls: `GET /api/tables/by-session/{token}/` (table + uska `branch` + koi open order) → `GET /api/menu/?branch={table.branch}` (khana ka menu) → `POST /api/orders/qr/` (cart submit) → `wss://…/ws/table/?session={token}` (live status). **By-session response mein menu nahi hota** — woh jaan-boojh kar alag call hai (menu cacheable rehti hai, aur ek hi table response do alag cheezon ko mix nahi karta).


### orders
POS-area (owner/manager/cashier) via `IsPOSStaff` unless noted otherwise.
- **`GET /api/orders/`** — added just before Phase 11 (it was simply missing, and reports read from the same `Order` data). Filterable by `?status=`, `?table=`, `?from=`/`?to=` (date range on `opened_at`, `YYYY-MM-DD`), any combination; ordered newest-`opened_at`-first; branch-scoped and paginated like everything else.
- `POST /api/orders/` — same URL, get-or-create the open (non-closed/cancelled) order for a table; 200 if one already exists, 201 if created. One view (`OrderListCreateView`, `ListModelMixin` + `GenericAPIView`) handles both — `POST` is a bespoke get-or-create, not `CreateModelMixin`, so it couldn't just be `ListCreateAPIView`.
- `GET /api/orders/{id}/`, `GET /api/tables/{id}/open-order/`
- `POST /api/orders/{id}/items/` (snapshots `name_en`/`name_ar`/`price` from the product at that instant), `PATCH /api/orders/{id}/items/{item_id}/` (quantity ≤ 0 removes the item)
- `POST /api/orders/{id}/send-to-kitchen/` — `pending` items → `fired`, order → `sent`, deducts recipe stock per item (transactional, see `inventory`); 400 if nothing is pending
- `POST /api/orders/{id}/payments/`, `POST /api/orders/{id}/close/`
- All mutating endpoints reject a closed/cancelled order with 409 `error.orderClosed` (or `error.orderAlreadyClosed` for a second close)
- **`GET /api/kds/tickets/`** — kitchen-facing (`IsKitchenStaff`: owner/manager/kitchen, **not** cashier). Orders that have at least one item in `fired`/`preparing`/`ready`; disappears from the list once every item is `served`/`voided`. Reuses `OrderSerializer` rather than a bespoke ticket shape — kitchen doesn't need a different payload, just a filtered one.
- **`PATCH /api/orders/{id}/items/{item_id}/status/`** — kitchen-facing. Can set `preparing`/`ready`/`served`/`voided` only: 409 `error.itemNotFired` if the item is still `pending` (POS hasn't sent it yet), 409 `error.itemAlreadyFinal` if it's already `served`/`voided`, 400 `error.statusInvalid` for anything else (including trying to set `pending`/`fired` directly). **Deliberately does not touch `Order.status`** — that stays under POS's control (open/sent/closed/cancelled); the frontend derives kitchen-progress display from the items array itself, not a separately-maintained order-level aggregate, since the plan never specified an aggregation rule and inventing one risks guessing wrong against what the frontend actually expects.
- *(Phase 13)* public order creation (`source=qr`)

### inventory
Read is any authenticated staff; write is owner/manager only (`IsOwnerOrManager`) — matching `catalog`/`tables`.
- `GET/POST /api/inventory-items/`, `PATCH/DELETE /api/inventory-items/{id}/` — 409 `error.inventoryItemInUse` deleting one still referenced by a recipe.
- `GET /api/inventory-items/low-stock/` (`current_stock <= par_level`), `POST /api/inventory-items/{id}/adjust/` (manual `waste`/`adjustment` only — `sale`/`purchase` are automatic and rejected here with `error.reasonInvalid`).
- `GET /api/stock-movements/?item=` — read-only, append-only data.
- `GET/PUT/DELETE /api/products/{id}/recipe/` — replace-all shape (same as `catalog`'s deal endpoint). `PUT` rejects any `inventory_item` that doesn't belong to the product's own branch with 400 `error.inventoryItemInvalid` (`RecipeItemSerializer`'s FK field isn't branch-scoped on its own, so this is checked explicitly in the view).

### purchasing
Read is any authenticated staff; write is owner/manager only.
- `GET/POST /api/suppliers/`, `PATCH/DELETE /api/suppliers/{id}/` — 409 `error.supplierInUse` deleting one a purchase still references.
- `GET/POST /api/purchases/`, `PATCH/DELETE /api/purchases/{id}/` — 400 `error.purchaseItemsRequired` for an empty item list, 400 `error.inventoryItemInvalid` if any line references another branch's `InventoryItem`, 409 `error.purchaseAlreadyReceived` on PATCH/DELETE once received.
- `POST /api/purchases/{id}/receive/` — owner/manager only. Adds stock (`+quantity` per line, `reason=purchase`) and updates `InventoryItem.cost_per_unit` from `unit_cost`, sets `received_at`. Double-receive-safe the same way `send-to-kitchen` is double-fire-safe: an outer unlocked check for the fast path, `select_for_update()` on the `Purchase` row as the actual protection inside the transaction.

### customers
- *(Phase 13)* `GET /api/customers/`, internal find-or-create — no endpoints yet, matches the `Customer` model note above.

### reports
All read-only, owner/manager only (`IsOwnerOrManager`), all accept `?days=&from=&to=` via a shared `reports/filters.py`'s `parse_date_range()` — `?from=`/`?to=` (`YYYY-MM-DD`) win when given, `?days=N` means "the last N days including today", no params at all defaults to the last 30 days. All revenue-oriented reports (everything except `recent-orders` and `wastage`) count only **`CLOSED`** orders — an open tab isn't "sales" yet — via a shared `BaseReportView.closed_orders()`.
- `GET /api/reports/dashboard-summary/` — `total_sales`, `order_count`, `average_order_value` for the range, plus `open_orders_count` and `low_stock_count` as live (not range-scoped) operational context.
- `GET /api/reports/recent-orders/` — the one non-aggregate report; `?limit=` (default 25, capped at 100), full `OrderSerializer` objects, newest-`opened_at`-first.
- `GET /api/reports/sales-overview/` — `total_sales`, a `by_day` time series, and `by_payment_method` breakdown. Optional `?method=` narrows the payment breakdown only (daily/grand totals are unaffected).
- `GET /api/reports/product-performance/` — per-product `quantity_sold`/`revenue`, ordered by revenue. Optional `?category=`.
- `GET /api/reports/peak-hours/` — order count by hour-of-day (0–23), summed across the range. Footfall/volume timing, not revenue.
- `GET /api/reports/margins/` — per-product `revenue`/`cost`/`margin`. Optional `?category=`. **Cost uses the product's current `cost_price`, not a historical snapshot** — `OrderItem` only snapshots the selling price (by design, for pricing integrity), not cost, so a margin on an old sale reflects today's cost, not what it actually cost at the time. Documented limitation, not a bug; revisit by adding a cost snapshot to `OrderItem` if historically-accurate margins are ever needed.
- `GET /api/reports/table-turnover/` — per-table `order_count` and `average_duration_seconds` (`closed_at − opened_at`, explicit float — not the raw `timedelta`, to avoid relying on DRF's default duration serialization). Optional `?table=`.
- `GET /api/reports/wastage/` — per-inventory-item `quantity_wasted` and `estimated_cost` (× current `cost_per_unit`), sourced from `StockMovement` rows with `reason=waste`.
- `GET /api/reports/repeat-customers/` — customers with more than one order in range. **Aggregation logic is verified correct, but has no real data path feeding it yet**: `Order.customer` is only ever set by the public QR ordering flow (Phase 13), which doesn't exist. Every order placed through Phase 12 is POS-initiated with `customer=None`, so this endpoint will legitimately return an empty result set until Phase 13 lands — tested by setting `Order.customer` directly via the ORM (bypassing the nonexistent API) to confirm the aggregation itself works, not by pretending real customer data exists already.

**Validation errors return codes, not text.** Two layers, both implemented in Phase 3:
- Serializer field errors are codes because we write them ourselves, e.g. `serializers.CharField(error_messages={"required": "error.nameRequired"})` → `{"name_en": ["error.nameRequired"]}`. See `catalog/serializers.py` for the pattern every future app's serializers should follow.
- Generic framework exceptions (403/401/404/405/429) don't go through a serializer, so DRF's default English `"detail"` text would otherwise leak through — found this while testing Phase 2's `IsOwnerOrManager` 403s. Fixed with a global `EXCEPTION_HANDLER` (`common/exceptions.py`, `error_code_exception_handler`) that maps them to codes (`error.forbidden`, `error.notAuthenticated`, `error.notFound`, etc.). Applies retroactively to every endpoint, not just catalog's.

No Django gettext/`.po` setup.

---

## Auth

- `djangorestframework-simplejwt`, but **`Staff` is not `AUTH_USER_MODEL`**. `AUTH_USER_MODEL` stays Django's default `auth.User`, used only for `/admin/` superuser login (developers managing data directly) — completely separate from restaurant staff. Reasoning: PIN login isn't Django's username/password system, so making `Staff` a full `AbstractBaseUser` would mean building `create_user`/`create_superuser`/`is_staff` machinery and picking an awkward `USERNAME_FIELD` for a model with no natural unique identifier beyond PIN+branch — real complexity for a login flow that's entirely custom anyway. Simpler: `Staff` is an ordinary model, and a custom authentication class does the work.
- `POST /api/auth/pin-login/` looks up the staff record, checks the PIN via `check_pin()`, and issues a token with `RefreshToken.for_user(staff)` (SimpleJWT only needs a `pk`-like attribute, not an `AUTH_USER_MODEL` instance) — with `role` and `branch_id` added to the token claims.
- A custom `authentication.py` extends `JWTAuthentication` and overrides `get_user()` to look up `Staff` (instead of the default `auth.User` lookup) from the token's user-id claim, so `request.user` in every view is a `Staff` instance.
- Access token lifetime: 8h, refresh token 12h — long enough for a shift, refreshed silently so POS/KDS terminals don't force re-PIN mid-shift.
- Role enforcement via DRF permission classes in `accounts/permissions.py`: `IsOwnerOrManager`, `IsPOSStaff` (owner/manager/cashier), `IsKitchenStaff` (owner/manager/kitchen). Enforced server-side on every viewset — the frontend hiding a button is not access control.
- Rate-limit on `pin-login` via DRF's `ScopedRateThrottle` (`throttle_scope = "pin-login"`, `10/min`) — a 4-digit PIN is only 10,000 combinations. Set globally as the default throttle class in `REST_FRAMEWORK`, but it's a no-op for any view that doesn't set `throttle_scope`, so this doesn't rate-limit anything else by accident.
- **No `rest_framework_simplejwt.token_blacklist` app.** Found during testing: it hard-FKs `OutstandingToken.user` to `AUTH_USER_MODEL`, which breaks the moment `Staff` isn't that model. Rather than reverse the `Staff`-not-`AUTH_USER_MODEL` decision for this, logout is client-side only (`LogoutView` is a no-op that exists for API symmetry) — a logged-out token stays valid until it naturally expires (≤8h access / ≤12h refresh). Acceptable for an internal POS on physically-controlled terminals; revisit if that ever stops being true.
- **Not SimpleJWT's built-in `TokenRefreshView`.** Same root cause: its serializer re-validates the token's user-id claim against `get_user_model()` (`auth.User`). `accounts/views.py`'s `RefreshView` re-implements the same "mint a new access token from a valid refresh token" behavior directly against `RefreshToken`, without that lookup.

---

## Build order

- [x] **1. Scaffolding** — Django project (`config/`), all planned apps started, DRF + `django-environ` + `django-cors-headers` + `simplejwt` installed, `requirements.txt` frozen, `.env`/`.env.example`/`.gitignore` in place, `DATABASE_URL` read from env with a local-sqlite fallback (Neon URL to be plugged in by you). Plain WSGI dev server — no Channels yet.
  - [x] Shared `common.BaseModel` (soft delete + managers) + global DRF pagination — done ahead of individual app models, per plan.
- [x] **2. `accounts` + `branches`** — one seeded branch, PIN auth, JWT, roles. Unlocks permission-gating everywhere else. *(restaurant-admin login screen can go live here.)*
  - [x] `Branch`, `Staff`, `Shift` models + migrations generated (not applied — you run `migrate` against Neon yourself; verified locally against sqlite first).
  - [x] `pin-login`/`refresh`/`logout` endpoints, custom JWT authentication class (`accounts/authentication.py`), role permission classes (`accounts/permissions.py`), `StaffViewSet` (CRUD + shifts/clock-in/clock-out), `ActiveBranchView`/`BranchUpdateView`.
  - [x] End-to-end smoke-tested locally: PIN login (success/failure), role-gated staff list (owner/manager only, cashier blocked), self-vs-manager clock-in/out, duplicate-PIN-in-branch rejection, active branch fetch, refresh, logout.
- [x] **3. `catalog`** — categories/products/deals. Read open to any authenticated staff, write owner/manager only (see API endpoints section for why read isn't admin-only).
  - [x] `Category`, `Product`, `Deal`, `DealWindow` models + migrations generated (not applied).
  - [x] `CategoryViewSet`, `ProductViewSet` (`?category=&available=` filtering), `ProductDealView` (put/delete), global error-code exception handler added (`common/exceptions.py`).
  - [x] End-to-end smoke-tested locally: category/product CRUD + role gating, duplicate-name rejection, negative-price/missing-name rejection, category-in-use 409 block, deal set → price-too-high rejection → replace → delete → recreate (the OneToOne hard-delete fix specifically verified).
- [x] **4. `tables`** — CRUD (owner/manager) + open/close/needs-bill (POS-area: owner/manager/cashier).
  - [x] `Table` model + migration generated (not applied). `open()`/`close()`/`mark_needs_bill()` model methods.
  - [x] `TableViewSet` (CRUD + open/close/needs-bill actions, 409 block on deleting a non-empty table).
  - [x] End-to-end smoke-tested locally: role gating (cashier can open/close but not CRUD, kitchen can read but not open/close), session-token mint on open + QR code format, needs-bill blocking delete, close clearing token/QR/status, missing-label rejection.
  - [x] *(Added in Phase 5)* `close_table` now blocks with 409 `error.tableHasOpenOrder` if the table still has a non-closed/cancelled order — the plan's own "can't close a table with an open order" risk, only implementable once `Order` existed.
- [x] **5. `orders`** — full POS flow (create/add-item/send-to-kitchen/payments/close), staff-initiated only.
  - [x] `Customer` model added (unplanned as its own phase, but `Order.customer` needed it — see `customers` in Models).
  - [x] `Order`, `OrderItem`, `Payment` models + migrations generated (not applied).
  - [x] `OrderCreateView` (get-or-create), `OrderDetailView`, `TableOpenOrderView`, `OrderItemsView`/`OrderItemDetailView`, `SendToKitchenView`, `OrderPaymentsView`, `CloseOrderView`.
  - [x] End-to-end smoke-tested locally, including the snapshot-immutability rule specifically: changed a product's live price after adding it to an order, confirmed the order's total was unaffected. Also verified: get-or-create idempotency, quantity-0 item removal, send-to-kitchen status transitions, double-send/double-close rejection, mutating a closed order rejected, and the new table/order cross-check above.
  - [x] *(Retrofitted in Phase 5)* Branch scoping added to every viewset/view built so far (`common/mixins.py`'s `BranchScopedQuerysetMixin` + scoped lookups in `orders`), and a real bug in the Phase 3 exception handler fixed (404s weren't converting to error codes). See Risks section for both — found via a dedicated two-branch test, not by inspection.
- [x] **6. Real-time** — Django Channels, wired to broadcast the same event shape `restaurant-admin/src/lib/eventBus.ts` already emits locally.
  - [x] `daphne` + `channels` + `channels-redis` installed. `daphne` first in `INSTALLED_APPS` so `manage.py runserver` becomes ASGI-aware automatically. `config/asgi.py` wraps `http` (unchanged) and `websocket` (new) in one `ProtocolTypeRouter`.
  - [x] `CHANNEL_LAYERS`: `REDIS_URL` set → real Redis layer; unset → `InMemoryChannelLayer` (single-process only — fine for local `runserver`, **not** for a real multi-process deployment; set `REDIS_URL` for anything beyond local dev). Same "local convenience fallback, real value from you" pattern as `DATABASE_URL`.
  - [x] New `realtime` app: `BranchEventsConsumer` (one WS group per branch, `/ws/events/`), `JWTAuthMiddleware` (JWT arrives as `?token=` on the WS URL, since a WebSocket handshake can't carry an `Authorization` header the way `fetch()` can — **not** Channels' built-in `AuthMiddlewareStack`, which is for Django session/cookie auth this project doesn't use), `publisher.py`'s `publish_event(branch_id, name, entity_id)`.
  - [x] **Broadcasts wired via Django signals, not manual `publish_event()` calls in every view** (`realtime/signals.py`, `post_save`/`post_delete` on `Table`→`table:updated`, `Order`/`OrderItem`/`Payment`→`order:updated` (item/payment changes broadcast the *order's* id, not their own), `Product`/`Deal`→`product:updated`). This means every future endpoint that saves one of these models broadcasts correctly for free — nothing to remember. The one gap this doesn't cover: a bulk `queryset.update()`/`.delete()` fires no per-instance signal; currently the only such call (`SendToKitchenView`'s `pending_items.update(...)`) is always paired with an `Order.save()` in the same request, so `order:updated` still fires — keep that pairing in mind if a new bulk update is ever added elsewhere.
  - [x] End-to-end smoke-tested with `channels.testing.WebsocketCommunicator`: no-token and bad-token connections rejected before accept; a valid token is accepted and joins the branch's group; opening a table, creating a product, creating an order, and adding an order item each produced exactly the right broadcast (and creating a category — deliberately not wired — produced none). Specifically verified the `OrderItem`→order-id cross-reference, the one piece of this logic actually worth distrusting.
- [x] **7. KDS wiring** — kitchen ticket endpoints + item status updates.
  - [x] `KitchenTicketsView` (`GET /api/kds/tickets/`), `OrderItemStatusView` (`PATCH .../status/`), both `IsKitchenStaff`.
  - [x] End-to-end smoke-tested: role gating (cashier blocked from both endpoints, owner allowed same as kitchen), full status progression `fired→preparing→ready→served`, rejecting a jump back to `fired`, rejecting further updates on an already-`served` item, voiding an item, and the ticket list correctly dropping an order once all its items are `served`/`voided`.
  - [x] **The actual "confirm live updates reach KitchenQueuePage" check**: opened a real WebSocket authenticated as kitchen, then — from a *separate* HTTP client acting as cashier — added an item, sent it to kitchen, and PATCHed its status; the kitchen socket received a live `order:updated` broadcast for every one of those three actions. This is the first phase where the real-time plumbing built in Phase 6 was exercised against actual KDS traffic, not just a synthetic model save.
- [x] **8. `inventory`** — items, recipes, transactional stock deduction on send-to-kitchen.
  - [x] `Supplier` model added (unplanned as its own phase, same reason as `Customer` in Phase 5 — `InventoryItem.supplier` needed it). `InventoryItem`, `RecipeItem`, `StockMovement` models + migrations generated (not applied).
  - [x] `InventoryItemViewSet` (CRUD + `low-stock`/`adjust` actions), `StockMovementListView`, `ProductRecipeView`, `inventory/services.py`'s `adjust_stock`/`deduct_stock_for_order_item`.
  - [x] `SendToKitchenView` rewritten to wrap item-firing + stock deduction in one transaction with `select_for_update()` — the plan's own named concurrency risk.
  - [x] `inventory:updated` real-time broadcast added (`realtime/signals.py`), exactly as anticipated when Phase 6 said "adding this later is trivial."
  - [x] End-to-end smoke-tested: role gating, initial-stock-at-creation vs. ignored-on-PATCH, low-stock filtering, recipe set/replace, cross-branch recipe-reference rejection, a full order→send-to-kitchen→correct deduction→correct `StockMovement` row cycle, double-send-doesn't-double-deduct, manual waste adjustment, `reason=sale` rejected on the manual endpoint, delete-blocked-while-in-use, and full cross-branch isolation (list/detail/adjust all correctly scoped). Also re-ran Phases 5–6's smoke tests as a regression pass — all still green.
  - [x] **One real bug found by testing**: `SendToKitchenView`'s response showed items as still `pending` immediately after firing them, even though the DB was correct (confirmed by later checks). Root cause: `_get_order_or_404` prefetches `order.items`; a later bulk `.update()` on those same rows doesn't invalidate that cache, so re-serializing the same `order` instance returned stale data. Fixed by re-fetching the order fresh before serializing the response. See Risks section — this class of bug is worth remembering for any future view that prefetches then bulk-updates the same relation.
- [x] **9. `purchasing`** — suppliers, purchases, receive-stock flow.
  - [x] `Purchase`, `PurchaseItem` models + migration generated (not applied) — `Supplier` already existed from Phase 8.
  - [x] `SupplierViewSet`, `PurchaseViewSet` (CRUD + `receive` action, immutability guard on PATCH/DELETE once received).
  - [x] End-to-end smoke-tested: role gating, empty-items rejection, cross-branch inventory-item-in-a-line rejection, `status=received` blocked via plain PATCH, full receive cycle (stock added, cost updated, `StockMovement` recorded with `reason=purchase`), double-receive rejected, immutability after receiving (PATCH and DELETE both blocked), a not-yet-received purchase can still be edited/deleted freely, supplier-in-use delete block, full cross-branch isolation, and the `inventory:updated` broadcast firing from `receive()` (no new signal needed — `adjust_stock`'s existing `InventoryItem.save()` already covers it, exactly as anticipated). Re-ran Phase 8's inventory smoke test as a regression — still green. **No bugs found this phase** — first one since Phase 7 with a clean first pass.
- [x] **10. `accounts` round 2** — shifts/clock-in-out endpoints.
  - [x] **Nothing new to build**: `shifts`/`clock-in`/`clock-out` were already implemented on `StaffViewSet` back in Phase 2 (bundled in early since they're Staff-model actions, same pattern as pulling `Customer`/`Supplier` models into the phases that needed them). This phase was pure verification.
  - [x] Ran a fresh, more thorough regression than Phase 2's original test, since branch scoping (`BranchScopedQuerysetMixin`) and the exception-handler fix both landed *after* Phase 2 and had never been checked against shifts specifically: full clock-in→shifts-list→clock-out cycle, duplicate clock-in rejected, clock-out-when-not-clocked-in rejected, a peer cashier blocked from clocking a colleague in/out while a manager can, and — the actually new coverage — cross-branch isolation on clock-in/clock-out/shifts (all correctly 404, confirming `BranchScopedQuerysetMixin` covers these actions too since they route through `get_object()`). Re-ran Phase 2's original smoke test as well — still green.
- [x] **Ad hoc, before Phase 11: `GET /api/orders/`** — a plain orders list endpoint was simply missing (Phases 5–10 only ever built `POST`-to-get-or-create and `GET` single-order detail). Added filtering by `?status=`, `?table=`, `?from=`/`?to=` since reports (next) reads the same data and a filterable list was worth having regardless. `OrderCreateView` became `OrderListCreateView` (`ListModelMixin` + `GenericAPIView`, custom `post()` kept as-is). End-to-end smoke-tested: role gating, each filter individually and combined, date-range filtering (backdated an order to verify exclusion), newest-first ordering, cross-branch isolation, and that `POST` on the same URL still works unchanged. Re-ran Phase 5's orders regression — still green.
- [x] **11. `reports`** — all nine aggregation endpoints.
  - [x] `reports/filters.py`'s `parse_date_range()` (shared `?days=&from=&to=` parsing, 30-day default), `BaseReportView` (permission + `closed_orders()` helper). All 9 views in `reports/views.py`, no models of its own.
  - [x] Extra filters added beyond the plan's baseline, per request: `?method=` (sales-overview), `?category=` (product-performance, margins), `?table=` (table-turnover), `?limit=` (recent-orders).
  - [x] End-to-end smoke-tested against a hand-built, hand-verified dataset (2 products, 3 closed orders across 2 days with known prices/quantities/payment methods/durations, 1 open order, 1 cancelled order, a waste stock movement, a customer manually linked to 2 orders): **every single computed number matched the hand-calculated expectation exactly** — total sales, order counts, averages, per-product revenue/cost/margin, per-hour counts, per-table average duration, wastage cost, and the repeat-customer count. Also verified: role gating (cashier blocked from all reports), invalid-date rejection, the day-range filter actually excluding out-of-range data, and full cross-branch isolation (all reports return zeros/empty for a branch with no data, not another branch's numbers). **No bugs found.**
- [ ] **12. Full regression pass on restaurant-admin against the real backend**, all roles, before touching anything public-facing.
- [ ] **13. Public integration (last)** — `/api/menu/`, `/api/tables/by-session/{token}/`, public order creation, real-time broadcast to `burger_web`/`restaurant-mobile`. Only now do `submitQrOrder`/`qr-bridge.json`/hardcoded `WEBSITE_API_BASE_URL` get retired.

---

## Risks to keep in mind while building

- ~~**Stock deduction concurrency**~~ — **implemented in Phase 8** via `select_for_update()` in `inventory/services.py`'s `adjust_stock` (locks the `InventoryItem` row) and in `SendToKitchenView` (locks the order's pending `OrderItem` rows, so a double-submit of the same request can't double-deduct). **Important, honest caveat: this could only be logic-tested locally, not concurrency-tested.** The local dev DB is sqlite, and sqlite doesn't support real row-level locking the way Postgres does — `select_for_update()` is effectively a no-op there. Everything about *what* gets deducted and *when* was verified; the actual "two simultaneous requests can't both win" guarantee is exactly what Postgres provides and sqlite can't, so it has only been verified by code review, not by a real concurrency test. **Recommend a genuine concurrent-request test against Neon once it's connected** (e.g. fire two `send-to-kitchen` requests for orders sharing an ingredient at the same time and confirm the final stock reflects both deductions, not a lost update) before trusting this under real load.
- **New bug pattern found in Phase 8, worth remembering for any future view**: a queryset that's `prefetch_related()`d and then has a bulk `.update()`/`.delete()` run against those same rows will serialize *stale* data if the original instance is reused afterward — the prefetch cache isn't invalidated by a bulk operation that bypasses the ORM's per-instance save. Fix is to re-fetch fresh before serializing (done in `SendToKitchenView`), not to assume `manage.py check` or even most tests would catch this — it only showed up because the smoke test actually asserted on the *response body's* item status, not just the DB state.
- ~~**Snapshot immutability**~~ — **done in Phase 5**, and specifically verified: changing a product's live price after ordering it did not change the order's already-computed total.
- ~~**Branch scoping**~~ — **retrofitted in Phase 5**, once an audit found it was actually missing everywhere (Phases 2–4 built viewsets without it, since it caused no visible bug with only one branch in play). Fixed via `common/mixins.py`'s `BranchScopedQuerysetMixin` on every viewset (`StaffViewSet`, `CategoryViewSet`, `ProductViewSet`, `TableViewSet`), scoped lookups on every `orders` app view (`_get_order_or_404`/`_get_table_or_404`), and `branches`' `ActiveBranchView`/`BranchUpdateView` scoped to `request.user.branch` instead of "the only row". Verified with a dedicated two-branch isolation test: cross-branch reads return empty lists, cross-branch lookups by id 404 (not 403 — doesn't even confirm the row exists). **New app going forward: add `BranchScopedQuerysetMixin` (or equivalent scoped lookups) from the start — don't repeat this gap.**
- ~~**Order/table state transitions**~~ — **done in Phase 5**: `close_table` now blocks with 409 `error.tableHasOpenOrder` if the table has a non-closed/cancelled order; `send-to-kitchen` only ever touches `pending` items, so an already-fired item is never re-fired.
- **Found and fixed in Phase 5, worth knowing about**: the global exception handler (`common/exceptions.py`, added in Phase 3) silently didn't work for 404s. DRF's *default* handler converts Django's `Http404` to its own `NotFound` only in its own local scope — a wrapping handler that calls it and then inspects the original `exc` never sees that conversion, so `isinstance(exc, NotFound)` always failed and 404s leaked DRF's default `{"detail": "..."}` text right up until this was caught by testing (not by `manage.py check`, which can't catch this class of bug). Fixed by converting `Http404`/Django's `PermissionDenied` to their DRF equivalents at the top of `error_code_exception_handler` itself, mirroring what DRF's default handler does internally. If you ever add another exception type to `_CODE_MAP`, check whether it needs the same treatment.
