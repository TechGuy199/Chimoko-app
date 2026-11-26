# Chimoko POS (MS-POS): Developer Specification

This describes the web app in `web/` in enough detail to rebuild its **structure, look and behaviour** in another
codebase. It is written so a developer, or another Claude session, can follow it step by step.

Paths are relative to `web/`. Where this spec and the code disagree, the code wins: copy from these files rather than
retyping:

| Copy verbatim | Why |
|---|---|
| `public/static/styles.css` | The whole design system (tokens, 21 themes, every component) |
| `public/static/mobile.css` | The phone layout (loaded only below 760px) |
| `templates/base.html` | The app shell: sidebar, top bar, tab bar, menus, flashes, script loading |
| `public/static/nav.js` | In-place navigation (no white flash between pages) |
| `public/static/dialog.js` | Overlay edit dialogs |
| `public/static/notify.js` | Alerts polling, toasts, the badge, push notifications |
| `public/static/print.js` | Printing on A4 or 80/58mm receipt printers |
| `public/static/splash.js` | The launch screen (rebrand it) |
| `public/sw.js` | Service worker: installable PWA and offline page copies |
| `templates/_appearance.html` + the theme constants in `app.py:306-334` | The per-user appearance picker |

---

## 1. Architecture at a glance

| Layer | Choice | Notes |
|---|---|---|
| Server | **Flask 3**, one module `app.py` (~13.5k lines, ~200 routes) | Server-rendered HTML. No SPA framework. |
| Templates | **Jinja2**, every page `{% extends "base.html" %}` | Blocks: `title`, `content`, `scripts` |
| Database | `db.py`: **Postgres** (psycopg 3) when `DATABASE_URL` is set, **SQLite** file otherwise | One SQL dialect: `?` placeholders, rewritten to `%s` for Postgres |
| Front end | Plain JS files, one per feature, no build step | Configured through `data-*` attributes on their `<script>` tag |
| CSS | Two hand-written files driven by CSS custom properties | No Tailwind, no preprocessor |
| Hosting | **Vercel** (Python serverless) | `public/**` served from the CDN; crons in `vercel.json` |
| AI | Google Gemini (`google-genai`) in `assistant.py` / `assistant_tools.py` | Optional; the app works with it off |
| Exports | `reportlab` (PDF), `XlsxWriter` (Excel), CSV | |
| Push | `pywebpush` + VAPID keys | |

### Database helper (`db.py`)
```python
db = get_db()                  # one per request, stored on flask.g, closed at teardown
db.one(sql, params)  -> dict | None
db.all(sql, params)  -> list[dict]
db.insert(sql, params) -> new id      # appends RETURNING id
db.execute(sql, params) -> cursor
db.commit() / db.rollback()
```
- The schema is a list of `CREATE TABLE IF NOT EXISTS` strings with `{pk}` filled in per engine (`SERIAL PRIMARY KEY` or `INTEGER PRIMARY KEY AUTOINCREMENT`).
- New columns go through `add_column(db, table, column, definition)` at startup. That is the migration system: additive only.
- `startup_signature()` / `startup_done()` skip schema work on warm serverless starts.
- Rule: SQL must run on both engines. Write plain SQL, never put a literal `%` or `?` inside it, and always alias aggregates (`COUNT(*) AS n`).
- Postgres connections are kept between requests and retried once if the server closed them.

---

## 2. Request conventions (apply these to every route)

1. **Post/Redirect/Get, always.** A save is a form POST that ends in `redirect(url_for(...))`, with a `flash()` message. A page is never rendered in reply to a POST.
2. **Flash categories:** `ok` (green), `error` (red), `notice` (amber). `base.html` renders them as `<div class="flash {{category}}">` above the content.
3. **CSRF on every POST.** `{{ csrf_field() }}` goes inside every form. A `before_request` hook compares `session["csrf"]` with `request.form["csrf"]` using `hmac.compare_digest` and returns 400 if they differ. JS `fetch` POSTs send the token as a form field too; scripts get it from `data-csrf` on their tag.
4. **Auth decorator:** `@need(None)` means any signed-in user, `@need("admin")` means admins only, and `@need("sell"|"procure"|"transfer"|"delete")` checks a per-user permission. Failing sends the user to login, or flashes "Your account does not have permission…" and redirects.
5. **`g.user`** is loaded in `before_request` by one SQL join (user + business settings) and exposed to templates as `user`.
6. **Template globals** (context processor): `csrf_field`, `csrf_token`, `user`, `can(perm)`, `look` (appearance), the theme/font/accent lists, `asset()`.
7. **Cache busting:** `asset("styles.css")` makes `/static/styles.css?v=<git sha>`. Never link static files without it.
8. **Form helpers:** `text(key)`, `number(key, kind, default)` (raises `ValueError`), `checked(key)`.
9. **Money:** Jinja filters `|money` (2dp with thousands separators and the business currency), `|money4`, `|signed`, `|month`.
10. **CSV exports** go through `csv_response()` and `csv_safe()`, which guards against formula injection. Every list page with an export has a sibling `.csv` route.
11. **Timezone:** every "now" goes through `now()`, which uses `APP_TIMEZONE` (default `Africa/Lusaka`). Dates are stored as `TEXT` (`YYYY-MM-DD HH:MM:SS`).
12. **`Server-Timing` header** on every response, for debugging slow pages.
13. **FollowSaves WSGI middleware** (`app.py:68`): when `nav.js` sends a POST with `X-Nav: follow`, the server follows the redirect itself and returns the next page in the same response, with `X-Nav-Url`. This saves a round trip on slow networks. Copy it as-is.

