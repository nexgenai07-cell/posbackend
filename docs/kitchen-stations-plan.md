# Kitchen Stations — Plan

> Status: **Phase 1 code ho gaya** — backend (`kitchen` app, routing, KDS `?stations=`, notifications, bulk-station, import, tests) aur admin (station picker, category/product/staff forms, `/admin/kitchen-stations`, menu column + bulk set, KDS chips + greyed lines, setup checklist, en/ar).
> **Phase 2 code ho gaya** — §5 dekhein. **Phase 3 code ho gaya** — §6 dekhein. Migrations (`accounts 0007`, `branches 0007`, `orders 0015`, `orders 0016`, `orders 0017`) abhi chalni hain.
> Repos: `restaurant-backend`, `restaurant-admin`. `burger_web` (customer site) mein koi change nahi — customer ko ek hi order dikhta rahega.

---

## 1. Masla

Abhi kitchen screen (`KitchenTicketsView`, `/kds`) par **har kitchen staff ko har order ki har line** dikhti hai. Lekin har chef alag cheez banata hai — BBQ ka expert BBQ, koi fryer, koi desserts. Chahiye ke:

- Har chef ko **sirf apni cheezein** dikhein (jaise waiter ko sirf apne orders dikhte hain).
- Order **split na ho** — customer, waiter, cashier aur bill ke liye ek hi order rahe.
- Agar kuch set nahi kiya gaya, to **poora order as it is kitchen mein** jaye (koi cheez kabhi atke nahi).

## 2. Solution — Kitchen Stations

Har dish ek **station** (BBQ, Grill, Fryer, Desserts, Drinks…) se judi hoti hai. Har chef ek ya zyada stations mein hota hai. Order kitchen jate waqt **har line khud apne station par** chali jati hai — cashier ko har order par kuch chunna nahi padta.

```
Product "Mixed Grill"  ──► Station: BBQ   ──► Chef Ahmed, Chef Bilal (BBQ)
Product "Fries"        ──► Station: Fryer ──► Chef Sana (Fryer)
Product "Cola"         ──► (koi station nahi) ──► General ──► saare kitchen staff

Order #20: 2× Mixed Grill, 1× Fries, 1× Cola
  Ahmed/Bilal ki screen: 2× Mixed Grill   (+ greyed: Fries · Fryer, Cola · General)
  Sana ki screen:        1× Fries         (+ greyed: Mixed Grill · BBQ, Cola · General)
  General:               1× Cola
Waiter/cashier/customer: ek hi Order #20, har line ka apna status.
```

### Kyun station, kisi ek chef ko direct assign kyun nahi
- Ek station par 2 chef ho sakte hain → dono ko dikhe, jo pehle "Start" kare us ka naam lag jaye.
- Chef break / chhutti par ho → order us ke naam par nahi atakta, station ke dusre chef ko dikhta hai.
- Cashier par rush time mein har order ka bojh nahi.

## 3. Pehle se kya maujood hai (reuse hoga)

| Cheez | Kahan | Faida |
|---|---|---|
| Har line ka apna status (pending → fired → preparing → ready → served) | `OrderItem.status`, `OrderItemStatusView` | "BBQ ready, Fries abhi ban rahi" pehle se support hai |
| Order status lines se calculate hota hai | `recompute_order_status_from_items` | Order tab ready jab saari stations ki lines ready |
| `fired_at`, `ready_at` har line par | `OrderItem` | Phase 3 mein station-wise time report muft |
| Har staff ka apna realtime group | `staff_<id>_events` (`realtime/consumers.py`) | Notification sirf us station ke chefs ko bheji ja sakti hai |
| Kitchen ko notification | `_notify_role_staff(..., {StaffRole.KITCHEN})` | Isi ko station-aware banana hai |
| Setup checklist | `restaurant-admin/src/components/dashboard/SetupChecklist.tsx` | Naya step "Kitchen stations" |
| Excel import | `imports/importers.py` | Products ke saath `station` column |

