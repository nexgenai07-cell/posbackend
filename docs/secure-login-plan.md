# Secure Login — Plan

Status: **Implemented, awaiting test run and review** · Planned 2026-11-10 · Built 2026-11-11
Decisions 1–9 below were taken as recommended. One addition: a password set by someone else
(new account, owner reset, unlock with a temporary password) always sets `must_change_password`,
so the staff member picks their own on first login. Recovery for a locked Owner:
`python manage.py unlock_staff <username> [--password ...]` or the Django admin's "Unlock selected" action.
Scope: `restaurant-backend`, `restaurant-admin` (burger_web and mobile do not use staff login, so no changes there)

## Problem

How staff log in today:

- `GET /api/auth/login-choices/` is **public** and returns every active staff
  member's **name and role**. The login screen shows them in a dropdown
  (`restaurant-admin/src/pages/login/LoginPage.tsx`).
- The staff member picks their name and types a **4+ digit PIN**
  (`POST /api/auth/pin-login/`, `accounts/serializers.py` `PinLoginSerializer`).
- The only brute-force protection is an IP throttle of **10 tries a minute**
  (`config/settings.py` → `pin-login`). That allows about 14,000 guesses a day per IP.
  A 4-digit PIN has 10,000 combinations.

Why this is weak:
1. Anyone who opens the login page sees the full staff list, including who the **owner**
   is. That gives an attacker half of the credentials.
2. A PIN is short, numeric and easy to guess.
3. There is no per-account lockout, so a stolen or guessed PIN goes unnoticed.

## Goals

1. **No staff list before login.** Staff type a **username**; the dropdown is removed.
2. **Password instead of PIN**, with a minimum strength.
3. **Lockout:** after **3 wrong passwords in a row**, the account is locked and
   only the **super admin** can unlock it.
4. **New login screen:** split layout. Left side has an animated, 3D-looking
   restaurant illustration (a karahi with flames and steam). Right side has the form.

## Decisions (recommended, please confirm)

| # | Question | Recommendation |
|---|----------|----------------|
| 1 | Who is the "super admin"? | **Owner** role. There is no separate super-admin role today, and the Owner already holds the highest rights (ZATCA, etc.). |
| 2 | What if the **Owner** gets locked out? | Two developer-side ways in: a management command `python manage.py unlock_staff <username>`, and the Django admin (`/admin/`, which uses a separate developer account). The Owner cannot unlock themselves from the app. |
| 3 | Lock = `is_active = False`? | **No. Use a separate lock field.** "Inactive" means the person left the job. "Locked" means a security block. Keeping them separate means an unlock never re-activates someone who left, and reports and staff lists stay correct. |
| 4 | Can a manager unlock? | **No, Owner only**, as requested. (Could be extended to managers later.) |
| 5 | Existing staff have PINs. How do they move to passwords? | A migration copies each PIN hash in as the starting password and sets `must_change_password`. On first login the staff member uses their **old PIN as the password**, then must set a new password before doing anything else. Nobody is locked out on upgrade day. |
| 6 | Usernames for existing staff | The migration creates them from the name (e.g. "Ali Khan" becomes `ali.khan`, and a duplicate becomes `ali.khan2`). The Owner sees and can edit every username on the Staff page. |
| 7 | Password rules | At least **8 characters**, not all digits, and not the same as the username. Django's built-in validators do the check. |
| 8 | 3D animation tech | **SVG + CSS 3D transforms** (perspective, rotate, flicker, steam). No three.js: it would add about 600 KB to the login page for a decorative picture. It respects `prefers-reduced-motion`. |
| 9 | Does the response say "account locked"? | Yes. After 3 failures the user sees **"Account locked, ask the owner"**, which avoids repeated support calls. Trade-off: this confirms the username exists. An unknown username always gets the same generic "invalid username or password". |

## Milestone 1: Backend, username + password

| File | Change |
|------|--------|
| `accounts/models.py`, `Staff` | Add `username` (unique, stored lowercase), `password_hash`, `must_change_password` (bool), `failed_login_attempts` (int), `locked_at` (datetime, null), `locked_reason`, `unlocked_by` (FK Staff, null), `unlocked_at`. Add `set_password()`, `check_password()` and a `is_locked` property. Remove `set_pin`, `check_pin`, `pin_taken_in_branch` and `pin_hash`. |
| Migration 1 (schema) | Add the new fields |
| Migration 2 (data) | For each staff member: generate a username, copy `pin_hash` into `password_hash`, and set `must_change_password=True` |
| Migration 3 (schema) | Drop `pin_hash` |
| `accounts/serializers.py` | Replace `PinLoginSerializer` with `PasswordLoginSerializer` (username + password). `StaffSerializer` changes `pin` to `password` (write-only, validated) and adds `username`, `is_locked`, `locked_at`, `must_change_password` (read). Remove the PIN-unique check, since passwords no longer need to be unique. |
| `accounts/views.py` | `POST /api/auth/login/` replaces `pin-login`. `login-choices` returns **only the branch** (name and logo), with no staff list. |
| `POST /api/auth/change-password/` | Logged-in user: current password + new password. Clears `must_change_password`. |
| `accounts/authentication.py` | When `must_change_password` is set, the token works **only** for `change-password`, `logout` and `me`; every other API returns `403 error.passwordChangeRequired`. |
| `accounts/admin.py` (Django admin) | Username and password fields instead of PIN. A "Locked" column and an "Unlock" action. |
| `config/settings.py` | Rename the throttle scope `pin-login` to `login`, still 10 a minute per IP |