### Multi-tenancy (critical)
- Every business-owned table has `business_id INTEGER NOT NULL`, and uniqueness is per business: `UNIQUE (business_id, item_code)`.
- **Every** query filters on `business_id = g.user["business_id"]`. Fetching by id is always `WHERE id=? AND business_id=?`, so another business's id returns 404.
- `tests/test_business_separation.py` signs in as two businesses and proves neither can see or change the other's records. Write the same test in the other app.
- Signup is open unless `ALLOW_SIGNUP=0`. The first user of a business is its `admin`.
- **Demo companies:** a one-click 24-hour demo filled with sample data (`fill_demo_data`). Expired demos are swept on requests (`sweep_expired_demos`) and deleted with their logins.
- Account deletion: a user can delete their own login, and an admin can delete the whole business. Both require typing a password or a confirmation phrase, and the last admin cannot delete themselves.

### Roles and permissions
- `users.role` is `admin` or `operator`.
- Operators have flags: `can_sell`, `can_procure`, `can_transfer`, `can_delete`.
- Navigation links marked admin-only are hidden from operators (see the `nav` list in `base.html`).
- **Permission requests:** an operator can ask an admin to allow one action on one item. The request becomes an alert for admins, who approve or decline it at `/requests`.

---

## 3. Design system

### 3.1 Principles
- **Dark by default** (`Midnight`). It has a translucent "glass" look whose strength is a per-user slider.
- Every colour, radius, size and spacing value is a CSS custom property on `:root`. **No component hard-codes a colour.** Themes only redefine tokens.
- Calm, dense, data-first screens: uppercase small headings for sections, large tabular numbers for figures.
- Every clickable card shows that it opens something (see 3.6).
- Respect `prefers-reduced-motion`: all animation is turned off.
- Print is always black on white, whatever the theme.

### 3.2 Tokens (`styles.css` top)
```
Surfaces  --bg  --surface  --surface-alt
Ink       --ink (an "r, g, b" triple laid over surfaces: borders, hovers, fields → rgba(var(--ink), .04-.14))
Text      --text  --text-muted  --text-bright  --placeholder  --on-accent
Brand     --primary  --primary-hover  --accent (links/highlights)  --primary-soft
Status    --success(-hover)  --danger  --ok-text  --warn-text  --gold-text  --ok-soft --warn-soft --gold-soft
Depth     --shadow  --shadow-soft  --glow-1..3 (background glows)
Shape     --radius 12px  --radius-small 8px   (phones: 18px / 12px)
Motion    --ease cubic-bezier(0.2, 0.7, 0.2, 1)
Type      --font-family  --font-fallback
          --text-xs 12 | sm 13 | md 15 | lg 17 | xl 20 | 2xl 26 (px)
Space     --space-1..6 = 4, 8, 12, 16, 20, 28 px
Layout    --side-w 248px (64px folded)
Charts    --series-1 #3987e5  --series-2 #d95926  --chart-grid
Derived   --line / --line-strong        = rgba(var(--ink), .08 / .14)
(from     --blur                        = var(--glass) * 26px
 --glass) --card-bg / --tile-bg / --bar-bg / --panel-bg / --side-bg = color-mix(surface, transparent by --glass)
```
Midnight values: bg `#0c0d13`, surface `#14151d`, surface-alt `#1c1d28`, text `#e6e9f0`, muted `#8b93a7`,
primary `#3b82f6`, accent `#8ab4ff`, success `#1f9d55`, danger `#e5484d`.
Daylight: bg `#f2f4f9`, surface `#fff`, text `#1f2937`, primary `#2563eb`.