---

## 4. Phase 1 — Detailed plan (core: setup + auto routing + chef ki screen)

### 4.1 Setup flow (user ki taraf se)

1. **Product add/edit karte waqt** "Kitchen station" dropdown.
   - List mein station na ho to wahin **"+ New station"** → naam (English/Arabic) likho → ban kar select ho jaye. Alag page par jana zaroori nahi.
   - Khaali chhoda → **General** (saare kitchen staff ko dikhta hai).
2. **Category par default station.** "BBQ Platters" category = BBQ → us category mein naya product add karo to station pehle se BBQ chuna hua aaye (badal sakte ho). Product ka apna station category se upar (override).
3. **Staff add/edit** — role Kitchen ho to **"Stations"** checkboxes. Ek chef ke kai stations ho sakte hain.
4. **Purane products** (menu pehle se bana hai):
   - Menu page par **bulk assign**: products select karo → "Set station: BBQ".
   - Excel import mein **`station`** column (naam se match; naya naam ho to station ban jaye ya error — decision #4 dekhein).
5. **Setup checklist** mein step: "Kitchen stations" — dikhaye ke kitne products bina station ke hain aur kis station par koi chef nahi.

### 4.2 Data model (backend)

**Naya model** `kitchen.KitchenStation` (nayi app `kitchen`, ya `catalog` ke andar — decision #5):

| Field | Type | Note |
|---|---|---|
| `branch` | FK Branch | branch-scoped, baaqi models ki tarah |
| `name_en`, `name_ar` | CharField | bilingual, `require_bilingual_pair` jaisa rule |
| `sort_order` | int | screen par tabs ka order |
| `is_active` | bool | delete ki jagah band karna (purane orders ka record bacha rahe) |

Unique: `(branch, name_en)` case/space-insensitive — tables wale label check jaisa.

**Fields jo add honge:**

| Model | Field | Note |
|---|---|---|
| `Category` | `station` FK null | default station |
| `Product` | `station` FK null | override; null = category ka, woh bhi null = General |
| `Staff` | `stations` M2M | sirf Kitchen role ke liye meaningful |
| `OrderItem` | `station` FK null | **kitchen bhejte waqt snapshot** — baad mein product ka station badle to purane/chalte orders na badlein. null = General |
| `OrderItem` | `started_by` FK Staff null | "Start" dabane wala chef (Phase 2 mein UI, field Phase 1 mein hi) |

"General" ek **asli row nahi** — `station = null` ka matlab General. Is se "station delete ho gaya" ya "set hi nahi" dono khud General mein gir jate hain.

Migrations: schema + `nothing to backfill` (purani lines null = General, jaisa abhi sab ko dikhta hai). Aap ke rule ke mutabiq migrations aakhir mein ek saath chalengi.

### 4.3 Routing — line station tak kaise jati hai

Ek helper `resolve_station(product)` → `product.station or product.category.station or None`.

Jahan line **fire** hoti hai (pending → fired), wahin `item.station = resolve_station(item.product)` save ho:
- `SendToKitchenView` (waiter/cashier "Send to kitchen")
- `OrderItemsView.post` — dispatched order mein add hua item seedha fire hota hai
- cashier review / koi bhi aur jagah jo `status = FIRED` + `fired_at` set karti hai (ek shared helper `fire_items(items)` bana kar sab ko us se guzaarna — taake koi raasta station bhool na jaye)

Agar station `is_active=False` ho → null (General).

### 4.4 Chef ki screen (KDS)

**API** — `GET /api/orders/kitchen/` (`KitchenTicketsView`) mein:
- `?stations=mine` (kitchen staff ka default): sirf woh orders jin mein **login chef ke stations ki ya General ki** active line ho.
- `?stations=all`: sab (head chef / manager / owner).
- `?stations=<id>,<id>`: kuch khaas stations.
- Response mein har line ke saath `station` (id + naam) — taake frontend apni lines highlight aur baaqi greyed dikha sake.

**Fallback:** kisi station par koi chef **assign hi nahi** → us ki lines General mein bhi dikhein (sab ko).

**Badla gaya (General):** "My stations" par ab General ki lines **nahi** aatin agar General ka koi chef duty par ho — General ka chef = jo kitchen staff kisi station mein nahi, ya head chef. Aisa koi chef na ho to General sab ki "mine" par (taake koi dish na atke); KDS hint batata hai. Notification bhi isi rule par. `GET /api/kitchen-stations/settings/` ab `mine_includes_general` bhi deta hai.

**Kaun bana sakta hai (sirf dekhna kaafi nahi):** chef sirf apne station, General, ya bina-chef station ki line Start/Ready kar sakta hai — "All" view par bhi dusre station ki line greyed rehti hai, server 403 `error.notYourStation`. Ticket line par `can_cook`. Station chef "General" chip daba kar General ki dishes bana sakta hai. Head chef/owner/manager sab. "Abhi login nahi" wala fallback (shift/online status) Phase 2 mein.

**Admin UI** (`KitchenQueuePage.tsx`, `KitchenTicketSlip.tsx`):
- Upar toggle: **My stations** | **All**, aur station chips (BBQ, Fryer, General…).
- Ticket par apni lines normal; usi order ki dusri stations ki lines **greyed** — "Fries · Fryer · cooking" — taake chef apni cheez time kar sake (decision #2).
- Ticket print (`KitchenTicketSlip`) sirf apni station ki lines (station printers Phase 3).

### 4.5 Realtime notifications

`_notify_role_staff(order, "order:kitchen_new", {StaffRole.KITCHEN}, ...)` ki jagah station-aware helper:
- Lines ke stations nikaalo → un stations ke chefs + agar koi line General ki ho to saare kitchen staff.
- Station par koi chef nahi → saare kitchen staff (fallback ke mutabiq).
- `order:changed` / `order:cancelled` bhi sirf us order ki lines wale stations ko.

Existing `staff_<id>_events` groups hi use honge — naya websocket group zaroori nahi.

### 4.6 Admin screens (setup)

| Screen | Change |
|---|---|
| Product form (`ProductFormPage.tsx`) | Station dropdown + "+ New station" inline; category ka default pehle se select |
| Category form (`EditCategoryPage.tsx` / new) | Default station dropdown |
| Menu list (`MenuPage.tsx`) | Station column + bulk "Set station" |
| Staff form (`EditStaffPage.tsx` / new) | Kitchen role par Stations checkboxes |
| Naya page: **Kitchen stations** (`/admin/kitchen-stations`) | List, add/rename, band karna, har station ke chefs aur dishes ki ginti |
| Setup checklist | "Kitchen stations" step |
| Excel import | `station` column (products sheet) |

Sab bilingual (en/ar locales), aur errors proper codes ke saath (`error.stationNameExists` waghera — en/ar translations ke saath).

### 4.7 Tests (backend)

- Product → category → General resolve order.
- Fire hote waqt station snapshot; baad mein product ka station badla to line nahi badalti.
- `?stations=mine` sirf apni + General lines wale orders; `all` sab.
- Station par chef nahi → General mein bhi dikhe.
- Notification sirf sahi chefs ko.
- Station `is_active=False` → naye fire General mein.
- Branch scoping: dusri branch ka station assign na ho sake.
- Order status: ek station ready, dusri cooking → order "preparing"; sab ready → "ready" (existing behaviour bachta hai).

### 4.8 Phase 1 mein kya **nahi**
- "Start" par chef ka naam UI mein, cashier override, "abhi login nahi" fallback → Phase 2
- Station printers, station-wise reports → Phase 3
- Customer site — koi change nahi

---

## 5. Phase 2 — Kaam ki taqseem aur override

- **Start / claim:** chef "Start" dabaye → `started_by` = woh chef, line "preparing". Usi station ke dusre chef ko "Ahmed is making this" dikhe — do log ek cheez na banayein.
- **Cashier override:** waiter assign karte waqt cashier dekhe order kin stations par ja raha hai; kisi line ko dusre station par bhej sake (jaise BBQ chef aaj chhutti par).
- **Koi chef login nahi** fallback: station ka koi chef us waqt online/on-shift nahi (attendance clock-in pehle se hai — `accounts` shifts) → lines General mein bhi.
- **Expo / head chef view:** saari stations ek saath, order "sab ready" hone par waiter ko handover.

### 5.1 Kya bana (code)

| Hissa | Backend | Admin |
|---|---|---|
| Start / claim | `OrderItem.started_at` naya; Start par `started_by` + `started_at`. Dusra chef Start dabaye → 409 `error.itemAlreadyStarted` + `started_by_name`. **Badla gaya:** start ho chuki line sirf wohi chef Ready/void kar sakta hai (dusron ke buttons disabled, "Only Ahmed can finish it"); head chef/owner/manager finish kar sakte hain. | KDS line par "Ahmed is making this" / "You're making this"; error toast mein naam. |
| Cashier override | `POST /api/orders/<id>/items/<item>/station/` `{station: id\|null}`. `OrderItem.station_moved_by` — fire hote waqt moved line apna station rakhti hai (station band ho gaya to General). Pending line: cashier/owner/manager/head chef. Fired (kisi ne start nahi kiya): sirf head chef/owner/manager. Preparing ke baad → 409. History event `item_station_moved`. Purane + naye station ke chefs ko notification. | Cashier screen (`OrdersPage`) par "Kitchen stations" panel: har waiting line kahan jayegi (dropdown) + "Moved" tag. Head chef KDS par fired line ka "Cook at: …" dropdown. Order item API ab `route_station` bhejti hai (fire se pehle bhi). |
| Clock-in fallback | `Branch.kitchen_requires_clock_in` (default **off**). On ho to station "staffed" tab hi jab us ka koi chef clocked-in ho (`Shift.clock_out` null) — warna lines sab ko. Screens aur notifications dono isi rule par. Setup checklist assignment hi dekhta hai, clock-in nahi. `GET/PATCH /api/kitchen-stations/settings/`. | Kitchen stations page par toggle "Only clocked-in chefs count"; chef ke naam ke saath "(not clocked in)". KDS "mine" hint ab on-duty dekhta hai. |
| Expo / head chef | `Staff.is_head_chef` (sirf kitchen role). Head chef = default `all`, har line ki notification, line move, handover. `GET /api/kds/tickets/?scope=expo` (head chef/owner/manager) — har station ki fired/preparing/ready lines + `all_ready`. `POST /api/orders/<id>/handover/` — sab ready ho to lines **ready hi rehti hain**, `OrderItem.handed_over_at` lagta hai (expo se hat jati hain), waiter ko `order:handed_over` ("pick it up"); served waiter hi table par karta hai. Warna 409 `error.orderNotReady`. | Staff form par "Head chef / expo" tick. KDS par "Expo / pass" tab: ready orders pehle, har line ka station + status, "Hand over to waiter" button. |

Tests: `kitchen/test_phase2.py` (claim, move, clock-in, expo).

### 5.2 Phase 2 ke faisle (default jo liye — badalna ho to batayein)

| # | Sawal | Liya gaya default |
|---|---|---|
| 7 | "Online" ka matlab? | Attendance clock-in (websocket presence nahi). Branch setting se on/off, default off — jo restaurant attendance use nahi karte un ka Phase 1 behaviour nahi badalta. |
| 8 | Expo alag role ya flag? | Kitchen staff par **flag** `is_head_chef` — naya role nahi (har jagah permissions badalni padtin). |
| 9 | Handover ka matlab? | **Badla gaya:** lines ready rehti hain, sirf `handed_over_at` + waiter ko "pick it up" notification. Served waiter hi karta hai (purana flow bacha rahe). |
| 10 | Kitchen mein pahunchi line kaun move kare? | Head chef / owner / manager. Cashier sirf bhejne se pehle. Start ho chuki line kabhi nahi. |
| 11 | Head chef ka naya flag kab lagu? | Agli login par (login payload se aata hai). |

## 6. Phase 3 — Hardware aur reports

- **Station printers:** har station ka apna kitchen printer — fire hote hi us station ki lines us printer par.
- **Reports:** har station ka average prep time (`fired_at` → `ready_at`), har chef ki lines, rush hours mein kaun sa station slow.

### 6.1 Kya bana (code)

| Hissa | Backend | Admin |
|---|---|---|
| Station printers | Printing browser se hi hoti hai (cloud server restaurant ke printer tak nahi pahunchta), is liye **station screen auto-print**. `OrderItem.printed_at` naya. `POST /api/kds/print/` `{stations: "3,general" \| "all", since}` — us station ki fired/preparing lines jo `since` ke baad aayin aur abhi print nahi huin, ek transaction mein "printed" mark karke wapas deta hai — do screens ek station par hon to bhi har line ek hi baar print. Line dusre station par move ho to `printed_at` saaf, naya station print kare. | KDS par "Station printer — auto-print on this screen": stations chuno (All / har station / General). On karte hi us waqt se aane wali lines print; har 15 sec aur har `order:kitchen_new` par check. Kai slips ek print job mein, har ek alag cut. Setting is device ki hai (browser storage). Chrome `--kiosk-printing` se dialog nahi aata. |
| Reports | `GET /api/reports/kitchen-stations/` (`?preset=` / `?from=&to=`, `?station=<id>\|general`). Line us din/ghante ki jab fire hui (branch timezone). Har station: lines, portions, voided, avg **wait** (fired→Start), **cook** (Start→Ready), **total** (fired→Ready) — har average ke saath kitni lines par hai — aur 3 sabse slow dishes. Saath mein din-ba-din × station, ghanta × station, aur chef-wise (jis ne Start dabaya). | `/admin/reports/kitchen-stations` (Reports overview par link "How long did each station take?"): KPI tiles, station table, din-ba-din table, chef table, busiest hours, CSV export (din-ba-din). |

Tests: `kitchen/test_phase3.py` (print claim, report).

### 6.2 Phase 3 ke faisle

| # | Sawal | Liya gaya |
|---|---|---|
| 12 | Printer kaise? | Station screen auto-print (aap ne chuna). Local print agent nahi. |
| 13 | Auto-print on karte waqt purani lines? | Print nahi — sirf on karne ke baad aane wali. |
| 14 | General lines kaun print kare? | Jis screen par "General" ya "All" tick ho; koi na kare to screen warning deti hai. |
| 15 | Phase 2 se pehle ki lines? | Sirf total time (Start ka waqt record nahi tha) — report yeh khud likhti hai. |

---

## 7. Aap ke decisions (OK se pehle)

| # | Sawal | Mera default (agar aap kuch na kahein) |
|---|---|---|
| 1 | Assign station ko ya khaas chef ko? | **Station** |
| 2 | Chef ko usi order ki dusri stations ki items greyed mein dikhein? | **Haan, greyed** |
| 3 | Head chef / expo hai jo saari stations dekhe? | Owner/Manager + "All" toggle har kitchen staff ke paas; alag expo role Phase 2 |
| 4 | Import mein naya station naam aaye to? | **Station khud ban jaye** (import preview mein dikhe ke naya bana) |
| 5 | Station model kahan? | Nayi app **`kitchen`** (catalog aur orders dono use karte hain) |
| 6 | Ek dish ke variants (Half KG / 1 KG) alag stations par? | **Nahi** — station product ka; zaroorat ho to baad mein |