## Milestone 2: Backend, lockout + unlock

| Item | Behaviour |
|------|-----------|
| Wrong password | `failed_login_attempts += 1`, inside `select_for_update()` so parallel tries can't skip the count |
| 3rd wrong password in a row | Set `locked_at = now`. Response: `423 error.accountLocked`. All **Owners** of the branch get a realtime notification ("Ali's account was locked after 3 failed logins"). |
| Correct password | Counter goes back to 0 |
| Correct password while locked | Still refused with `error.accountLocked`. Even the right password does not open a locked account. |
| Already-issued tokens | A locked staff member's existing sessions stop working (`StaffJWTAuthentication` rejects locked staff, the same way it rejects inactive staff) |
| `POST /api/staff/<id>/unlock/` | **Owner only.** Clears the lock and counter, and records `unlocked_by` and `unlocked_at`. Optional `new_password` sets a temporary password, which also sets `must_change_password`. |
| `POST /api/staff/<id>/reset-password/` | **Owner only.** Sets a temporary password and `must_change_password`. Used when someone forgets their password. |
| `python manage.py unlock_staff <username> [--password]` | Recovery path for a locked Owner |
| Login response for a wrong password | `400 error.invalidCredentials` + `attempts_left` (2, then 1). The UI shows "2 attempts left". |
| Audit | The lock, the unlock and who did it are stored on the staff row. The history also goes to the log (`accounts.auth`). |

Tests: wrong ×3 locks; correct after lock refused; unlock by Owner works;
unlock by Manager/Cashier gets 403; counter resets on success; a locked
staff member's token is rejected; parallel wrong attempts still lock at 3;
`must_change_password` blocks other APIs; unknown username gives the generic error;
migration turns an old PIN into a working first-login password.

## Milestone 3: Admin, login screen UI

**Layout** (`pages/login/LoginPage.tsx`):

```
┌──────────────────────────────┬─────────────────────────┐
│                              │  [logo]  Brand · Branch │
│   Animated karahi (SVG)      │                         │
│   - 3D tilt (perspective)    │  Welcome back           │
│   - flame flicker below      │                         │
│   - steam curls rising       │  Username  [_________]  │
│   - spices / sparks float    │  Password  [_______ 👁] │
│                              │                         │
│   "Fresh from our kitchen"   │  [      Sign in      ]  │
│   (brand tagline, en/ar)     │                         │
│                              │  ⚠ 2 attempts left      │
└──────────────────────────────┴─────────────────────────┘
```

- **Left panel:** new `components/login/KarahiScene.tsx`, an inline SVG with CSS
  keyframes. It tilts gently in 3D, the flames flicker, steam rises, and spices
  float. The colours come from the theme tokens (ember gradient).
  `prefers-reduced-motion` shows a still picture.
- **Right panel:** username, password with a show/hide button, sign in, and a language switcher.
- **Messages:** wrong password shows "Invalid username or password · 2 attempts left".
  A locked account shows a red box: "Account locked. Ask the owner to unlock it." After 10
  tries a minute from one IP, the existing "too many attempts" message.
- **Responsive:** on a phone or narrow tablet the illustration becomes a small header above the form.
- **RTL:** uses logical CSS (`ps`/`pe`, `start`/`end`), so in Arabic the illustration
  moves to the right side automatically. `npm run check:rtl` must pass.
- **Change password screen:** new `pages/login/ChangePasswordPage.tsx`. It opens straight
  after login when `must_change_password` is set (current password, new password, confirm),
  with a live strength hint.

Files: `lib/api/auth.ts` (`loginWithPassword`, `changePassword`, error reasons
`invalid`/`locked`/`throttled`), `context/AuthContext.tsx` (`mustChangePassword`
redirect), `types/Staff.ts`, translations in `auth.json` and `errors.json` (en + ar).

## Milestone 4: Admin, staff management

| Page | Change |
|------|--------|
| `NewStaffPage.tsx` | Username + password (+ confirm) instead of PIN |
| `EditStaffPage.tsx` | Edit the username. "Reset password" (Owner only) sets a temporary password. |
| `StaffListPage.tsx` / `StaffDetailPage.tsx` | A red **"Locked"** badge with the lock time. An **"Unlock"** button that only the Owner sees (with an optional new temporary password). |
| Notifications | The realtime "account locked" alert reaches Owners through the existing notification toasts |
| `lib/api/staff.ts`, `types/Staff.ts` | New fields and the `unlock` / `resetPassword` calls |
| Translations | `staff.json` (en + ar) |

## Rollout order (important)

1. Deploy the backend (Milestones 1–2) **and** the admin (Milestones 3–4) **together**. The
   old login screen calls `pin-login`, which no longer exists.
2. After migrating, the Owner opens the Staff page and tells each person their username.
   Each person's first password is their old PIN, and they must change it on first login.
3. Check that `python manage.py unlock_staff` works on the server before go-live.

## Out of scope (later)

- Two-factor login (OTP by SMS)
- Password expiry (e.g. every 90 days)
- Letting managers unlock
- Login history page (every successful or failed login with IP and time)