### 3.3 The appearance attributes on `<html>`
```html
<html data-theme="dark" data-accent="" data-size="normal" data-font="inter" data-spacing="normal" style="--glass: 0.5">
```
| Attribute | Values | Effect |
|---|---|---|
| `data-theme` | 21 themes. Dark: dark (Midnight), forest, ocean, sunset, graphite, contrast, slate, plum, coffee, nord, emerald, crimson. Light: light (Daylight), sand, mint, paper, sky, blush, lavender, stone. Plus `system`, which follows the OS | Redefines tokens |
| `data-accent` | `""` (the theme's own), blue, green, teal, purple, orange, rose, gold | Overrides `--primary`; `--accent` is mixed from it |
| `data-size` | normal, large (`zoom:1.08`), larger (`zoom:1.16`) | Accessibility |
| `data-font` | inter, jakarta, manrope, dm, outfit, lexend, atkinson, plex, system | Sets `--font-family`. Only the chosen Google Font is loaded, non-blocking (`media="print" onload`) |
| `data-spacing` | compact, normal, relaxed | line-height, letter-spacing, table cell padding |
| `--glass` | 0–1 (stored as 0–100) | Transparency and blur of cards and bars |

- Stored per **user** (`users.theme, glass, accent, text_size, font, text_spacing`), so the look follows them across devices.
- Mirrored into a `look` cookie so signed-out pages (login) use the last look on that device.
- `/appearance` saves in the background when a choice changes (`X-Requested-With: fetch` → 204), with a plain form POST as the fallback. Inputs are validated against the constant lists.
- The picker (`_appearance.html`) shows swatch cards split into Dark and Light groups, accent dots, "Ag 123" font cards, segmented controls for size and spacing, and a range slider for transparency.

### 3.4 Typography
- Body is 15px, line-height 1.6, antialiased.
- `h1`: 26px, weight 650, letter-spacing -0.018em, `--text-bright`. **One per page.**
- `h2`: **section labels**. 12px, uppercase, letter-spacing 0.08em, `--text-muted`. Every card's title is an `h2`.
- `h3`: 17px, weight 650, for sub-headings.
- Numbers use `font-variant-numeric: tabular-nums`. Table headers are 11.5px uppercase muted.
- On phones, fields are 16px so iOS doesn't zoom in on focus.

### 3.5 The app shell (`base.html`)

```
┌──────────────┬──────────────────────────────────────────────────┐
│ ▣ Business   │ Page name                 [Alerts ③] [user ▾]   │ ← .topbar (sticky, blurred)
│──────────────│──────────────────────────────────────────────────│
│ HOME         │  flash messages                                  │
│  ▢ Overview  │  <main> (max-width 1680, padding 28/28/56)       │
│ SELL         │    {% block content %}                           │
│  ▢ Sales&POS │                                                  │
│  ...         │                                                  │
│──────────────│                                                  │
│ ⇤ Fold menu  │                                                  │
└──────────────┴──────────────────────────────────────────────────┘
  .sidebar (≥1024px only, fixed, 248px, folds to 64px icons; folded state kept in localStorage "ms-side")
```
- **Navigation is data.** A Jinja list of groups → links: `(label, endpoint, [endpoints that count as active], admin_only, svg_path)`. Groups are Home, Sell, Stock, Insights, People, Business. The same list renders the sidebar, the top bar's Menu dropdown and the phone's bottom sheet. The current page's label goes in `.here` in the top bar.
- Icons are inline 24×24 SVG `path`s drawn as strokes (`fill:none; stroke:currentColor; stroke-width:1.7; round caps and joins`). There's no icon font.
- The active link has a `--primary-soft` background, a primary-coloured icon and a 3px primary bar on its left edge.
- **Tablets (760–1024px):** no sidebar. The top bar shows the business name and a **Menu** dropdown, a two-column grid of groups.
- **Phones (<760px):** a compact header, plus a fixed **bottom tab bar** with the 4 most-used pages and **More**. More opens the full menu as a **bottom sheet**: a dimmed backdrop, rounded 24px top corners, a drag handle and a two-column grid of 44px-tall items. The tab bar respects `env(safe-area-inset-bottom)`.
- Top bar dropdowns are `<details class="menu">` with a `.menu-panel`. Only one is open at a time; a click outside or Esc closes them (the script is in `base.html`).
- Header items: **Alerts** (with a `.badge` count, lit in the colour of the most pressing alert) and a user menu (My Account & Appearance, Work offline, Logout).
- A demo company shows a permanent `flash notice` banner with its expiry time.

### 3.6 Page anatomy and component classes

A typical list page, top to bottom:
```html
<div class="page-head">                        <!-- h1 left, .head-actions buttons right, wraps -->
  <div><a class="back" href="…">← Back</a><h1>Suppliers</h1></div>
  <div class="head-actions"><a class="btn primary">New …</a></div>
</div>
<form method="get" class="toolbar">            <!-- search + filters; selects submit on change -->
  <input type="search" name="q" placeholder="Type a name or phone number" autofocus>
  <select onchange="this.form.requestSubmit()">…</select>
  <button class="btn primary">Search</button> <a class="btn">Clear</a>
</form>
<div class="stats">                            <!-- summary tiles, auto-fit 160px -->
  <div class="stat"><span>Owed</span><strong class="warn">ZMW 1,200.00</strong><small>across all accounts</small></div>
</div>
<section class="card">
  <div class="table-wrap"><table>…</table></div>
</section>
```

| Class | What it is |
|---|---|
| `.card` | The basic panel: 20px padding, `--card-bg`, 1px `--line` border, `--radius`. `.card.block` adds a top margin, `.card.narrow` is a centred 420px card (login), `.narrow-form` is 560px max |
| `.card-head` | A flex row at the top of a card: `h2` on the left, a muted link on the right that gets an automatic " →" |
| `.stats` > `.stat` | A grid of summary tiles. `span` is the uppercase label, `strong` the big number, `small` the note |
| `.kpis` > `a.kpi` | Dashboard KPI cards: `.kpi-label`, `.kpi-value` (with a `<small>` currency), `.kpi-foot`, `.kpi-spark` (an SVG polyline). `.kpi.lead` is double width |
| `.delta.good/.bad/.flat` | A pill for change against the previous period, always with ▲/▼ so colour is never the only signal |
| `.dash-grid` + `.span-4/5/6/7/8/12` | A 12-column dashboard grid at ≥1100px, one column below |
| `.cards` | An auto-fit grid of cards, min 260px |
| `.dept-tiles` > `a.dept-tile` | Big pickable tiles (categories, departments, plots). `.active` marks the chosen one |
| **Clickable cards** (`a.card`, `a.stat`, `a.kpi`, `a.dept-tile`, `.card-link`) | A chevron in the top-right corner, a 2px lift with a primary border on hover, and every 7s a thin line of primary light sweeping across, staggered along a row (`card-sweep` keyframes). A card that doesn't open anything stays flat and still |
| `.rows` > `li` > `a` | List rows inside a card: an `.avatar` or `.chip`, then `.row-main` (strong + small, ellipsised), then `.row-end` (right-aligned number + small), and a chevron |
| `.chip` (`.ok .warn .gold .info .plain`) | A status pill with a dot. Use it for any value from a short fixed list (Paid / Partial / Unpaid) |
| `.avatar` | A 30px circle with the initial in `--primary-soft` |
| `.empty` | An empty state: centred, `<strong>` title plus one line of guidance and a link to the action that fills it. Every list has one |
| `.flash.ok/.error/.notice` | Messages, also used inline for warnings ("3 pairs look like duplicates – Review") |
| `.btn` (`.primary .success .danger .ghost .small .wide`) | Buttons. `.danger` is a tinted outline that fills red on hover. `:active` scales to 0.98 |
| `.segmented` | A pill group of radio labels or links; the selected one is filled with primary |
| `.toolbar` | A wrapping flex row for search and filters |
| `.grid` | An auto-fit form grid (220px columns, 14px gap). Labels wrap their inputs: `<label>Name <input></label>` |
| `label.check` | Checkbox plus text in a row |
| `table` | Full width; cells have a 1px `--line` bottom border; uppercase muted headers; `.num` is right-aligned tabular; `td.actions` is a right-aligned flex row of buttons; `.total-row` |
| `.split` > `.grow` + `.side` | Main column plus a 340px side column. Stacks below 1500px |
| `.facts` (`dl`) | Two-column label/value list |
| `.invoice`, `.statement`, `.payslip` | Printable documents: a centred header, `dl` totals, a footer |
| `.muted .ok .warn .gold .accent` | Text colour utilities |
| `.no-print` / `.print-only` | Print control |
| `.trend`, `.rank`, `.chart` | Charts drawn by the server as inline SVG (no chart library). `.rank` is a horizontal bar list. Hover tips come from `data-tip` |
| `.danger-zone` | A red-bordered section for destructive settings |
| `details.fold` / `data-fold-mobile` | Collapsible sections that start folded on phones (unless `data-fold-mobile="open"`) |
| `dialog.modal` > `.modal-body` | Overlay editor (see 3.8) |
| `.toasts` > `.toast` | Alert pop-ups (`notify.js`) |
| `.badge` | A count bubble |

### 3.7 Motion
- `main` fades and rises 8px when a page arrives (`page-in`, 0.2s). Stats and tiles rise with a 30ms stagger.
- Menus scale in from 0.97. The phone sheet slides up (`sheet-in`).
- `@view-transition { navigation: auto }` cross-fades pages, and the top bar and sidebar stay put.
- `nav.js` shows a 3px primary progress bar at the top (`#nav-progress`) only when a load is slow. `html.nav-quiet` skips the entrance animation when only filters changed.
- All of it is off under `prefers-reduced-motion`.

### 3.8 Behaviour wired by data attributes (no per-page JS needed)
| Attribute | Behaviour | File |
|---|---|---|
| `<form data-confirm="Delete this?">` | Native confirm before submitting | `base.html` |
| `<button data-modal-open="id">` + `<dialog id="id" class="modal">` | Opens the overlay. `[data-modal-close]`, Esc or a click outside closes it. A save closes it at once and the page updates in place; if the server rejects the save, the dialog reopens with the error and the typed values | `dialog.js` |
| `data-full-reload` | Opt out of in-place navigation (uploads, offline page) | `nav.js` |
| `data-no-prefetch` | Don't preload this link on hover | `nav.js` |
| `data-print` / `data-print="choose"` | Print. The printer choice (A4, 80mm, 58mm) is remembered per device; receipt printers get the `[data-thermal]` block | `print.js` |
| `data-tray` | Opens a side panel over the page ("who bought this product") | `tray.js` |
| `data-merge` + checkboxes with `form="…"` | Tick-to-merge bar ("Nothing ticked yet." → "3 ticked") | `merge.js` |
| `data-tip` | Chart hover tooltip | page scripts |
| `details[data-fold-mobile]` | Starts folded on phones | `base.html` |
| `document` event `nav:swapped` | Fired after each in-place page change. Page scripts rebind on it | `nav.js` |

**In-place navigation (`nav.js`):** it intercepts same-site links and form submits, fetches the next page, and swaps `<main>` and `#page-scripts`. It preloads a link when the pointer rests on it (used once, for a few seconds, and thrown away by any save), serves Back/Forward from memory and then refreshes, lets the latest click win, prevents double submits (`form.is-sending`), and restores scroll. If anything goes wrong it falls back to a full navigation. Because the server always returns full HTML pages, **no route needs to know about it**.

### 3.9 Phones (`mobile.css`, `media="(max-width: 760px)"`)
- Radius goes to 18px, pill buttons, touch targets of at least 44px, 16px inputs.
- `main` gets bottom padding for the tab bar.
- Wide tables scroll sideways edge to edge.
- The order of sections is changed with CSS `order` so the most-used actions come first (e.g. on an item page: use it, then plant it, then details, then corrections, then delete).
- Blur is turned off on tiles on touch devices, for smooth scrolling.
- KPIs show two per row, with the lead KPI full width.

### 3.10 Print
`@media print`: `.no-print` is hidden, the sidebar and padding are removed, everything is black on white, and cards lose their borders and shadows. Documents (invoice, statement, payslips, delivery list) are designed to print. Receipt printers get a narrow layout whose page height matches the receipt, so the printer cuts at the end.

### 3.11 Launch screen and PWA
- `splash.js` runs in `<head>` and covers the first paint: a book opens and a pen writes (about 1.5s), with a tip underneath. It ends when the page is ready (never after 4s), shows once per tab, and can be skipped with Esc or Skip. It's pure CSS and SVG.
- `/manifest.json` is generated per business, so the installed app carries the business name. The icons are in `public/static/icons/` (SVG, maskable 192/512, apple-touch, favicon). The `theme-color` is `#2563eb`.

### 3.12 Copy and tone (part of the "format")
- Plain English, no jargon, written for the shop floor: "Type a supplier's name or phone number", "Nothing ticked yet.", "This cannot be undone."
- Every empty state says what will appear there and links to the action that creates it.
- Destructive actions say exactly what will happen.
- Code comments explain *why*, in full sentences, at the top of every file, function and template.

---

## 4. Cross-cutting systems

### 4.1 Alerts and notifications
- Table `notifications(business_id, for_user_id NULL = everyone, kind, title, body, link, created_at, read_at, wa_status, wa_error)`.
- `notify.js` polls `/notifications/poll`. It lists unread alerts in the Alerts dropdown, pops up a toast for each new one once, raises a desktop notification if allowed, and sets the installed app's icon badge.
- `/notifications/<id>/open` marks the alert read and redirects to its link.
- **Push:** `/push/subscribe|unsubscribe|test`. `sw.js` shows push notifications while the app is closed. Needs `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` and `VAPID_SUBJECT`.
- **WhatsApp** copies of alerts go out through the Meta Cloud API (`WHATSAPP_TOKEN/PHONE_ID/TEMPLATE`) or CallMeBot (`WHATSAPP_CALLMEBOT_KEY`).
- Per-business switches for each alert kind: low stock, permission requests, pre-orders, collections, deliveries, customer requests, payroll.
- **Reminders cron** (`/cron/reminders`, daily) covers who owes money, what's ready to sell, what isn't on the POS, what has been ready too long, and pre-orders due.

### 4.2 Offline and PWA
- `sw.js` fetches pages network-first and keeps a copy of each page opened while signed in. When offline, or slower than `SLOW_MS`, it shows the copy with a "this is a copy from…" banner and disables saving. A page never opened on the device falls back to `offline.html`. Versioned CSS and JS are served cache-only. Bump `CACHE_NAME` only when caching behaviour itself changes. The cache is cleared when a different user signs in.
- `offline-store.js` keeps an IndexedDB snapshot (POS products, stock, customers, 30 days of invoices, plantings, pre-orders), refreshed every few minutes from `/offline/snapshot`.
- `offline-app.js` / `offline.html` provide an offline till and planting log. Sales get temporary numbers (`OFF-…`) and an idempotency key. They upload to `/offline/sales` and `/offline/plantings`, which record each key once. A rejected upload waits under "Needs attention" instead of being lost.

### 4.3 Backups
- **Local folder** (`backup.js`): Excel books, a master workbook and every table as CSV, written into a folder the admin picks once (File System Access API, Chrome/Edge). File names never change, so each backup replaces the last. Other browsers get a ZIP (`/backup/all.zip`). There's an end-of-day reminder at `businesses.backup_hour`.
- **Google Drive** (`gdrive.py`): each business connects its own Drive with OAuth (`/backup/drive/connect|callback`). "Back up now" uploads file by file, and `/cron/drive-backups` runs nightly.

### 4.4 Assistant (optional)
- A floating button (`.assistant-fab`) on every page opens a chat panel docked beside the page. The user can type, speak, or attach a photo or PDF. Answers can be read aloud in a chosen voice, and a wake phrase starts hands-free mode.
- Tools in `assistant_tools.py` read anything the signed-in user is allowed to open. **Changes are never made directly:** the assistant creates a pending action shown as a card with **Confirm / Cancel** (`/assistant/actions/<id>/confirm`). Messages meant for customers come with Copy and Open-in-WhatsApp buttons; the assistant never sends anything itself.
- Cost controls: simple questions are answered by the app without the model, fewer tools are sent per question, every call is logged (`assistant_calls`), and use is capped per business. The model is set by `ASSISTANT_MODEL`.
- It has memory (`assistant_memory`), stock preferences, an evening brief (`/cron/evening-brief`) and reminders.

### 4.5 Crons (`vercel.json`)
```
/cron/evening-brief   0 16 * * *
/cron/drive-backups   0 21 * * *
/cron/reminders       0 6 * * *
```
Each is protected by `CRON_SECRET`.

---

## 5. Feature catalogue (modules → pages → routes)

Navigation groups as they appear in the sidebar. ★ = admin only.

### Home
- **Overview** `/overview`: KPI row (each compared with the previous period, with a 14-day sparkline), sales a day for the last 30 days against the 30 before (server-drawn SVG), best sellers, *Needs doing* (deliveries, collections, pre-orders, customer requests and pay days due or overdue), latest sales, and what is in the greenhouse. Every card links to the page behind it.

### Sell
- **Sales & POS** `/` (pos): product grid and cart, discounts (amount or %), customer name and phone (matched to customer accounts, with aliases), payment status Paid / Partial / Unpaid, payment method (cash, bank, Airtel Money, MTN Money, each with its own reference label), handover (collected now, or collected / delivered on a later day), and pre-orders. `/checkout` posts the sale; stock checks raise `SaleRefused`. Removing an item from the POS is a stock adjustment with a reason.
- **Sales & Invoices** `/history` (+ `.csv`): filter by period and search, totals, filter for sales waiting to be collected. `/invoices/<id>`: the invoice document, printable on A4 or a receipt printer, with payment updates, mark collected or delivered, cancel (stock goes back), and request deletion (operator) or delete (admin, logged in Deletion Records). `/sales/item`: who bought a product.
- **Deliveries** `/deliveries`, `/deliveries/list(.pdf)`: pick invoices to build a delivery list or route, print it, and mark items delivered.
- **Customer Requests** `/customer-requests`: what a customer asked for that wasn't in stock, with follow-up dates, notes, close and reopen, and a log.
- **Customers** ★ `/customers`: balances owed, statements (PDF), record payments (oldest invoice first), find duplicates, merge, or mark two accounts as distinct.

### Stock
- **Inventory** `/inventory`: category tiles, then department chips, then the item table. Facets show stock and cost. Items have a unit (bag, box, kg, litre…) and **pack contents** (a 50 kg bag, a sachet of 15,000 seeds), so farm use can be given by amount and packs are opened as needed. `/inventory/<id>`: edit, correct the quantity (with a reason), transfer to the POS, delete. Categories can be managed, merged and renamed.
- **Log a purchase** `/inventory/new`, `/procurements/new`: a guided form. Is it an inventory item or an expense? Category and department are picked as tiles. **Purpose** is farm use, for sale, or both; for-sale stock goes straight onto the POS.
- **Scan a receipt** `/procurements/receipt`: a photo or PDF is shrunk on the device, read by Gemini (or typed in), then reviewed line by line. Each line is matched to an item, a new item or an expense, and checked against the printed total.
- **Inventory Analysis** `/inventory/analysis` (+ `.csv`): purchases by category, department, item and period.
- **Planting Records** `/planting`: the greenhouse. A planting has trays × holes per tray = seeds, seed packs are taken from inventory, mortality % gives the seedlings expected, and there's a ready date. **Planter tiles** open one person's day for entering trays by **tray type** (Big, Small…, each paid differently). Also: *Up next* (what is ready soon) with reminders, pre-orders against a planting, and offline entry.
- **Procurements** `/procurements` (+ `.csv`): purchase list with payment status and payments.
- **Suppliers** ★ `/suppliers`: supplier statements, record payments with a live oldest-first allocation preview, edit or delete payments, combine statements, find duplicates and merge.
- **Import** ★ `/import`: CSV templates and imports for purchases, sales, payroll and planting. Names in the file that don't match an employee are confirmed in a dialog first (`import_names.js`).

### Insights
- **Reminders** ★ `/reminders`: the reminder report (also told by the assistant).
- **Accounts** ★ `/accounts` (+ `.csv`, `standing.xlsx`, `standing.pdf`): a cashbook with balances for cash at hand, bank, Airtel Money and MTN Money, kept up to date by every payment. Record money in or out, assign entries, recount balances.
- **Reports** `/reports`, `/reports/department`: a period picker (presets or a custom range), sales and costs by department and item, charts.
- **Deletion Records** ★ `/records/deleted-invoices`.
- **Stock Adjustments** ★ `/stock-adjustments`: write-offs and quantity corrections with before, after, reason and note.

### People
- **Daily Activities** `/activities`: plots grouped into departments, workers from the payroll, a log of what each did. Products used in an activity come out of inventory (by kg, litre or whole units). There's a day report by worker (`.xlsx`).
- **HR & Payroll** ★ `/hr`: employees (casual or salaried, a rate per tray, a pay schedule and next pay date, absences), HR settings (non-working days, NAPSA and NHIMA employee and employer %, contribution base, tray types), payslips (`/hr/payslips/new` fills itself in from trays planted or days worked minus absences), employee statements, and an alert on the pay date.
- **Operators & Admins** ★ `/operators`: users and their permission flags.
- **Permission Requests** ★ `/requests`.

### Business
- **Settings** ★ `/settings`: business name, subtitle and contact (used on receipts), currency, low-stock threshold, departments (add, rename, merge, delete), alert switches and WhatsApp number, assistant settings, reset the app or clear inventory (danger zone).
- **Backups** ★ `/backup`.
- **My Account & Appearance** `/appearance` (any user): look, password, delete my login or my business.
- Public pages: `/login`, `/setup` (sign up or start a demo), `/privacy`, `/terms`, linked from sign-in, registration and Backups.

---

## 6. Data model (52 tables, all with `business_id` except `businesses` and `app_meta`)
```
Tenancy      businesses, users, app_meta
Catalogue    categories, departments, products (inventory), pos_products (what is on sale), stock_preferences
Sales        invoices, invoice_items, deleted_invoices, preorders, customers, customer_aliases, customer_distinct,
             customer_requests, customer_request_log
Buying       procurements, suppliers, supplier_aliases, supplier_distinct, supplier_payments,
             supplier_payment_allocations, receipt_scans
Money        payment_log, cashbook, money_balances
Stock        stock_adjustments, seedling_batches
Greenhouse   plantings, planting_trays, tray_types
Field work   plots, plot_workers, activities
HR           employees, hr_settings, payroll, employee_absences
Alerts       notifications, permission_requests, push_subscriptions
Offline      offline_uploads
Backups      drive_links, drive_files
Assistant    assistant_messages, assistant_usage, assistant_actions, assistant_calls, assistant_memory
```
Patterns worth keeping:
- **Payments** are stored as `amount_paid` on the document, plus a `payment_log` history row, plus a `cashbook` entry for the money account. `payment_status_for(total, paid)` derives Paid, Partial Payment or Unpaid.
- **Aliases and distinct tables** support duplicate detection and merging (`difflib` name similarity plus phone matching). Merged names keep resolving to the surviving account.
- **Invoice numbers** come from `next_invoice_no(db, biz)` and are sequential per business.

---

## 7. Environment variables
```
DATABASE_URL / POSTGRES_URL   Postgres (required on Vercel)
SECRET_KEY                    Flask sessions (required on Vercel)
APP_TIMEZONE                  default Africa/Lusaka
ALLOW_SIGNUP                  "0" closes public sign-up
APP_OPERATOR, APP_CONTACT_EMAIL   shown in the privacy policy and terms
CRON_SECRET                   protects /cron/*
VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT   push notifications
WHATSAPP_PROVIDER, WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, WHATSAPP_TEMPLATE(_LANG), WHATSAPP_API_VERSION, WHATSAPP_CALLMEBOT_KEY
ASSISTANT_MODEL, ASSISTANT_LISTEN_MODEL, ASSISTANT_SPEAK_MODEL, ASSISTANT_THINKING, (Gemini API key)
Google Drive OAuth client id/secret (see gdrive.py)
```

## 8. Tests
`tests/` uses `unittest` with a temporary SQLite database and the Flask test client. There's one file per feature (`test_tray_types.py`, `test_offline_planting.py`, …) plus the business-separation test. `live_gemini_check.py` is a manual check that calls Gemini for real.

---

## 9. Replicating this format in another app: checklist

Do these in order. Each step leaves the other app working.

1. **Tokens and CSS.** Copy `styles.css` and `mobile.css` into the other app's static folder, and link them like `base.html` does: `mobile.css` with `media="(max-width: 760px)"`, both with `?v=<deploy id>`.
2. **`<html>` attributes.** Render `data-theme / data-accent / data-size / data-font / data-spacing / style="--glass"` from the user's saved preferences, with defaults `dark / "" / normal / inter / normal / 0.5`. Load only the chosen Google Font, non-blocking.
3. **Shell.** Port `base.html`: the sidebar (≥1024px, foldable), the top bar with `.here`, Alerts and the user menu, the tablet Menu dropdown, the phone tab bar plus More bottom sheet, flashes in `<main>`, and the menu script. Turn the other app's pages into the same `(label, endpoint, active_endpoints, admin_only, svg_path)` list, grouped by task.
4. **Pages.** Re-skin each page with the anatomy in 3.6: `page-head` → `toolbar` → `stats` → `card`s holding `table-wrap` tables or `.rows`. One `h1` per page, an `h2` label on every card, `.chip` for statuses, `.empty` on every list, `.num` on number columns.
5. **Clickable cards.** Any card or tile that is a link becomes an `<a class="card">` / `a.stat` / `a.kpi` to get the chevron, lift and sweep.
6. **Dashboard.** Build an Overview with `.dash-head`, `.kpis` (with `.delta` and a sparkline), and a `.dash-grid` of `span-*` cards: a trend chart, a ranking, *Needs doing*, and the latest records.
7. **Behaviour.** Add `nav.js`, `dialog.js` and `print.js`. Use `data-confirm` for destructive forms and `<dialog class="modal">` with `data-modal-open` for small edits. Keep the server on Post/Redirect/Get with flash categories `ok`, `error` and `notice`, so `nav.js` works unchanged. Add the FollowSaves middleware if the server is Python/WSGI.
8. **Appearance page.** Port `_appearance.html` and the theme, font and accent constants. Store the choices on the user, save in the background, and mirror them into a `look` cookie for the login page.
9. **PWA.** Add the manifest route, icons, `sw.js` and the splash screen, rebranded.
10. **Optional systems.** Alerts (`notify.js` + a notifications table), push, offline, backups and the assistant, in that order of value.

### If the other app isn't Flask/Jinja
- The CSS is framework-independent: keep the class names and render the same markup from React, Vue, Django or anything else.
- For React or Next.js: put the `<html>` attributes on the root layout, turn `base.html` into a `<Shell>` component, and use the router instead of `nav.js`. Keep `dialog.js` behaviour by using `<dialog>` with the same classes.
- For Tailwind: keep the `:root` and theme blocks from `styles.css` and map Tailwind's colours to the variables (`primary: 'var(--primary)'`, `surface: 'var(--surface)'`, and so on), so themes keep working.

### Prompt to give the other Claude session
> Read `APP_SPEC.md` (copied from my Chimoko app) and the files it lists in the "Copy verbatim" table. Re-skin this app to
> match that design system and app shell exactly, following section 9 step by step. Don't change any business logic or data.
> Work one step at a time, keep the app running after each step, and show me screenshots at desktop (1440px), tablet (900px)
> and phone (390px) widths after steps 3, 4 and 6.
