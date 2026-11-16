import base64
import calendar
import collections
import csv
import datetime
import difflib
import functools
import hmac
import io
import json
import math
import numbers
import os
import random
import re
import secrets
import time
import urllib.parse
import zipfile
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, flash, g, has_request_context, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
import xlsxwriter

from db import DB_ERRORS, IS_POSTGRES, Database, delete_business, init_schema, mark_startup_done, startup_done, startup_signature
import assistant
import gdrive

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ON_VERCEL = bool(os.environ.get("VERCEL"))
TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Africa/Lusaka"))
PAYMENT_STATUSES = ("Paid", "Partial Payment", "Unpaid")
PERMISSIONS = ("sell", "procure", "transfer", "delete")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# Statutory rules: each contribution has an employee rate (deducted from pay) and an employer rate (paid on top)
STATUTORY_RATES = ("napsa_employee_pct", "napsa_employer_pct", "nhima_employee_pct", "nhima_employer_pct")
HR_DEFAULTS = {
    "non_working_days": "saturday,sunday", "napsa_employee_pct": 5.0, "napsa_employer_pct": 5.0,
    "nhima_employee_pct": 1.0, "nhima_employer_pct": 1.0, "contribution_base": "basic",
    "holes_per_tray": 0, "tray_rate": 0.0,   # a planting's usual holes per tray, and what a casual worker is paid for each tray planted
}
MAX_TRAY_HOLES = 10_000    # holes in one seedling tray
MAX_TRAYS = 100_000        # trays in one planting

if ON_VERCEL and not IS_POSTGRES:
    raise RuntimeError("DATABASE_URL is not set. Connect a Postgres database to this Vercel project.")

secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    if ON_VERCEL:
        raise RuntimeError("SECRET_KEY is not set. Add it under Settings > Environment Variables in Vercel.")
    secret_key = "local-development-only"

# Vercel serves public/** from its CDN; locally Flask serves the same folder at the same URL
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "public", "static"), static_url_path="/static")
app.config.update(
    SECRET_KEY=secret_key,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=ON_VERCEL,
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
)

PUBLIC_DIR = os.path.join(BASE_DIR, "public")


# --- SAVES COME BACK AS THE NEXT PAGE ---
# Every save is a form POST answered with a redirect to the page to show next, so a refresh never sends it twice. Over a slow
# or far-away connection that costs a second full round trip: the browser has to come back and ask for that page. When the
# app's own in-place navigation (nav.js) sends a save, it says so (X-Nav: follow), and the redirect is followed here instead:
# the next page is produced by an ordinary request of its own, with the cookies the save just set (so its message shows once),
# and sent back in the same response, with X-Nav-Url saying which page it is. Anything else gets the plain redirect.
class FollowSaves:
    REDIRECTS = ("301", "302", "303")

    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        if environ.get("REQUEST_METHOD") != "POST" or environ.get("HTTP_X_NAV") != "follow":
            return self.wsgi(environ, start_response)
        status, headers, body = self.run(environ)
        location = next((value for name, value in headers if name.lower() == "location"), "")
        target = urllib.parse.urlsplit(location)
        same_site = not target.netloc or target.netloc == environ.get("HTTP_HOST")
        if status[:3] not in self.REDIRECTS or not location or not same_site or not target.path.startswith("/"):
            start_response(status, headers)
            return body
        saved_cookies = [value for name, value in headers if name.lower() == "set-cookie"]
        page = dict(environ)
        page.update({
            "REQUEST_METHOD": "GET", "PATH_INFO": urllib.parse.unquote(target.path), "QUERY_STRING": target.query,
            "CONTENT_LENGTH": "0", "CONTENT_TYPE": "", "wsgi.input": io.BytesIO(b""),
            "HTTP_COOKIE": self.cookies_after(environ.get("HTTP_COOKIE", ""), saved_cookies),
        })
        for key in ("HTTP_X_NAV", "werkzeug.request"):
            page.pop(key, None)
        status, headers, body = self.run(page)
        # The save's cookies first, then the page's: where both set one (the session, with its message read), the page's wins
        headers = [(n, v) for n, v in headers if n.lower() != "set-cookie"] + [("Set-Cookie", v) for v in saved_cookies] \
            + [(n, v) for n, v in headers if n.lower() == "set-cookie"] + [("X-Nav-Url", target.path + ("?" + target.query if target.query else ""))]
        start_response(status, headers)
        return body

    def run(self, environ):
        captured, chunks = {}, []

        def start(status, headers, exc_info=None):
            captured["status"], captured["headers"] = status, list(headers)
            return chunks.append

        result = self.wsgi(environ, start)
        try:
            chunks.extend(result)
        finally:
            if hasattr(result, "close"):
                result.close()
        return captured["status"], captured["headers"], chunks

    @staticmethod
    def cookies_after(sent, set_cookies):
        """The Cookie header the browser would send once it has taken in these Set-Cookie headers."""
        jar = {}
        for part in sent.split(";"):
            name, _, value = part.strip().partition("=")
            if name:
                jar[name] = value
        for header in set_cookies:
            first, *attributes = header.split(";")
            name, _, value = first.strip().partition("=")
            removed = any(a.strip().lower() == "max-age=0" for a in attributes) or any(
                a.strip().lower().startswith("expires=") and "1970" in a for a in attributes
            )
            if removed:
                jar.pop(name, None)
            elif name:
                jar[name] = value
        return "; ".join(f"{name}={value}" for name, value in jar.items())


app.wsgi_app = FollowSaves(app.wsgi_app)


# --- INSTALLABLE APP (PWA) ---
# sw.js lives at the project root (public/), not under public/static/, so it is served at "/sw.js": a service worker's
# scope is the folder it is served from, and it must cover the whole app. On Vercel that route is never actually reached
# (Vercel serves public/** straight from its CDN); it exists so the same URL works in local development.
#
# The manifest is made here instead of being a file, so an installed app is named after the business signed in when it was
# installed, and MS-POS before anyone signs in. The page links it with crossorigin="use-credentials", without which
# browsers fetch it signed out. Its id stays "/", so it is the same app whichever name it was installed under.
APP_NAME = "MS-POS"
APP_BACKGROUND = "#12372a"


@app.get("/manifest.json")
def pwa_manifest():
    user = g.get("user")
    name = user["business_name"] if user else APP_NAME
    # Shown under the icon on a phone's home screen, where only about 12 characters fit: as many whole words as fit
    short = ""
    for word in name.split():
        if len(f"{short} {word}".strip()) > 12:
            break
        short = f"{short} {word}".strip()
    icons = [
        {"src": asset(f"icons/{file}.png"), "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
        for file, size, purpose in (("icon-192", 192, "any"), ("icon-512", 512, "any"),
                                    ("icon-maskable-192", 192, "maskable"), ("icon-maskable-512", 512, "maskable"))
    ]
    manifest = {
        "name": name,
        "short_name": short or name[:12],
        "description": f"{APP_NAME}: inventory, point of sale, procurement, HR and greenhouse management" + (f" for {name}." if user else "."),
        "id": "/",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "display_override": ["window-controls-overlay", "standalone"],
        "orientation": "any",
        "background_color": APP_BACKGROUND,
        "theme_color": "#2563eb",
        "icons": icons,
    }
    return Response(json.dumps(manifest), mimetype="application/manifest+json", headers={"Cache-Control": "no-store", "Vary": "Cookie"})


@app.get("/sw.js")
def pwa_service_worker():
    return send_from_directory(PUBLIC_DIR, "sw.js", mimetype="application/javascript")


class StockError(Exception):
    pass


class PreorderGone(Exception):
    pass


# --- REQUEST PLUMBING ---
def get_db():
    if "db" not in g:
        g.db = Database()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_field():
    return Markup('<input type="hidden" name="csrf" value="{}">').format(csrf_token())


@app.before_request
def load_user_and_check_csrf():
    g.user = None
    g.started = time.perf_counter()
    if request.endpoint == "static":
        return

    if request.method == "POST":
        expected = session.get("csrf", "")
        supplied = request.form.get("csrf", "")
        if not expected or not hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
            abort(400, "This form has expired. Go back, refresh the page and try again.")

    sweep_expired_demos()
    uid = session.get("uid")
    if uid:
        g.user = user_row(get_db(), uid)
        if g.user is None:
            session.clear()
        elif g.user["demo_expires_at"] and g.user["demo_expires_at"] <= now().strftime("%Y-%m-%d %H:%M:%S"):
            purge_expired_demos(get_db())
            session.clear()
            g.user = None
            flash("Your 24-hour demo has ended. The demo company, its sample data and its login have been deleted.", "notice")


def user_row(db, uid):
    """A login with its business's settings: what g.user holds while they are signed in."""
    return db.one(
        """SELECT u.id, u.business_id, u.username, u.role, u.can_sell, u.can_procure, u.can_transfer, u.can_delete, u.theme, u.glass, u.accent, u.text_size, u.font, u.text_spacing,
                  u.evening_brief AS my_brief, u.brief_day, u.assistant_voice,
                  b.name AS business_name, b.subtitle, b.contact, b.currency, b.low_stock_threshold,
                  b.alert_phone, b.alert_low_stock, b.alert_permission, b.alert_preorders, b.alert_collections, b.alert_deliveries, b.alert_requests, b.alert_payroll, b.preorder_remind_days,
                  b.backup_hour, b.last_backup_at, b.last_backup_by, b.demo_expires_at, b.assistant_enabled,
                  b.wake_phrase, b.stop_phrase, b.stock_cover_days, b.evening_brief, b.brief_whatsapp,
                  b.reminder_every, b.reminder_stale_days, b.reminder_sent_on, u.reminders_seen,
                  (SELECT COUNT(*) FROM drive_links d WHERE d.business_id = b.id AND d.refresh_token IS NOT NULL) AS drive_on
           FROM users u JOIN businesses b ON b.id = u.business_id WHERE u.id=?""",
        (uid,),
    )


def can(perm):
    user = g.get("user")
    if not user:
        return False
    if user["role"] == "admin":
        return True
    return perm in PERMISSIONS and bool(user["can_" + perm])


def need(perm):
    """Requires a signed-in user with the given permission ('admin' means admins only, None means any user)."""
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            if perm is not None and not can(perm):
                flash("Your account does not have permission to do that. Ask an admin.", "error")
                return redirect(url_for("pos"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.context_processor
def template_globals():
    return {
        "csrf_field": csrf_field, "user": g.get("user"), "can": can, "look": appearance_of(g.get("user")), "themes": THEMES,
        "theme_swatches": THEME_SWATCHES, "accents": ACCENTS, "accent_colours": ACCENT_COLOURS, "text_sizes": TEXT_SIZES,
        "fonts": FONTS, "font_files": FONT_FILES, "text_spacings": TEXT_SPACINGS, "dark_themes": DARK_THEMES,
        "asset": asset, "csrf_token": csrf_token, "inv_link": inv_link, "assistant_on": assistant.enabled_for(g.get("user")),
        "assistant_phrases": assistant.phrases_of(g.user) if g.get("user") else (assistant.DEFAULT_WAKE, assistant.DEFAULT_STOP),
        "brief_waiting": bool(g.get("user")) and assistant.brief_due(g.user),
        "reminders_waiting": bool(g.get("user")) and assistant.reminders_due(g.user),
        "push_key": os.environ.get("VAPID_PUBLIC_KEY", "").strip() if g.get("user") else "",
    }


# The colour sets a person can choose (styles.css has one block for each), with the two colours their swatch shows
THEMES = (("dark", "Midnight"), ("light", "Daylight"), ("system", "Match my device"), ("forest", "Forest"), ("ocean", "Ocean"),
          ("sunset", "Sunset"), ("graphite", "Graphite"), ("sand", "Sand"), ("mint", "Mint"), ("contrast", "High contrast"),
          ("slate", "Slate"), ("plum", "Plum"), ("coffee", "Coffee"), ("nord", "Nordic"), ("emerald", "Emerald"), ("crimson", "Crimson"),
          ("paper", "Paper"), ("sky", "Sky"), ("blush", "Blush"), ("lavender", "Lavender"), ("stone", "Stone"))
THEME_SWATCHES = {"dark": ("#0c0d13", "#3b82f6"), "light": ("#f2f4f9", "#2563eb"), "system": ("#0c0d13", "#f2f4f9"), "forest": ("#0b130e", "#22a35a"),
                  "ocean": ("#08121a", "#14a3b8"), "sunset": ("#160d0b", "#f0703a"), "graphite": ("#111214", "#a1a1aa"), "sand": ("#f6f1e7", "#b45309"),
                  "mint": ("#eef7f2", "#0f9d6a"), "contrast": ("#000000", "#ffd400"), "slate": ("#0f172a", "#3b82f6"), "plum": ("#130d1a", "#8b5cf6"),
                  "coffee": ("#14100c", "#b7791f"), "nord": ("#2e3440", "#88c0d0"), "emerald": ("#06140f", "#0d9488"), "crimson": ("#150b0e", "#e11d48"),
                  "paper": ("#fafaf7", "#18181b"), "sky": ("#eef6fd", "#0284c7"), "blush": ("#fdf2f4", "#e11d48"), "lavender": ("#f5f2fd", "#7c3aed"),
                  "stone": ("#f3f2f0", "#0d9488")}
# For the picker, which lists the dark ones and the light ones apart
DARK_THEMES = {"dark", "forest", "ocean", "sunset", "graphite", "contrast", "slate", "plum", "coffee", "nord", "emerald", "crimson"}
# The typefaces a person can choose (styles.css has a line for each) and what to ask Google Fonts for; "system" is the device's own
FONTS = (("inter", "Inter"), ("jakarta", "Plus Jakarta Sans"), ("manrope", "Manrope"), ("dm", "DM Sans"), ("outfit", "Outfit"), ("lexend", "Lexend"),
         ("atkinson", "Atkinson Hyperlegible"), ("plex", "IBM Plex Sans"), ("system", "This device's own"))
FONT_FILES = {"inter": "Inter:wght@400;500;600;700", "jakarta": "Plus+Jakarta+Sans:wght@400;500;600;700", "manrope": "Manrope:wght@400;500;600;700",
              "dm": "DM+Sans:wght@400;500;600;700", "outfit": "Outfit:wght@400;500;600;700", "lexend": "Lexend:wght@400;500;600;700",
              "atkinson": "Atkinson+Hyperlegible:wght@400;700", "plex": "IBM+Plex+Sans:wght@400;500;600;700"}
TEXT_SPACINGS = (("compact", "Compact"), ("normal", "Normal"), ("relaxed", "Relaxed"))
# An accent colour for buttons, links and highlights, over any theme ("" = the theme's own)
ACCENTS = (("", "The theme's own"), ("blue", "Blue"), ("green", "Green"), ("teal", "Teal"), ("purple", "Purple"), ("orange", "Orange"),
           ("rose", "Rose"), ("gold", "Gold"))
ACCENT_COLOURS = {"blue": "#3b82f6", "green": "#16a34a", "teal": "#0d9488", "purple": "#8b5cf6", "orange": "#f97316", "rose": "#e11d48", "gold": "#ca8a04"}
TEXT_SIZES = (("normal", "Normal"), ("large", "Large"), ("larger", "Larger"))
DEFAULT_GLASS = 50
# Changes with every deployment, so browsers never keep using an older stylesheet or script
ASSET_VERSION = os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:10] or datetime.datetime.now().strftime("%Y%m%d%H%M%S")


def asset(filename):
    return url_for("static", filename=filename, v=ASSET_VERSION)


def appearance_of(user):
    """The theme, transparency (0-100), accent colour, text size, typeface and text spacing to draw the page with.

    A signed-in user's saved choice wins. Signed-out pages (login) reuse the look last used on this
    device, kept in a cookie. Anything unset or invalid falls back to the defaults.
    """
    theme = glass = accent = size = font = spacing = None
    if user:
        theme, glass, accent, size = user["theme"], user["glass"], user.get("accent"), user.get("text_size")
        font, spacing = user.get("font"), user.get("text_spacing")
    else:
        remembered = request.cookies.get("look", "").split(":")
        if len(remembered) >= 2 and remembered[1].isdigit():
            theme, glass = remembered[0], int(remembered[1])
            accent, size, font, spacing = (remembered[2:6] + ["", "", "", ""])[:4]
    return {
        "theme": theme if theme in dict(THEMES) else "dark",
        "glass": glass if isinstance(glass, int) and 0 <= glass <= 100 else DEFAULT_GLASS,
        "accent": accent if accent in ACCENT_COLOURS else "",
        "size": size if size in dict(TEXT_SIZES) else "normal",
        "font": font if font in dict(FONTS) else "inter",
        "spacing": spacing if spacing in dict(TEXT_SPACINGS) else "normal",
    }


@app.after_request
def server_timing(response):
    """How long the server took over a page, for the browser's developer tools (Network > Timing) when a page feels slow."""
    if g.get("started") is not None:
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - g.started) * 1000:.0f}"
    return response


@app.after_request
def remember_look(response):
    user = g.get("user")
    if user:
        look = appearance_of(user)
        value = f"{look['theme']}:{look['glass']}:{look['accent']}:{look['size']}:{look['font']}:{look['spacing']}"
        if request.cookies.get("look") != value:
            response.set_cookie("look", value, max_age=365 * 24 * 3600, samesite="Lax", secure=ON_VERCEL, httponly=True)
    return response


@app.route("/appearance", methods=["GET", "POST"])
@need(None)
def appearance():
    """Every user chooses their own look; it follows them to whichever device they sign in on."""
    if request.method == "POST":
        # The page saves in the background the moment a choice is made; the plain form post is the fallback
        background = request.headers.get("X-Requested-With") == "fetch"
        theme = text("theme")
        try:
            glass = number("glass", int)
        except ValueError:
            glass = -1
        # Accent and text size: kept as they were when a form does not send them
        accent = text("accent") if "accent" in request.form else (g.user.get("accent") or "")
        size = text("text_size") if "text_size" in request.form else (g.user.get("text_size") or "normal")
        font = text("font") if "font" in request.form else (g.user.get("font") or "inter")
        spacing = text("text_spacing") if "text_spacing" in request.form else (g.user.get("text_spacing") or "normal")
        valid = (theme in dict(THEMES) and 0 <= glass <= 100 and (accent == "" or accent in ACCENT_COLOURS) and size in dict(TEXT_SIZES)
                 and font in dict(FONTS) and spacing in dict(TEXT_SPACINGS))
        if valid:
            db = get_db()
            db.execute("UPDATE users SET theme=?, glass=?, accent=?, text_size=?, font=?, text_spacing=? WHERE id=?",
                       (theme, glass, accent or None, size, font, spacing, g.user["id"]))
            db.commit()
            g.user["theme"], g.user["glass"], g.user["accent"], g.user["text_size"] = theme, glass, accent or None, size
            g.user["font"], g.user["text_spacing"] = font, spacing
        if background:
            return ("", 204) if valid else ("Choose a theme and a transparency between 0 and 100.", 400)
        if valid:
            flash("Appearance saved.", "ok")
        else:
            flash("Choose a theme and a transparency between 0 and 100.", "error")
        back_to_settings = request.form.get("back") == "settings" and g.user["role"] == "admin"
        return redirect(url_for("settings" if back_to_settings else "appearance"))
    return render_template("appearance.html", last_admin=is_last_admin(get_db()), delete_phrase=DELETE_BUSINESS_PHRASE)


# --- DELETING AN ACCOUNT ---
# Anyone can delete their own login, except the last admin of a business (it would be left with nobody to run it).
# An admin can instead delete the whole business: every record and every login in it, for good.
DELETE_BUSINESS_PHRASE = "DELETE BUSINESS"


def is_last_admin(db):
    if g.user["role"] != "admin":
        return False
    return db.one("SELECT COUNT(*) AS n FROM users WHERE business_id=? AND role='admin'", (g.user["business_id"],))["n"] <= 1


def password_confirmed(db):
    me = db.one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
    return bool(me) and check_password_hash(me["password_hash"], request.form.get("password", ""))


@app.post("/account/delete")
@need(None)
def delete_my_login():
    db = get_db()
    if is_last_admin(db):
        flash("You are the only admin, so your login cannot be deleted on its own. Make another user an admin first, or delete the whole business.", "error")
        return redirect(url_for("appearance"))
    if not password_confirmed(db):
        flash("That password is not correct. Nothing was deleted.", "error")
        return redirect(url_for("appearance"))
    try:
        notify(db, "account", "A login was deleted", f"{g.user['username']} deleted their own login.", url_for("operators"))
        db.execute("DELETE FROM users WHERE id=? AND business_id=?", (g.user["id"], g.user["business_id"]))
        assistant.forget(db, g.user["business_id"], g.user["id"])
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("Your login could not be deleted. Please try again.", "error")
        return redirect(url_for("appearance"))
    session.clear()
    flash("Your login has been deleted.", "ok")
    return redirect(url_for("login"))


@app.post("/account/delete-business")
@need("admin")
def delete_my_business():
    db = get_db()
    if request.form.get("phrase", "").strip() != DELETE_BUSINESS_PHRASE:
        flash(f"Type {DELETE_BUSINESS_PHRASE} exactly to confirm. Nothing was deleted.", "error")
        return redirect(url_for("appearance"))
    if not password_confirmed(db):
        flash("That password is not correct. Nothing was deleted.", "error")
        return redirect(url_for("appearance"))
    name = g.user["business_name"]
    try:
        delete_business(db, g.user["business_id"])
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The business could not be deleted, so nothing was removed. Please try again.", "error")
        return redirect(url_for("appearance"))
    session.clear()
    flash(f"'{name}' has been deleted, with all its records and logins.", "ok")
    return redirect(url_for("login"))


@app.post("/account/password")
@need(None)
def change_password():
    """Lets a signed-in user (admin or operator) change their own password, proving it is them with their current one."""
    db = get_db()
    back = redirect(url_for("appearance"))
    me = db.one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
    current, new, confirm = request.form.get("current_password", ""), request.form.get("new_password", ""), request.form.get("confirm_password", "")
    if not me or not check_password_hash(me["password_hash"], current):
        flash("Your current password is not correct. Nothing was changed.", "error")
    elif len(new) < 8:
        flash("Choose a new password of at least 8 characters.", "error")
    elif new != confirm:
        flash("The two new passwords do not match.", "error")
    elif check_password_hash(me["password_hash"], new):
        flash("That is already your current password. Choose a different one.", "error")
    else:
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), g.user["id"]))
        db.commit()
        flash("Your password has been changed.", "ok")
    return back


@app.template_filter("money4")
def money_precise(value):
    """Per-seed costs are tiny (ZMW 0.0625), so they are shown to four decimals instead of two."""
    user = g.get("user")
    return f"{user['currency'] if user else ''}{float(value or 0):,.4f}"


@app.template_filter("money")
def money(value):
    user = g.get("user")
    currency = user["currency"] if user else ""
    amount = float(value or 0)
    return f"{currency}{0.0 if abs(amount) < 0.005 else amount:,.2f}"   # never "-0.00" from rounding


def tray_figure(value):
    """A number of trays as people say it: 12, 12.5 (never 12.0 or 12.50)."""
    value = round(float(value or 0), 2)
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}".rstrip("0")


app.add_template_filter(tray_figure, "trays")


@app.template_filter("signed")
def signed_money(value):
    """An amount's effect on the money held: + for money in, a minus sign for money out."""
    amount = float(value or 0)
    if abs(amount) < 0.005:
        return money(0)
    return ("+ " if amount > 0 else "− ") + money(abs(amount))


def text(key):
    return request.form.get(key, "").strip()


def checked(key, default=True):
    """Reads a checkbox sent as a hidden '0' followed by the box itself sending '1' when ticked, so the box's own
    value always wins over the hidden one, and a field missing entirely (an older client, or a GET) keeps default.
    """
    ticks = request.form.getlist(key)
    return ticks[-1] != "0" if ticks else default


def number(key, kind=float, default=None):
    """Reads a numeric form field. Raises ValueError when it is missing (and no default is given) or not a number."""
    raw = text(key).replace(",", "")
    if not raw:
        if default is None:
            raise ValueError(key)
        return default
    value = kind(raw)
    if not math.isfinite(value):
        raise ValueError(key)
    return value


def now():
    return datetime.datetime.now(TZ)


def payment_status_for(total, paid):
    if paid >= total - 0.005:
        return "Paid"
    return "Partial Payment" if paid > 0.005 else "Unpaid"


def amount_for_status(status_text, amount_text, total):
    """Works out the amount paid from a Paid / Partial Payment / Unpaid choice. Raises ValueError with the reason."""
    status = status_text.strip().lower()
    if status in ("", "paid"):
        return total
    if status == "unpaid":
        return 0.0
    if not status.startswith("partial"):
        raise ValueError(f"payment status '{status_text}' must be Paid, Partial Payment or Unpaid")
    try:
        amount = round(parse_amount(amount_text), 2)
    except ValueError:
        raise ValueError("a partial payment needs the amount paid") from None
    if not 0 < amount < total:
        raise ValueError("a partial payment must be more than zero and less than the total")
    return amount


def update_payment(db, kind, ref_id, total, old_paid):
    """Reads the admin payment form, logs the change and returns the new amount paid. Raises ValueError on bad input."""
    if request.form.get("mark_paid"):
        new_paid = total
    else:
        new_paid = round(number("amount_paid"), 2)
        if not 0 <= new_paid <= total + 0.005:
            raise ValueError("amount_paid")
        new_paid = min(new_paid, total)
    db.execute(
        "INSERT INTO payment_log (business_id, kind, ref_id, old_paid, new_paid, note, changed_by, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (g.user["business_id"], kind, ref_id, old_paid, new_paid, text("note")[:200], g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return new_paid


def payment_history(db, kind, ref_id):
    return db.all(
        "SELECT old_paid, new_paid, note, changed_by, changed_at FROM payment_log WHERE business_id=? AND kind=? AND ref_id=? ORDER BY id DESC",
        (g.user["business_id"], kind, ref_id),
    )


WALK_IN = "Walk-in Customer"


def name_key(name):
    key = " ".join(name.lower().split())
    return "" if key == WALK_IN.lower() else key


def phone_key(phone):
    """Digits only, with Zambia's +260 prefix folded to a leading 0 so both spellings find the same account."""
    digits = re.sub(r"\D", "", phone or "")
    return "0" + digits[3:] if digits.startswith("260") and len(digits) == 12 else digits


def customer_for(db, biz, name, phone, when):
    """Finds or opens the customer account for a sale. Returns None for an anonymous walk-in (no name and no phone)."""
    name, phone = (name or "").strip(), (phone or "").strip()
    nkey, pkey = name_key(name), phone_key(phone)
    if not nkey and not pkey:
        return None

    exact = db.one("SELECT id FROM customers WHERE business_id=? AND name_key=? AND phone_key=?", (biz, nkey, pkey))
    if exact:
        return exact["id"]
    merged = db.one(
        """SELECT c.id FROM customer_aliases a JOIN customers c ON c.id = a.customer_id
           WHERE a.business_id=? AND a.name_key=? AND a.phone_key=?""",
        (biz, nkey, pkey),
    )
    if merged:
        return merged["id"]
    # Only one of the two details was given: use the account it points to when there is exactly one
    if not pkey or not nkey:
        column, value = ("name_key", nkey) if not pkey else ("phone_key", pkey)
        matches = db.all(f"SELECT id FROM customers WHERE business_id=? AND {column}=? LIMIT 2", (biz, value))
        if len(matches) == 1:
            return matches[0]["id"]
    return db.insert(
        "INSERT INTO customers (business_id, name, phone, name_key, phone_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (biz, name if nkey else "Unnamed customer", phone, nkey, pkey, when),
    )


@app.template_filter("month")
def month_label(value):
    try:
        return datetime.datetime.strptime(value, "%Y-%m").strftime("%B %Y")
    except (ValueError, TypeError):
        return value


def csv_safe(value):
    # A leading = or @ (or a +/- that is not a number or phone) makes spreadsheet apps run the cell as a formula
    if value is None:
        return ""
    if isinstance(value, str) and value and (
        value[0] in "=@\t\r" or (value[0] in "+-" and not re.fullmatch(r"[+-][\d\s().,-]*", value))
    ):
        return "'" + value
    return value


def csv_response(filename, header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_safe(value) for value in row])
    # utf-8-sig adds the marker Excel needs to read accented characters correctly
    return Response(
        buffer.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def department_names(db, biz):
    return [row["name"] for row in db.all("SELECT name FROM departments WHERE business_id=? ORDER BY name", (biz,))]


# --- CATEGORIES (the tiles inventory is organised by) ---
def norm(value):
    """Capital letters, spacing and stray whitespace do not make two names different."""
    return " ".join((value or "").split()).lower()


def clean_name(value, limit=60):
    return " ".join((value or "").split())[:limit]


def category_rows(db, biz):
    return db.all("SELECT id, name FROM categories WHERE business_id=? ORDER BY name", (biz,))


def resolve_category(db, biz, typed):
    """The managed category a typed name belongs to; blank means General. A name that is new is added to the list.

    The caller commits, so a purchase that fails afterwards leaves no stray category behind.
    """
    label = clean_name(typed) or "General"
    for row in db.all("SELECT name FROM categories WHERE business_id=?", (biz,)):
        if norm(row["name"]) == norm(label):
            return row["name"]
    db.execute("INSERT INTO categories (business_id, name) VALUES (?, ?)", (biz, label))
    return label


def sync_categories():
    """Makes the category list match the items already in stock and gives every item the one canonical spelling.

    Runs at start-up. It is what turns the free-text categories of older data into tiles, and it is safe to
    repeat: once everything is canonical it changes nothing.
    """
    db = Database()
    try:
        for biz in [row["id"] for row in db.all("SELECT id FROM businesses")]:
            known = {norm(row["name"]): row["name"] for row in category_rows(db, biz)}
            if "general" not in known:
                db.execute("INSERT INTO categories (business_id, name) VALUES (?, 'General')", (biz,))
                known["general"] = "General"

            spellings = {}      # normalised name -> {spelling: items using it}
            raw_values = []     # (value stored on items, spelling it stands for)
            for row in db.all("SELECT category, COUNT(*) AS n FROM products WHERE business_id=? GROUP BY category", (biz,)):
                label = clean_name(row["category"]) or "General"
                spellings.setdefault(norm(label), {})[label] = spellings.get(norm(label), {}).get(label, 0) + row["n"]
                raw_values.append((row["category"], label))

            for key, options in spellings.items():
                if key not in known:
                    # The most used spelling wins; a capitalised one wins a tie
                    best = max(options, key=lambda s: (options[s], s[:1].isupper(), s))
                    db.execute("INSERT INTO categories (business_id, name) VALUES (?, ?)", (biz, best))
                    known[key] = best

            for raw, label in raw_values:
                canonical = known[norm(label)]
                if raw == canonical:
                    continue
                if raw is None:
                    db.execute("UPDATE products SET category=? WHERE business_id=? AND category IS NULL", (canonical, biz))
                else:
                    db.execute("UPDATE products SET category=? WHERE business_id=? AND category=?", (canonical, biz, raw))
        db.commit()
    except DB_ERRORS:
        db.rollback()
    finally:
        db.close()



def next_invoice_no(db, biz):
    year = now().strftime("%Y")
    # Longest then highest = the largest sequence, whatever order invoices were entered or imported in
    last = db.one(
        "SELECT invoice_no FROM invoices WHERE business_id=? AND invoice_no LIKE ? ORDER BY LENGTH(invoice_no) DESC, invoice_no DESC LIMIT 1",
        (biz, f"INV-{year}-%"),
    )
    seq = 1
    if last:
        try:
            seq = int(last["invoice_no"].rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            pass
    # Imported invoices can carry any number, so make sure the candidate really is free. A number whose invoice was
    # deleted stays taken, so the deletion record can never be confused with a later invoice.
    def taken(number):
        return db.one(
            """SELECT (SELECT COUNT(*) FROM invoices WHERE business_id=? AND invoice_no=?)
                      + (SELECT COUNT(*) FROM deleted_invoices WHERE business_id=? AND invoice_no=?) AS n""",
            (biz, number, biz, number),
        )["n"]

    while taken(f"INV-{year}-{seq:04d}"):
        seq += 1
    return f"INV-{year}-{seq:04d}"


# --- ACCOUNTS ---
def signup_open(db):
    # Any number of businesses can register, each seeing only its own records. Set ALLOW_SIGNUP=0 to close registration
    # once the first business exists (demo companies do not count, so trying the demo first never locks the owner out).
    if os.environ.get("ALLOW_SIGNUP") != "0":
        return True
    return db.one(
        "SELECT COUNT(*) AS n FROM users u JOIN businesses b ON b.id = u.business_id WHERE b.demo_expires_at IS NULL"
    )["n"] == 0


@app.route("/setup", methods=["GET", "POST"])
def setup():
    db = get_db()
    open_ = signup_open(db)
    # A demo can always be started; a real business only while registration is open
    demo_only = not open_ or request.args.get("demo") == "1"

    if request.method == "POST":
        # The demo-only page has one button, so a browser that does not send which button was pressed still means a demo
        demo = request.form.get("mode", "demo" if demo_only else "real") == "demo"
        if not demo and not open_:
            flash("Registration is closed. Ask your admin for an operator account.", "error")
            return redirect(url_for("login"))
        purge_expired_demos(db)
        business, username, password = text("business_name"), text("username"), request.form.get("password", "")
        if demo and db.one("SELECT COUNT(*) AS n FROM businesses WHERE demo_expires_at IS NOT NULL")["n"] >= DEMO_LIMIT:
            flash("Too many demos are running right now. Please try again later.", "error")
        elif not business or not username:
            flash("Business name and username are required.", "error")
        elif len(password) < 8:
            flash("Choose a password of at least 8 characters.", "error")
        elif password != request.form.get("confirm", ""):
            flash("The two passwords do not match.", "error")
        elif db.one("SELECT id FROM users WHERE username=?", (username,)):
            flash("That username is already taken.", "error")
        else:
            ends = (now() + datetime.timedelta(hours=DEMO_HOURS)).strftime("%Y-%m-%d %H:%M:%S") if demo else None
            try:
                biz = db.insert(
                    "INSERT INTO businesses (name, subtitle, contact, cashbook_ready, demo_expires_at) VALUES (?, ?, ?, 1, ?)",
                    (business, text("subtitle"), text("contact"), ends),
                )
                db.execute("INSERT INTO departments (business_id, name) VALUES (?, 'General')", (biz,))
                db.execute("INSERT INTO categories (business_id, name) VALUES (?, 'General')", (biz,))
                uid = db.insert(
                    """INSERT INTO users (business_id, username, password_hash, role, can_sell, can_procure, can_transfer, can_delete)
                       VALUES (?, ?, ?, 'admin', 1, 1, 1, 1)""",
                    (biz, username, generate_password_hash(password)),
                )
                if demo:
                    # The helpers that record money and stock sign each record with the user making it
                    signed_in, g.user = g.user, {"id": uid, "business_id": biz, "username": username, "role": "admin"}
                    try:
                        fill_demo_data(db, biz)
                    finally:
                        g.user = signed_in
                db.commit()
            except DB_ERRORS:
                db.rollback()
                flash("The account could not be created. Please try again.", "error")
            else:
                if demo:
                    # Batches due within the release window go on the POS, as they would on any page load of the greenhouse
                    release_plantings(db, biz)
                session.clear()
                session["uid"] = uid
                session.permanent = True
                if demo:
                    flash(
                        f"Your demo company is ready, filled with sample data. Explore freely: after {DEMO_HOURS} hours it is deleted, "
                        "together with your login.",
                        "ok",
                    )
                return redirect(url_for("pos"))

    return render_template("setup.html", demo_only=demo_only, signup_open=open_, demo_hours=DEMO_HOURS)


# --- PRIVACY POLICY AND TERMS ---
# Open to everyone (Google's sign-in screen links to them). Who runs the app and how to reach them come from APP_OPERATOR and APP_CONTACT_EMAIL.
LEGAL_EFFECTIVE = "10 November 2026"


def legal_facts():
    return {
        "app": "MS-POS", "effective": LEGAL_EFFECTIVE, "site": request.host_url.rstrip("/").replace("http://", "https://") if ON_VERCEL else request.host_url.rstrip("/"),
        "operator": os.environ.get("APP_OPERATOR", "").strip() or "TechGuy", "contact": os.environ.get("APP_CONTACT_EMAIL", "").strip(),
    }


@app.get("/privacy")
def privacy():
    return render_template("privacy.html", legal=legal_facts())


@app.get("/terms")
def terms():
    return render_template("terms.html", legal=legal_facts())


@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    if g.user:
        return redirect(url_for("pos"))

    if request.method == "POST":
        # An ended demo's login is gone before anyone can sign in with it
        purge_expired_demos(db)
        found = db.one("SELECT id, password_hash FROM users WHERE username=?", (text("username"),))
        if found and check_password_hash(found["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = found["id"]
            session.permanent = True
            return redirect(url_for("pos"))
        flash("Invalid username or password.", "error")
    elif db.one("SELECT COUNT(*) AS n FROM users")["n"] == 0:
        return redirect(url_for("setup"))

    return render_template("login.html", signup_open=signup_open(db))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- DEMO COMPANIES ---
# Anyone can start a demo from the registration page, even when registration is closed. The demo company starts filled
# with sample data and lasts DEMO_HOURS; then the company, every record in it and all its logins are deleted for good.
DEMO_HOURS = 24
# Demo companies that may exist at once, so the database cannot be filled up by people starting demo after demo
DEMO_LIMIT = 50
# How often (at most) a page load also deletes other people's ended demos
DEMO_SWEEP_SECONDS = 300
demo_sweep = {"last": None}


def purge_expired_demos(db):
    """Deletes every demo company whose time is up, with all its records and logins. Returns how many were deleted."""
    ended = db.all(
        "SELECT id FROM businesses WHERE demo_expires_at IS NOT NULL AND demo_expires_at <= ?", (now().strftime("%Y-%m-%d %H:%M:%S"),)
    )
    try:
        for row in ended:
            delete_business(db, row["id"])
        db.commit()
    except DB_ERRORS:
        db.rollback()
        return 0
    return len(ended)


def sweep_expired_demos():
    if demo_sweep["last"] is not None and time.monotonic() - demo_sweep["last"] < DEMO_SWEEP_SECONDS:
        return
    demo_sweep["last"] = time.monotonic()
    purge_expired_demos(get_db())


DEMO_SUPPLIERS = {
    "farm": ("Kafue Agro Supplies", "0971 234 567"),
    "tools": ("Chongwe Hardware & Tools", "0966 345 678"),
    "seed": ("Lusaka Seed Centre", "0977 456 789"),
}
# (code, name, category, department, unit, quantity bought, unit cost, selling price, supplier, share put on the POS)
DEMO_ITEMS = (
    ("FERT-DC50", "D-Compound Fertiliser 50kg", "Fertilisers", "Agro Shop", "bag", 40, 780, 950, "farm", 0.6),
    ("FERT-UREA50", "Urea Fertiliser 50kg", "Fertilisers", "Agro Shop", "bag", 30, 820, 990, "farm", 0.6),
    ("FERT-COMP25", "Organic Compost 25kg", "Fertilisers", "Agro Shop", "bag", 50, 120, 180, "farm", 0.7),
    ("CHEM-CYP1L", "Cypermethrin 1L", "Chemicals", "Agro Shop", "bottle", 24, 145, 210, "farm", 0.5),
    ("CHEM-MANC1", "Mancozeb 1kg", "Chemicals", "Agro Shop", "pack", 30, 95, 150, "farm", 0.5),
    ("CHEM-GLY5L", "Glyphosate 5L", "Chemicals", "Agro Shop", "bottle", 12, 380, 520, "farm", 0.5),
    ("TOOL-SPR16", "Knapsack Sprayer 16L", "Tools", "Agro Shop", "piece", 8, 450, 650, "tools", 0.5),
    ("TOOL-HOE", "Garden Hoe", "Tools", "Agro Shop", "piece", 20, 85, 130, "tools", 0.6),
    ("TOOL-CAN10", "Watering Can 10L", "Tools", "Agro Shop", "piece", 15, 70, 110, "tools", 0.6),
    ("TOOL-TRAY128", "Seedling Tray (128 cells)", "Tools", "Greenhouse", "tray", 60, 25, 45, "tools", 0.5),
    ("SEED-TOM", "Tomato Seeds (Rodade)", "Vegetable Seeds", "Greenhouse", "pack", 10, 180, 250, "seed", 0),
    ("SEED-CAB", "Cabbage Seeds (Star 3301)", "Vegetable Seeds", "Greenhouse", "pack", 8, 220, 300, "seed", 0),
    ("SEED-RAPE", "Rape Seeds (English Giant)", "Vegetable Seeds", "Greenhouse", "pack", 15, 60, 95, "seed", 0),
)
# Purchases left owing, so supplier statements have balances: code -> share of the cost paid when logged
DEMO_OWING = {"FERT-UREA50": 0.5, "CHEM-GLY5L": 0.0}
DEMO_CUSTOMERS = (
    ("Mwila Banda", "0971 112 233"), ("Chileshe Mulenga", "0966 223 344"), ("Natasha Phiri", "0977 334 455"),
    ("Joseph Tembo", "0955 445 566"), ("Kalaba Farms", "0976 556 677"), ("Grace Zulu", "0965 667 788"),
)
# (name, NRC, basic salary, employment type, address, age, days per week, days missed last month)
DEMO_EMPLOYEES = (
    ("Bwalya Mwansa", "112233/10/1", 4500, "Permanent", "Chilenje, Lusaka", 34, 5, 0),
    ("Esther Lungu", "223344/11/1", 3800, "Permanent", "Kabwata, Lusaka", 28, 5, 0),
    ("Peter Sakala", "334455/12/1", 2500, "Casual Worker", "Chawama, Lusaka", 41, 5, 2),
)
# (name, batch, seeds, mortality %, cost of one seed, price, planted days ago, ready in days, marked ready)
DEMO_PLANTINGS = (
    ("Tomato Seedlings", "GH-T1", 1000, 12, 0.18, 2.0, 40, -5, True),
    ("Rape Seedlings", "GH-R1", 2000, 10, 0.06, 1.0, 25, 4, False),
    ("Cabbage Seedlings", "GH-C1", 1000, 15, 0.22, 2.5, 6, 30, False),
)
# (days ago, amount, method, category, party, description)
DEMO_EXPENSES = (
    (30, 3500, "Bank transfer", "rent", "Landlord", "Shop rent"),
    (25, 400, "Cash", "transport", "", "Fuel for deliveries"),
    (20, 850, "Airtel Money", "utilities", "ZESCO", "Electricity tokens"),
    (15, 45, "Airtel Money", "charges", "", "Mobile money charges"),
    (8, 350, "Cash", "transport", "", "Fuel for deliveries"),
)


def fill_demo_data(db, biz):
    """Fills a new demo company with a month and a half of sample records, made through the same helpers the pages use so every
    page (stock, suppliers, customers, accounts, payroll, greenhouse, reports) agrees. Runs as the demo's admin (g.user); the caller commits."""
    username = g.user["username"]
    rng = random.Random(biz)
    current = now().replace(tzinfo=None)
    today = current.date()
    stamp = current.strftime("%Y-%m-%d %H:%M:%S")

    def day(days_ago):
        return (today - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")

    for name in ("Agro Shop", "Greenhouse"):
        db.execute("INSERT INTO departments (business_id, name) VALUES (?, ?)", (biz, name))
    for name in ("Fertilisers", "Chemicals", "Tools", "Vegetable Seeds", "Seedlings"):
        db.execute("INSERT INTO categories (business_id, name) VALUES (?, ?)", (biz, name))

    # What was held in cash, at the bank and in each wallet before the first record
    for account, amount in (("Cash", 3000), ("Bank", 120000), ("Airtel Money", 1500), ("MTN Money", 1000)):
        db.execute(
            "INSERT INTO money_balances (business_id, account, amount, as_of, last_id, expected, set_by, set_at) VALUES (?, ?, ?, ?, 0, NULL, ?, ?)",
            (biz, account, amount, f"{day(46)} 07:00:00", username, stamp),
        )

    # Stock bought from the three suppliers, then part of each item put on the POS
    pos = []
    for i, (code, name, category, dept, unit, qty, cost, price, supplier, on_pos) in enumerate(DEMO_ITEMS):
        bought = day(45 - i // 4)
        share = DEMO_OWING.get(code, 1.0)
        supplier_name, supplier_phone = DEMO_SUPPLIERS[supplier]
        record_purchase(db, biz, {
            "item_code": code, "name": name, "category": category, "department": dept, "batch_no": f"B{bought.replace('-', '')[2:]}",
            "date_planted": "", "quantity": qty, "unit_cost": float(cost), "price": float(price), "unit": unit,
            "supplier_name": supplier_name, "supplier_phone": supplier_phone, "invoice_no": f"SUP-{101 + i}", "purchase_date": bought,
            "expiry_date": "", "seeds_per_pack": 1000 if is_seed_category(category) else None,
            "amount_paid": round(qty * cost * share, 2), "payment_method": "Bank transfer", "payment_reference": f"TRF{4100 + i}",
        })
        listed = int(qty * on_pos)
        if listed:
            item = db.one("SELECT id, batch_no FROM products WHERE business_id=? AND item_code=?", (biz, code))
            db.execute("UPDATE products SET quantity = quantity - ? WHERE id=?", (listed, item["id"]))
            pos_id = db.insert(
                """INSERT INTO pos_products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, price)
                   VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)""",
                (biz, code, name, category, dept, item["batch_no"], listed, float(price)),
            )
            # keep: what the sample sales leave on the shelf, so the demo's POS still has every item to sell
            pos.append({"id": pos_id, "item_code": code, "name": name, "department": dept, "batch_no": item["batch_no"], "quantity": listed,
                        "price": float(price), "keep": max(2, listed // 3)})

    # A month of sales: walk-ins pay at once, named customers sometimes buy on credit
    for days_ago in range(30, -1, -1):
        times = sorted(f"{rng.randint(8, 16):02d}:{rng.randint(0, 59):02d}:00" for _ in range(rng.choice((0, 1, 1, 2, 2, 3))))
        for clock in times:
            sold_at = f"{day(days_ago)} {clock}"
            if sold_at > stamp:
                continue
            stock = [p for p in pos if p["quantity"] > p["keep"]]
            if not stock:
                break
            lines = []
            for p in rng.sample(stock, min(len(stock), rng.choice((1, 1, 2, 3)))):
                qty = min(p["quantity"] - p["keep"], rng.choice((1, 1, 2, 3)))
                p["quantity"] -= qty
                lines.append((p, qty, round(qty * p["price"], 2)))
            total = round(sum(line_total for _, _, line_total in lines), 2)
            customer, phone = rng.choice(DEMO_CUSTOMERS) if rng.random() < 0.55 else ("", "")
            roll = rng.random()
            status = "Paid" if not customer or roll < 0.7 else ("Partial Payment" if roll < 0.88 else "Unpaid")
            paid = {"Paid": total, "Partial Payment": round(total / 2, 2), "Unpaid": 0.0}[status]
            method = rng.choice(("Cash", "Cash", "Cash", "Airtel Money", "Airtel Money", "MTN Money", "Bank transfer")) if paid else NOT_RECORDED
            invoice_no = next_invoice_no(db, biz)
            invoice_id = db.insert(
                """INSERT INTO invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at, customer_id)
                   VALUES (?, ?, 'SALE', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (biz, invoice_no, customer or WALK_IN, phone, status, total, paid, round(total - paid, 2), username, sold_at,
                 customer_for(db, biz, customer, phone, sold_at)),
            )
            record_money(db, biz, "in", paid, method, "sale", "", customer or WALK_IN, f"Invoice {invoice_no}", "invoice", invoice_id, sold_at)
            for p, qty, line_total in lines:
                db.execute(
                    """INSERT INTO invoice_items (invoice_id, business_id, item_code, item_name, department, batch_no, qty, unit_price, line_total)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (invoice_id, biz, p["item_code"], p["name"], p["department"], p["batch_no"], qty, p["price"], line_total),
                )
    for p in pos:
        db.execute("UPDATE pos_products SET quantity=? WHERE id=?", (p["quantity"], p["id"]))

    for days_ago, amount, method, category, party, description in DEMO_EXPENSES:
        record_money(db, biz, "out", amount, method, category, "", party, description, "manual", None, f"{day(days_ago)} 10:00:00")

    # Three employees, each paid for last month on the first of this month
    rules = hr_settings_for(db, biz)
    last_month = (today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    expected = float(expected_work_days(last_month.year, last_month.month, rules["non_working_days"].split(",")))
    paid_at = min(f"{today.replace(day=1)} 09:00:00", stamp)
    for name, nrc, salary, kind, address, age, days_week, missed in DEMO_EMPLOYEES:
        employee_id = db.insert(
            """INSERT INTO employees (business_id, full_name, nrc_number, basic_salary, employment_type, address, age, start_date, expected_days_week)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (biz, name, nrc, float(salary), kind, address, age, day(400 + age), days_week),
        )
        worked = expected - missed
        calculated_basic = round(salary / expected * worked, 2)
        share = statutory_amounts(rules, calculated_basic, 0.0)
        net = round(calculated_basic - share["napsa_employee"] - share["nhima_employee"], 2)
        month = last_month.strftime("%Y-%m")
        slip_id = db.insert(
            """INSERT INTO payroll (business_id, employee_id, employee_name, pay_month, basic_salary, expected_days, worked_days, calculated_basic,
                                    bonuses_json, deductions_json, napsa_amt, nhima_amt, employer_napsa, employer_nhima, net_payable, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', ?, ?, ?, ?, ?, ?)""",
            (biz, employee_id, name, month, float(salary), expected, worked, calculated_basic, share["napsa_employee"], share["nhima_employee"],
             share["napsa_employer"], share["nhima_employer"], net, paid_at),
        )
        record_payslip_money(db, biz, slip_id, name, month, net, sum(share.values()), "Bank transfer", paid_at, username)

    # Greenhouse: one batch ready and selling, one about to be listed, one still growing
    for name, batch, seeds, mortality, seed_cost, price, planted, ready_in, ready in DEMO_PLANTINGS:
        ready_on = (today + datetime.timedelta(days=ready_in)).strftime("%Y-%m-%d")
        insert_planting(db, biz, {
            "seed_code": seed_code_for(name.split()[0]), "seed_name": name.split()[0] + " seeds", "name": name, "department": "Greenhouse",
            "batch_no": batch, "source": "manual", "seeds": seeds, "mortality": mortality, "seedlings": seedlings_from(seeds, mortality),
            "seed_cost": seed_cost, "cost_per_seedling": seed_cost / (1 - mortality / 100), "price": price, "planted_on": day(planted),
            "ready_on": ready_on, "actual_ready_on": ready_on if ready else None, "notes": "Sample batch",
        })


# --- SALES & POS ---
@app.get("/")
@need(None)
def pos():
    db, biz = get_db(), g.user["business_id"]
    growing = release_plantings(db, biz)
    products = db.all(
        "SELECT id, item_code, name, department, date_planted, quantity, price FROM pos_products WHERE business_id=? AND quantity > 0 ORDER BY name",
        (biz,),
    )
    # The figures on the tiles, in one round trip
    sales = "FROM invoices WHERE business_id=? AND kind='SALE' AND payment_status <> 'Cancelled'"
    totals = db.one(
        f"""SELECT (SELECT COALESCE(SUM(ii.qty), 0) FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
                    WHERE i.business_id=? AND i.kind='SALE' AND i.payment_status <> 'Cancelled') AS units,
                   (SELECT COALESCE(SUM(amount_paid), 0) {sales}) AS received, (SELECT COALESCE(SUM(balance_due), 0) {sales}) AS owed,
                   (SELECT COALESCE(SUM(amount_paid), 0) FROM procurements WHERE business_id=?) AS paid,
                   (SELECT COALESCE(SUM(total_cost - amount_paid), 0) FROM procurements WHERE business_id=?) AS payable,
                   (SELECT COUNT(*) FROM preorders WHERE business_id=? AND status='pending') AS preorders,
                   (SELECT COALESCE(SUM(qty), 0) FROM preorders WHERE business_id=? AND status='pending') AS preorder_qty,
                   (SELECT COUNT(*) {WAITING_SQL}) AS waiting, (SELECT MIN(collect_on) {WAITING_SQL}) AS next_collection,
                   (SELECT COUNT(*) {TO_DELIVER_SQL}) AS to_deliver, (SELECT MIN(deliver_on) {TO_DELIVER_SQL}) AS next_delivery""",
        (biz,) * 11,
    )
    stats = {
        "listed": len(products),
        "low": sum(1 for p in products if p["quantity"] < g.user["low_stock_threshold"]),
        "units": totals["units"],
        "revenue": totals["received"],
        "receivable": totals["owed"],
        "expenditure": totals["paid"],
        "payable": totals["payable"],
    }
    known_customers = db.all("SELECT name, phone FROM customers WHERE business_id=? ORDER BY name LIMIT 1000", (biz,))
    # Pre-orders sent here by "Generate invoice": they go into the cart at the quantity and price agreed when they were placed
    preorder_lines, problem = pos_preorders(db, biz, request.args.getlist("preorder", type=int))
    if problem:
        flash(problem, "error")
    return render_template(
        "pos.html", products=products, stats=stats, departments=department_names(db, biz), statuses=PAYMENT_STATUSES, methods=PAYMENT_METHODS,
        known_customers=known_customers, up_next=up_next_summary(db, biz, growing, totals), removal_reasons=POS_REMOVAL_REASONS,
        preorder_lines=preorder_lines, waiting=totals["waiting"], next_collection=totals["next_collection"], tomorrow=add_days(today_str(), 1),
        to_deliver=totals["to_deliver"], next_delivery=totals["next_delivery"], today=today_str(),
    )


POS_REMOVAL_REASONS = (("disease", "Disease"), ("damaged", "Damaged / spoiled"), ("mortality", "Mortality"), ("other", "Other"))


def record_stock_adjustment(db, biz, kind, item_code, item_name, department, before, after, reason, note, pos_product_id=None, product_id=None):
    """Logs a manual stock change (a POS removal or an inventory quantity correction) so it stays reviewable."""
    db.execute(
        """INSERT INTO stock_adjustments (business_id, kind, item_code, item_name, department, quantity_before, quantity_after, delta, reason, note,
                                          pos_product_id, product_id, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            biz, kind, item_code, item_name, department, before, after, after - before, reason, note,
            pos_product_id, product_id, g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )


@app.post("/pos/<int:pos_id>/remove")
@need("transfer")
def remove_from_pos(pos_id):
    """Takes stock listed in the POS off sale - disease, damage, mortality or anything else - with a reason kept on record."""
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("pos"))
    p = db.one("SELECT id, item_code, name, department, quantity FROM pos_products WHERE id=? AND business_id=?", (pos_id, biz))
    if p is None:
        abort(404)

    reason_key = text("reason")
    reason_labels = dict(POS_REMOVAL_REASONS)
    if reason_key not in reason_labels:
        flash("Choose why this stock is being removed.", "error")
        return back
    try:
        qty = number("quantity", int)
        if qty < 1:
            raise ValueError
    except ValueError:
        flash("Enter how many to remove (a whole number above zero).", "error")
        return back
    if qty > p["quantity"]:
        flash(f"Only {p['quantity']:,} of '{p['name']}' are listed. Nothing was removed.", "error")
        return back
    note = text("note")[:300]
    if reason_key == "other" and not note:
        flash("Say what happened, since the reason is 'Other'.", "error")
        return back

    new_qty = p["quantity"] - qty
    try:
        db.execute("UPDATE pos_products SET quantity=? WHERE id=? AND business_id=?", (new_qty, pos_id, biz))
        record_stock_adjustment(
            db, biz, "pos_removal", p["item_code"], p["name"], p["department"], p["quantity"], new_qty,
            reason_labels[reason_key], note, pos_product_id=pos_id,
        )
        notify(
            db, "stock_removal", f"Stock removed from the POS: {p['name']}",
            f"{g.user['username']} removed {qty:,} of '{p['name']}' (code {p['item_code']}) from the POS. Reason: {reason_labels[reason_key]}."
            + (f" {note}" if note else ""),
            url_for("stock_adjustments"),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The removal could not be saved. Please try again.", "error")
        return back

    flash(f"Removed {qty:,} of '{p['name']}' from the POS ({reason_labels[reason_key]}). {new_qty:,} remain listed.", "ok")
    return back


# A sale the customer collects on a later day is "waiting" from the sale until it is marked collected (or cancelled)
WAITING_SQL = "FROM invoices WHERE business_id=? AND kind='SALE' AND collect_on IS NOT NULL AND collected_at IS NULL AND COALESCE(payment_status, '') <> 'Cancelled'"
# A sale we deliver is "to deliver" from the sale until it is marked delivered (or cancelled)
TO_DELIVER_SQL = "FROM invoices WHERE business_id=? AND kind='SALE' AND deliver_on IS NOT NULL AND delivered_at IS NULL AND COALESCE(payment_status, '') <> 'Cancelled'"


def read_discount(raw, gross):
    """Money off a sale line worth gross, typed as an amount ("5", "5.50") or a percentage ("10%"). Blank is none. Raises ValueError
    when it is not a number, or more than the line is worth."""
    raw = " ".join(str(raw or "").replace(",", "").split())
    if not raw:
        return 0.0
    if raw.endswith("%"):
        pct = float(raw[:-1].strip())
        if not 0 <= pct <= 100:
            raise ValueError(raw)
        amount = gross * pct / 100
    else:
        amount = float(raw)
    amount = round(amount, 2)
    if amount < 0 or amount > round(gross, 2) + 0.005:
        raise ValueError(raw)
    return amount


def read_handover():
    """How the customer gets the goods, from the sale form: (collect_on, deliver_on, problem). Taken now leaves both empty; a later
    collection is from tomorrow on, a delivery from today on."""
    choice = text("collection")
    if choice == "later":
        day = day_or_none(text("collect_on")) if text("collect_on") else None
        earliest = add_days(today_str(), 1)
        if day is None:
            return None, None, "Choose the day the customer will collect the goods."
        if day < earliest:
            return None, None, f"The collection day must be {earliest} or later (or choose that they take the goods now)."
        return day, None, None
    if choice == "deliver":
        day = day_or_none(text("deliver_on")) if text("deliver_on") else None
        if day is None:
            return None, None, "Choose the day the goods will be delivered."
        if day < today_str():
            return None, None, "The delivery day cannot be in the past."
        return None, day, None
    return None, None, None


class SaleRefused(Exception):
    """A sale that cannot be recorded as it stands: what to tell the person, and where to send them to put it right."""

    def __init__(self, message, back=None):
        super().__init__(message)
        self.message, self.back = message, back


def record_sale(offline=None):
    """Records the sale in the posted form (the POS cart) and returns its invoice id, or raises SaleRefused and saves nothing.

    offline: a sale made on a device with no connection, uploaded now ({"ref", "sold_at", "prices"}). It was made with the goods in front
    of the seller, so it is recorded even when the app's stock says there is not enough (stock may go below zero, and admins are told to
    count it); at the price the device showed; on the day and time it was made; and taken there and then (no delivery or collection)."""
    db, biz = get_db(), g.user["business_id"]

    # The browser only says which products and how many, any discount, and which pre-orders; names, prices and stock come from the database
    wanted, preorder_ids, discounts = {}, [], {}
    try:
        for line in json.loads(request.form.get("cart", "[]")):
            if "preorder" in line:
                if offline:
                    continue   # a pre-order is collected online, where it can be claimed
                preorder_ids.append(int(line["preorder"]))
                discounts[("po", int(line["preorder"]))] = line.get("discount", "")
                continue
            pid, qty = int(line["id"]), int(line["qty"])
            if qty <= 0:
                raise ValueError("qty")
            wanted[pid] = wanted.get(pid, 0) + qty
            discounts[("item", pid)] = line.get("discount", "")
    except (ValueError, KeyError, TypeError, AttributeError):
        wanted, preorder_ids = {}, []
    # Back to the POS with the pre-orders still in the cart, when something has to be put right
    back = redirect(url_for("pos", preorder=preorder_ids) if preorder_ids else url_for("pos"))
    if not wanted and not preorder_ids:
        raise SaleRefused("Add at least one item to the cart before completing the invoice.", back)

    lines, total = [], 0.0
    if wanted:
        # Every item in the cart in one query
        ids = list(wanted)
        found = {
            p["id"]: p
            for p in db.all(
                f"SELECT id, item_code, name, department, batch_no, quantity, price FROM pos_products WHERE business_id=? AND id IN ({', '.join('?' for _ in ids)})",
                [biz] + ids,
            )
        }
        for pid, qty in wanted.items():
            p = found.get(pid)
            if not p:
                raise SaleRefused("An item in this sale is no longer listed on the POS.", back)
            if qty > p["quantity"] and not offline:
                raise SaleRefused(f"Insufficient stock for {p['name']}. Available: {p['quantity']}.", back)
            price = p["price"]
            if offline and offline["prices"].get(pid) is not None:
                price = offline["prices"][pid]   # what the customer was charged, from the device's copy of the prices
            gross = round(qty * price, 2)
            try:
                discount = read_discount(discounts.get(("item", pid)), gross)
            except ValueError:
                raise SaleRefused(f"The discount on {p['name']} must be an amount up to {money(gross)}, or a percentage like 10%.", back)
            lines.append((p, qty, discount, round(gross - discount, 2), price))
            total += gross - discount

    # Pre-orders: at the quantity and price agreed when they were placed. Their seedlings were held from the batch then, so the
    # sale takes nothing more out of stock.
    preorders = []
    if preorder_ids:
        preorders, problem = pos_preorders(db, biz, preorder_ids)
        if problem or len(preorders) != len(set(preorder_ids)):
            raise SaleRefused(problem or "Some of these pre-orders were already collected or cancelled. Please check the cart.",
                              redirect(url_for("pos", preorder=[po["preorder"] for po in preorders]) if preorders else url_for("pos")))
        for po in preorders:
            try:
                po["discount"] = read_discount(discounts.get(("po", po["preorder"])), po["total"])
            except ValueError:
                raise SaleRefused(f"The discount on {po['name']} must be an amount up to {money(po['total'])}, or a percentage like 10%.", back)
            po["line_total"] = round(po["total"] - po["discount"], 2)
            total += po["line_total"]
    total = round(total, 2)

    status = text("payment_status")
    if status not in PAYMENT_STATUSES:
        status = "Paid"
    if status == "Paid":
        amount_paid = total
    elif status == "Unpaid":
        amount_paid = 0.0
    else:
        try:
            amount_paid = round(number("amount_paid"), 2)
        except ValueError:
            raise SaleRefused("Enter the amount paid for a partial payment.", back)
        if amount_paid <= 0 or amount_paid >= total:
            raise SaleRefused("A partial payment must be more than zero and less than the invoice total.", back)
    # Whatever is paid now goes to Accounts with how it was paid: cash, bank or mobile money
    method, reference = NOT_RECORDED, ""
    if amount_paid > 0.005:
        method, reference, problem = read_money_method()
        if problem:
            raise SaleRefused(problem, back)
    # Taken now, collected on a later day (reminded the day before), or delivered on a day (on that day's delivery list)
    collect_on, deliver_on, problem = (None, None, None) if offline else read_handover()
    if problem:
        raise SaleRefused(problem, back)

    try:
        sold_at = offline["sold_at"] if offline else now().strftime("%Y-%m-%d %H:%M:%S")
        invoice_no, customer = next_invoice_no(db, biz), text("customer_name") or WALK_IN
        invoice_id = db.insert(
            """INSERT INTO invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at,
                                     customer_id, collect_on, deliver_on, offline_ref)
               VALUES (?, ?, 'SALE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, invoice_no, customer, text("customer_phone"),
                status, total, amount_paid, round(total - amount_paid, 2), g.user["username"], sold_at,
                customer_for(db, biz, text("customer_name"), text("customer_phone"), sold_at), collect_on, deliver_on, offline["ref"] if offline else None,
            ),
        )
        record_money(
            db, biz, "in", amount_paid, method, "sale", reference, customer, f"Invoice {invoice_no}" + (" (pre-order collected)" if preorders else ""),
            "invoice", invoice_id, sold_at,
        )
        short = []
        for p, qty, discount, line_total, price in lines:
            if offline:
                # Sold from what was in front of the seller: recorded whatever the app's stock said
                db.execute("UPDATE pos_products SET quantity = quantity - ? WHERE id=? AND business_id=?", (qty, p["id"], biz))
                left = db.one("SELECT quantity FROM pos_products WHERE id=?", (p["id"],))["quantity"]
                if left < 0:
                    short.append((p, left))
            else:
                # The quantity check in the WHERE clause stops two tills selling the same last units
                moved = db.execute(
                    "UPDATE pos_products SET quantity = quantity - ? WHERE id=? AND business_id=? AND quantity >= ?",
                    (qty, p["id"], biz, qty),
                )
                if moved.rowcount != 1:
                    raise StockError(p["name"])
            low_stock_alert(db, p["name"], p["item_code"], p["quantity"], p["quantity"] - qty, "listed for sale in the POS")
            db.execute(
                """INSERT INTO invoice_items (invoice_id, business_id, item_code, item_name, department, batch_no, qty, unit_price, line_total, discount)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (invoice_id, biz, p["item_code"], p["name"], p["department"], p["batch_no"], qty, price, line_total, discount or None),
            )
        for p, left in short:
            notify(db, "low_stock", f"Count {p['name']}: the app's stock is below zero",
                   f"A sale made offline ({offline['ref']}, now {invoice_no}) sold more {p['name']} (code {p['item_code']}) than the app had in stock: "
                   f"it now shows {left}. Count it and correct the stock.", url_for("inventory"), whatsapp=True)
        for po in preorders:
            # Claimed only while still pending, so two tills cannot both sell the same pre-order
            claimed = db.execute(
                "UPDATE preorders SET status='collected', collected_by=?, approved_by='', collected_at=?, invoice_id=? WHERE id=? AND business_id=? AND status='pending'",
                (g.user["username"], sold_at, invoice_id, po["preorder"], biz),
            )
            if claimed.rowcount != 1:
                raise PreorderGone(po["customer_name"])
            db.execute(
                """INSERT INTO invoice_items (invoice_id, business_id, item_code, item_name, department, batch_no, qty, unit_price, line_total, discount)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (invoice_id, biz, po["pos_code"], po["name"], po["department"], po["batch_no"], po["qty"], po["price"], po["line_total"], po["discount"] or None),
            )
        if offline:
            db.execute("INSERT INTO offline_uploads (business_id, upload_key, invoice_id, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                       (biz, offline["key"], invoice_id, g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S")))
        db.commit()
    except PreorderGone as e:
        db.rollback()
        raise SaleRefused(f"{e}'s pre-order was just collected or cancelled elsewhere. Nothing was saved; please review the cart.", redirect(url_for("pos")))
    except StockError as e:
        db.rollback()
        raise SaleRefused(f"'{e}' was just sold elsewhere and is now short. Nothing was saved; please review the cart.", back)
    except DB_ERRORS:
        db.rollback()
        raise SaleRefused("The sale could not be saved. Please try again.", back)

    if collect_on:
        flash(f"To be collected on {collect_on}. Everyone gets a reminder the day before.", "ok")
    if deliver_on:
        flash(f"To be delivered on {deliver_on}: it is on that day's delivery list.", "ok")
    return invoice_id


@app.post("/checkout")
@need("sell")
def checkout():
    try:
        invoice_id = record_sale()
    except SaleRefused as e:
        flash(e.message, "error")
        return e.back
    return redirect(url_for("invoice", invoice_id=invoice_id))


# --- OFFLINE ---
# The app works on a bad network day: each signed-in device keeps a snapshot of what it needs to show stock, prices, customers, recent
# invoices and plantings, and to sell (offline.js keeps it fresh while there is a connection). A sale made with no connection waits on the
# device and is uploaded here when the connection is back, through the same checks as any sale (record_sale). Pages already opened are
# kept by the service worker (sw.js) and shown, marked as an offline copy, when they cannot be fetched.
OFFLINE_DAYS = 30            # the invoices a device keeps
OFFLINE_SALE_DAYS = 14       # how old an offline sale may be when it is uploaded


@app.get("/offline/snapshot")
@need(None)
def offline_snapshot():
    db, biz, user = get_db(), g.user["business_id"], g.user
    admin = user["role"] == "admin"
    since = add_days(today_str(), -OFFLINE_DAYS)
    products = db.all("SELECT id, item_code, name, category, department, batch_no, quantity, price FROM pos_products WHERE business_id=? ORDER BY name", (biz,))
    inventory = db.all(
        "SELECT item_code, name, category, department, quantity, unit, unit_cost, price, expiry_date FROM products WHERE business_id=? ORDER BY name", (biz,))
    if admin:
        customers = db.all(
            f"""SELECT c.name, c.phone, COALESCE(SUM(CASE WHEN i.kind='SALE' AND i.payment_status <> 'Cancelled' THEN i.balance_due ELSE 0 END), 0) AS owes
                FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id AND i.business_id = c.business_id
                WHERE c.business_id=? GROUP BY c.id, c.name, c.phone ORDER BY c.name""", (biz,))
    else:
        customers = db.all("SELECT name, phone FROM customers WHERE business_id=? ORDER BY name", (biz,))
    invoices = db.all(
        """SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_at, offline_ref
           FROM invoices WHERE business_id=? AND created_at >= ? ORDER BY id DESC LIMIT 2000""", (biz, since))
    items = {}
    for r in db.all(
        """SELECT ii.invoice_id, ii.item_name, ii.qty, ii.unit_price, ii.line_total FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
           WHERE ii.business_id=? AND i.created_at >= ? ORDER BY ii.id""", (biz, since)):
        items.setdefault(r["invoice_id"], []).append([r["item_name"], r["qty"], r["unit_price"], r["line_total"]])
    batches = with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? AND still_in_greenhouse=1 ORDER BY ready_on, id", (biz,)))
    preorders = db.all(
        """SELECT po.customer_name, po.customer_phone, po.qty, po.collect_on, p.name AS plant, p.batch_no, p.planted_on FROM preorders po
           JOIN plantings p ON p.id = po.planting_id WHERE po.business_id=? AND po.status='pending' ORDER BY po.collect_on, po.id""", (biz,))
    response = jsonify({
        "at": now().strftime("%Y-%m-%d %H:%M:%S"), "csrf": csrf_token(), "days": OFFLINE_DAYS,
        "user": {"id": user["id"], "name": user["username"], "admin": admin, "sell": can("sell")},
        "business": {"name": user["business_name"], "currency": user["currency"] or "", "low": user["low_stock_threshold"], "walk_in": WALK_IN},
        "methods": METHOD_NAMES, "statuses": list(PAYMENT_STATUSES), "urls": {"sales": url_for("offline_sale"), "snapshot": url_for("offline_snapshot")},
        "products": products, "inventory": inventory, "customers": customers,
        "invoices": [dict(i, items=items.get(i["id"], [])) for i in invoices],
        "plantings": [{"batch": b["label"], "planted_on": b["planted_on"], "ready_on": b["actual_ready_on"] or b["ready_on"], "stage": b["stage_label"],
                       "seedlings": b["seedlings"], "free": b["available"], "preordered": b["pending"], "price": b["price"]} for b in batches],
        "preorders": [{"customer": p["customer_name"], "phone": p["customer_phone"], "qty": p["qty"], "collect_on": p["collect_on"],
                       "batch": planting_label({"name": p["plant"], "batch_no": p["batch_no"], "planted_on": p["planted_on"]})} for p in preorders],
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/offline/sales")
@need("sell")
def offline_sale():
    """One sale made offline: recorded once, however often it is sent, as {ok, invoice_no, url} or {ok: false, error} for the device to show."""
    db, biz = get_db(), g.user["business_id"]
    key, ref = text("upload_key"), text("offline_ref")[:30]
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", key) or not re.fullmatch(r"OFF-[A-Za-z0-9-]{1,24}", ref):
        return jsonify({"ok": False, "error": "This sale is not one the app made offline."}), 400
    done = db.one("SELECT u.invoice_id, i.invoice_no FROM offline_uploads u LEFT JOIN invoices i ON i.id = u.invoice_id WHERE u.business_id=? AND u.upload_key=?", (biz, key))
    if done:
        return jsonify({"ok": True, "invoice_no": done["invoice_no"], "url": url_for("invoice", invoice_id=done["invoice_id"]), "again": True})
    # When it was made: on the device's clock, kept if it is believable (not ahead of now, not older than OFFLINE_SALE_DAYS)
    sold_at = now().strftime("%Y-%m-%d %H:%M:%S")
    made = text("sold_at")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", made) and add_days(today_str(), -OFFLINE_SALE_DAYS) <= made[:10] and made <= sold_at:
        sold_at = made
    try:
        prices = {int(k): round(float(v), 2) for k, v in json.loads(request.form.get("prices", "{}")).items() if float(v) >= 0}
    except (ValueError, TypeError, AttributeError):
        prices = {}
    try:
        invoice_id = record_sale(offline={"key": key, "ref": ref, "sold_at": sold_at, "prices": prices})
    except SaleRefused as e:
        session.pop("_flashes", None)
        again = db.one("SELECT u.invoice_id, i.invoice_no FROM offline_uploads u LEFT JOIN invoices i ON i.id = u.invoice_id WHERE u.business_id=? AND u.upload_key=?", (biz, key))
        if again:   # the same sale sent twice at once: the other one recorded it
            return jsonify({"ok": True, "invoice_no": again["invoice_no"], "url": url_for("invoice", invoice_id=again["invoice_id"]), "again": True})
        return jsonify({"ok": False, "error": e.message}), 409
    session.pop("_flashes", None)
    number = db.one("SELECT invoice_no FROM invoices WHERE id=?", (invoice_id,))["invoice_no"]
    return jsonify({"ok": True, "invoice_no": number, "url": url_for("invoice", invoice_id=invoice_id)})


# --- Sales collected on a later day ---
@app.post("/invoices/<int:invoice_id>/collected")
@need("sell")
def invoice_collected(invoice_id):
    """The customer has taken the goods of a sale they were to collect later."""
    db, biz = get_db(), g.user["business_id"]
    inv = db.one("SELECT invoice_no, collect_on, collected_at, payment_status FROM invoices WHERE id=? AND business_id=? AND kind='SALE'", (invoice_id, biz))
    if inv is None:
        abort(404)
    if not inv["collect_on"] or inv["collected_at"] or inv["payment_status"] == "Cancelled":
        flash("This sale is not waiting to be collected.", "error")
    else:
        db.execute("UPDATE invoices SET collected_at=?, collected_by=? WHERE id=? AND business_id=?",
                   (now().strftime("%Y-%m-%d %H:%M:%S"), g.user["username"], invoice_id, biz))
        db.commit()
        flash(f"{inv['invoice_no']} marked as collected.", "ok")
    return redirect(url_for("invoice", invoice_id=invoice_id))


@app.post("/invoices/<int:invoice_id>/collect-day")
@need("sell")
def invoice_collect_day(invoice_id):
    """Moves the day a sale is to be collected. Its reminder is raised again for the new day."""
    db, biz = get_db(), g.user["business_id"]
    inv = db.one("SELECT collect_on, collected_at, payment_status FROM invoices WHERE id=? AND business_id=? AND kind='SALE'", (invoice_id, biz))
    if inv is None:
        abort(404)
    day = day_or_none(text("collect_on")) if text("collect_on") else None
    if not inv["collect_on"] or inv["collected_at"] or inv["payment_status"] == "Cancelled":
        flash("This sale is not waiting to be collected.", "error")
    elif day is None or day < today_str():
        flash("Choose the new collection day (today or later). Nothing was changed.", "error")
    else:
        db.execute("UPDATE invoices SET collect_on=?, collect_reminded_at=NULL WHERE id=? AND business_id=?", (day, invoice_id, biz))
        db.commit()
        flash(f"Now to be collected on {day}.", "ok")
    return redirect(url_for("invoice", invoice_id=invoice_id))


def remind_collections_due(db, biz):
    """The day before a sale is to be collected (or any later day it is still waiting), everyone is told, once. Runs from the alerts
    poll; each sale is claimed before its alert is raised, so two pages polling at once cannot both raise it."""
    if not alert_enabled("collections"):
        return
    today = today_str()
    due = db.all(f"SELECT id, invoice_no, customer_name, collect_on {WAITING_SQL} AND collect_reminded_at IS NULL AND collect_on <= ? ORDER BY collect_on, id",
                 (biz, add_days(today, 1)))
    if not due:
        return
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    claimed = [d for d in due if db.execute("UPDATE invoices SET collect_reminded_at=? WHERE id=? AND collect_reminded_at IS NULL", (stamp, d["id"])).rowcount == 1]
    if not claimed:
        return
    items = {}
    for line in db.all(
        f"SELECT invoice_id, item_name, qty FROM invoice_items WHERE invoice_id IN ({', '.join('?' for _ in claimed)}) ORDER BY id", [d["id"] for d in claimed]
    ):
        items.setdefault(line["invoice_id"], []).append(f"{line['qty']:,} x {line['item_name']}")
    for d in claimed:
        when = when_words(d["collect_on"], today, before="due ")
        title = f"Collection {'today' if when == 'today' else when.split(',')[0]}: {d['invoice_no']} ({d['customer_name']})"
        body = f"{d['customer_name']} collects invoice {d['invoice_no']} {when}: {', '.join(items.get(d['id'], [])) or 'see the invoice'}."
        tell_everyone(db, biz, "collection", title, body, url_for("invoice", invoice_id=d["id"]))
    db.commit()



# --- Deliveries ---
# A sale can be delivered to the customer on a day. The deliveries of a day are that day's delivery list, compiled by itself, and on
# the morning of the day everyone is told. A delivery list can also be made from any invoices ticked on Sales & Invoices. Either way
# it covers several customers, laid out as a note for each customer (to sign and leave with them) or as one route sheet.
DELIVERY_LAYOUTS = (("notes", "A note for each customer"), ("route", "One route sheet"))
INVOICE_DELIVERY_COLUMNS = """id, invoice_no, created_at, customer_id, customer_name, customer_phone, payment_status, total, amount_paid, balance_due,
                              collect_on, collected_at, deliver_on, delivered_at"""


def delivery_selection(db, biz, ids=(), day=None):
    """The invoices on a delivery list (the ticked ones, or those to be delivered on a day; sales that were not cancelled), with their
    lines, gathered by customer in the order of the list."""
    if day:
        invoices = db.all(
            f"""SELECT {INVOICE_DELIVERY_COLUMNS} FROM invoices WHERE business_id=? AND kind='SALE' AND payment_status <> 'Cancelled' AND deliver_on=?
                ORDER BY customer_name, created_at, id""",
            (biz, day),
        )
    else:
        ids = list(dict.fromkeys(ids))[:300]
        invoices = db.all(
            f"""SELECT {INVOICE_DELIVERY_COLUMNS} FROM invoices WHERE business_id=? AND kind='SALE' AND payment_status <> 'Cancelled'
                AND id IN ({', '.join('?' for _ in ids)}) ORDER BY customer_name, created_at, id""",
            [biz] + ids,
        ) if ids else []
    lines_of = {}
    if invoices:
        for line in db.all(
            f"""SELECT invoice_id, item_code, item_name, qty, unit_price, COALESCE(discount, 0) AS discount, line_total FROM invoice_items
                WHERE business_id=? AND invoice_id IN ({', '.join('?' for _ in invoices)}) ORDER BY id""",
            [biz] + [inv["id"] for inv in invoices],
        ):
            lines_of.setdefault(line["invoice_id"], []).append(line)
    stops = {}
    for inv in invoices:
        inv["lines"] = lines_of.get(inv["id"], [])
        inv["discount"] = round(sum(line["discount"] for line in inv["lines"]), 2)
        inv["to_deliver"] = bool(inv["deliver_on"] and not inv["delivered_at"])
        inv["to_collect"] = bool(inv["collect_on"] and not inv["collected_at"])
        key = ("id", inv["customer_id"]) if inv["customer_id"] else ("name", name_key(inv["customer_name"] or ""), phone_key(inv["customer_phone"]))
        stop = stops.setdefault(key, {"name": inv["customer_name"] or WALK_IN, "phone": inv["customer_phone"] or "", "customer_id": inv["customer_id"], "invoices": []})
        stop["invoices"].append(inv)
    out = []
    for stop in stops.values():
        stop["units"] = sum(line["qty"] for inv in stop["invoices"] for line in inv["lines"])
        for field in ("total", "amount_paid", "balance_due", "discount"):
            stop[field] = round(sum(inv[field] for inv in stop["invoices"]), 2)
        out.append(stop)
    return {
        "stops": out, "invoices": invoices, "units": sum(s["units"] for s in out),
        "total": round(sum(s["total"] for s in out), 2), "paid": round(sum(s["amount_paid"] for s in out), 2), "owed": round(sum(s["balance_due"] for s in out), 2),
        "pending": [inv for inv in invoices if inv["to_deliver"] or inv["to_collect"]], "printed": now().strftime("%d %b %Y %H:%M"),
    }


def delivery_request_args():
    """What a delivery list is of, from the address: (day or None, invoice ids, layout, the arguments to make the same list again)."""
    day = day_or_none(request.args.get("day", "")) if request.args.get("day") else None
    ids = [] if day else request.args.getlist("invoice", type=int)
    layout = request.args.get("layout") if request.args.get("layout") in dict(DELIVERY_LAYOUTS) else "notes"
    args = {"day": day} if day else {"invoice": ids}
    return day, ids, layout, args


@app.get("/deliveries")
@need(None)
def deliveries():
    """The days with deliveries still to make, soonest first (overdue ones at the top), and what was delivered lately."""
    db, biz = get_db(), g.user["business_id"]
    days = db.all(
        f"""SELECT deliver_on AS day, COUNT(*) AS invoices, COUNT(DISTINCT COALESCE(CAST(customer_id AS TEXT), customer_name)) AS customers,
                   COALESCE(SUM(total), 0) AS total, COALESCE(SUM(balance_due), 0) AS owed
            {TO_DELIVER_SQL} GROUP BY deliver_on ORDER BY deliver_on""",
        (biz,),
    )
    done = db.all(
        """SELECT deliver_on AS day, COUNT(*) AS invoices, MAX(delivered_at) AS last FROM invoices
           WHERE business_id=? AND kind='SALE' AND delivered_at IS NOT NULL AND deliver_on >= ? GROUP BY deliver_on ORDER BY deliver_on DESC""",
        (biz, add_days(today_str(), -30)),
    )
    return render_template("deliveries.html", days=days, done=done, today=today_str())


@app.get("/deliveries/list")
@need(None)
def delivery_list():
    db, biz = get_db(), g.user["business_id"]
    day, ids, layout, args = delivery_request_args()
    data = delivery_selection(db, biz, ids, day)
    if not data["invoices"] and not day:
        flash("Tick at least one invoice (not cancelled) to make a delivery list.", "error")
        return redirect(url_for("history", pick="delivery"))
    return render_template(
        "delivery_list.html", d=data, day=day, layout=layout, layouts=DELIVERY_LAYOUTS, args=args, today=today_str(),
        layout_url=lambda name: url_for("delivery_list", layout=name, **args), pdf_url=url_for("delivery_list_pdf", layout=layout, **args),
    )


@app.get("/deliveries/list.pdf")
@need(None)
def delivery_list_pdf():
    db, biz = get_db(), g.user["business_id"]
    day, ids, layout, _ = delivery_request_args()
    data = delivery_selection(db, biz, ids, day)
    if not data["invoices"]:
        abort(404)
    filename = f"deliveries-{day or today_str()}-{layout}.pdf"
    return Response(delivery_list_pdf_bytes(data, day, layout), mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/deliveries/delivered")
@need("sell")
def deliveries_delivered():
    """The goods on a delivery list have reached their customers: its deliveries are marked delivered (and any sale on it that was
    waiting to be collected, collected)."""
    db, biz = get_db(), g.user["business_id"]
    ids = request.form.getlist("invoice", type=int)
    data = delivery_selection(db, biz, ids)
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    delivered = collected = 0
    for inv in data["invoices"]:
        if inv["to_deliver"]:
            delivered += db.execute("UPDATE invoices SET delivered_at=?, delivered_by=? WHERE id=? AND business_id=? AND delivered_at IS NULL",
                                    (stamp, g.user["username"], inv["id"], biz)).rowcount
        if inv["to_collect"]:
            collected += db.execute("UPDATE invoices SET collected_at=?, collected_by=? WHERE id=? AND business_id=? AND collected_at IS NULL",
                                    (stamp, g.user["username"], inv["id"], biz)).rowcount
    db.commit()
    done = [f"{delivered} delivered"] if delivered else []
    done += [f"{collected} collected"] if collected else []
    flash("Marked " + " and ".join(done) + "." if done else "Nothing on this list was waiting to be delivered or collected.", "ok")
    back = text("back")
    return redirect(back if back.startswith("/deliveries") and not back.startswith("//") else url_for("deliveries"))


@app.post("/invoices/<int:invoice_id>/delivered")
@need("sell")
def invoice_delivered(invoice_id):
    db, biz = get_db(), g.user["business_id"]
    inv = db.one("SELECT invoice_no, deliver_on, delivered_at, payment_status FROM invoices WHERE id=? AND business_id=? AND kind='SALE'", (invoice_id, biz))
    if inv is None:
        abort(404)
    if not inv["deliver_on"] or inv["delivered_at"] or inv["payment_status"] == "Cancelled":
        flash("This sale is not waiting to be delivered.", "error")
    else:
        db.execute("UPDATE invoices SET delivered_at=?, delivered_by=? WHERE id=? AND business_id=?", (now().strftime("%Y-%m-%d %H:%M:%S"), g.user["username"], invoice_id, biz))
        db.commit()
        flash(f"{inv['invoice_no']} marked as delivered.", "ok")
    return redirect(url_for("invoice", invoice_id=invoice_id))


@app.post("/invoices/<int:invoice_id>/deliver-day")
@need("sell")
def invoice_deliver_day(invoice_id):
    """Moves a delivery to another day (it leaves the old day's list for the new one, and is alerted again that morning)."""
    db, biz = get_db(), g.user["business_id"]
    inv = db.one("SELECT deliver_on, delivered_at, payment_status FROM invoices WHERE id=? AND business_id=? AND kind='SALE'", (invoice_id, biz))
    if inv is None:
        abort(404)
    day = day_or_none(text("deliver_on")) if text("deliver_on") else None
    if not inv["deliver_on"] or inv["delivered_at"] or inv["payment_status"] == "Cancelled":
        flash("This sale is not waiting to be delivered.", "error")
    elif day is None or day < today_str():
        flash("Choose the new delivery day (today or later). Nothing was changed.", "error")
    else:
        db.execute("UPDATE invoices SET deliver_on=?, deliver_reminded_at=NULL WHERE id=? AND business_id=?", (day, invoice_id, biz))
        db.commit()
        flash(f"Now to be delivered on {day}.", "ok")
    return redirect(url_for("invoice", invoice_id=invoice_id))


def remind_deliveries_due(db, biz):
    """On the morning of a delivery day (or the first time the app is opened that day), everyone is told what is to be delivered,
    once, with the day's delivery list. Deliveries still not made from earlier days are told about too."""
    if not alert_enabled("deliveries"):
        return
    today = today_str()
    due = db.all(f"SELECT id, customer_name, deliver_on {TO_DELIVER_SQL} AND deliver_reminded_at IS NULL AND deliver_on <= ? ORDER BY deliver_on, id", (biz, today))
    if not due:
        return
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    claimed = [d for d in due if db.execute("UPDATE invoices SET deliver_reminded_at=? WHERE id=? AND deliver_reminded_at IS NULL", (stamp, d["id"])).rowcount == 1]
    days = {}
    for d in claimed:
        days.setdefault(d["deliver_on"], []).append(d)
    for day, invoices in days.items():
        people = list(dict.fromkeys(d["customer_name"] for d in invoices))
        n = len(invoices)
        when = "today" if day == today else f"due since {datetime.date.fromisoformat(day).strftime('%a %d %b')}"
        title = f"Deliveries {when}: {n} invoice{'s' if n != 1 else ''} for {len(people)} customer{'s' if len(people) != 1 else ''}"
        tell_everyone(db, biz, "delivery", title, f"To deliver {when}: " + ", ".join(people[:12]) + (" and more" if len(people) > 12 else "") + ". The delivery list is ready.",
                      url_for("delivery_list", day=day))
    db.commit()


def delivery_list_pdf_bytes(d, day, layout):
    """A delivery list as a PDF: the stops first, then a note for each customer on its own page, or one route sheet."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    base = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=base["Normal"], fontSize=8, leading=10)
    centre = ParagraphStyle("centre", parent=base["Normal"], alignment=1, fontSize=9, leading=12)
    title = ParagraphStyle("title", parent=base["Title"], fontSize=16, spaceAfter=2)
    heading = ParagraphStyle("heading", parent=base["Heading3"], fontSize=11, spaceBefore=8, spaceAfter=3)
    grey, line = colors.HexColor("#eeeeee"), colors.HexColor("#bbbbbb")
    any_discount = any(inv["discount"] > 0.005 for inv in d["invoices"])

    def grid(rows, widths, bold_last=False):
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 8), ("LEADING", (0, 0), (-1, -1), 10), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("BACKGROUND", (0, 0), (-1, 0), grey), ("LINEBELOW", (0, 0), (-1, -1), 0.4, line),
        ]
        if bold_last:
            style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
        return Table(rows, colWidths=[w * mm for w in widths], repeatRows=1, style=TableStyle(style))

    def item_rows(inv):
        head = ["Code", "Item", "Qty", "Price"] + (["Discount"] if any_discount else []) + ["Total"]
        rows = [head]
        for ln in inv["lines"]:
            rows.append([Paragraph(escape(ln["item_code"] or "-"), small), Paragraph(escape(ln["item_name"]), small), f"{ln['qty']:,}", money(ln["unit_price"])]
                        + ([money(ln["discount"]) if ln["discount"] else ""] if any_discount else []) + [money(ln["line_total"])])
        widths = (36, 62, 14, 24) + ((22,) if any_discount else ()) + (24,)
        return rows, widths

    def signatures():
        sign = [["Delivered by", "", "Received by", ""], ["Name", "", "Name", ""], ["Signature", "", "Signature", ""], ["Date", "", "Date", ""]]
        return Table(sign, colWidths=[24 * mm, 64 * mm, 24 * mm, 68 * mm], rowHeights=[14, 22, 22, 22], style=TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("TEXTCOLOR", (0, 1), (0, -1), colors.grey),
            ("TEXTCOLOR", (2, 1), (2, -1), colors.grey), ("LINEBELOW", (1, 1), (1, -1), 0.4, colors.black), ("LINEBELOW", (3, 1), (3, -1), 0.4, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ]))

    def header(subtitle):
        out = [Paragraph(escape(g.user["business_name"]), title)]
        for extra in (g.user["subtitle"], g.user["contact"]):
            if extra:
                out.append(Paragraph(escape(extra), centre))
        return out + [Spacer(1, 4), Paragraph(f"<b>{escape(subtitle)}</b>", centre), Spacer(1, 8)]

    story = header(("DELIVERY LIST" if layout == "route" else "DELIVERIES") + (f" - {day}" if day else ""))
    stops = [["Customer", "Phone", "Invoices", "Items", "Total", "Still owed"]]
    for s in d["stops"]:
        stops.append([Paragraph(escape(s["name"]), small), s["phone"] or "-", Paragraph(escape(", ".join(i["invoice_no"] for i in s["invoices"])), small),
                      f"{s['units']:,}", money(s["total"]), money(s["balance_due"])])
    stops.append(["All stops", "", f"{len(d['invoices'])}", f"{d['units']:,}", money(d["total"]), money(d["owed"])])
    story.append(grid(stops, (42, 26, 52, 14, 24, 24), bold_last=True))

    if layout == "route":
        for s in d["stops"]:
            block = [Paragraph(escape(f"{s['name']}" + (f" - {s['phone']}" if s["phone"] else "")), heading)]
            for inv in s["invoices"]:
                rows, widths = item_rows(inv)
                rows.append(["", f"{inv['invoice_no']} total" + (f", {money(inv['balance_due'])} still owed" if inv["balance_due"] > 0.005 else ""), "", ""]
                            + ([""] if any_discount else []) + [money(inv["total"])])
                block.append(grid(rows, widths, bold_last=True))
            block.append(Table([["Received by", "", "Signature", ""]], colWidths=[24 * mm, 66 * mm, 22 * mm, 68 * mm], rowHeights=[24], style=TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 8), ("TEXTCOLOR", (0, 0), (0, 0), colors.grey), ("TEXTCOLOR", (2, 0), (2, 0), colors.grey),
                ("LINEBELOW", (1, 0), (1, 0), 0.4, colors.black), ("LINEBELOW", (3, 0), (3, 0), 0.4, colors.black), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ])))
            story.append(KeepTogether(block))
    else:
        for s in d["stops"]:
            story.append(PageBreak())
            story += header("DELIVERY NOTE" + (f" - {day}" if day else ""))
            story.append(Table(
                [["Customer", Paragraph(escape(s["name"]), small), "Delivery date", day or d["printed"][:11]],
                 ["Phone", s["phone"] or "-", "Invoices", Paragraph(escape(", ".join(i["invoice_no"] for i in s["invoices"])), small)]],
                colWidths=[24 * mm, 66 * mm, 28 * mm, 62 * mm],
                style=TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TEXTCOLOR", (0, 0), (0, -1), colors.grey),
                                  ("TEXTCOLOR", (2, 0), (2, -1), colors.grey), ("BOX", (0, 0), (-1, -1), 0.4, line)]),
            ))
            for inv in s["invoices"]:
                rows, widths = item_rows(inv)
                rows.append(["", "Invoice total", "", ""] + ([""] if any_discount else []) + [money(inv["total"])])
                head = f"{inv['invoice_no']} - {inv['created_at'][:10]} - {inv['payment_status']}" + (f" - balance due {money(inv['balance_due'])}" if inv["balance_due"] > 0.005 else "")
                story.append(KeepTogether([Paragraph(escape(head), heading), grid(rows, widths, bold_last=True)]))
            story.append(Spacer(1, 8))
            story.append(Table(
                [["Items delivered", f"{s['units']:,}"], ["Total of these invoices", money(s["total"])], ["Paid", money(s["amount_paid"])], ["Balance due", money(s["balance_due"])]],
                colWidths=[130 * mm, 50 * mm],
                style=TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("LINEABOVE", (0, 0), (-1, 0), 0.8, colors.black),
                                  ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("LINEBELOW", (0, -1), (-1, -1), 0.8, colors.black)]),
            ))
            story += [Spacer(1, 18), signatures()]

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 9 * mm, f"Deliveries{' for ' + day if day else ''} - printed {d['printed']}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
                            title="Delivery list", author=g.user["business_name"])
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()

# --- INVENTORY ---
ITEM_COLUMNS = "id, item_code, name, category, department, batch_no, date_planted, quantity, unit_cost, price, supplier_name, invoice_no, purchase_date, expiry_date, seeds_per_pack, unit, purpose"

# --- Packaging unit: what a quantity of an item is counted in (bag, box, kg, litre...). Seed items are always packs. ---
DEFAULT_UNITS = ("bag", "bottle", "box", "bundle", "carton", "kg", "litre", "pack", "piece", "roll", "sachet", "tin", "tray")
SEED_UNIT = "pack"


def canonical_unit(typed, known):
    """A unit name cleaned up, using the spelling already in use when it only differs by capitals or spacing.

    known maps normalised names to the spelling in use. A blank unit stays blank: it means none has been set.
    """
    label = clean_name(typed, 30)
    return known.get(norm(label), label) if label else ""


def used_units(db, biz):
    return {norm(r["unit"]): r["unit"] for r in db.all("SELECT DISTINCT unit FROM products WHERE business_id=? AND unit IS NOT NULL AND unit <> ''", (biz,))}


def resolve_unit(db, biz, typed):
    return canonical_unit(typed, {**{norm(u): u for u in DEFAULT_UNITS}, **used_units(db, biz)})


def unit_choices(db, biz):
    """Suggestions for the unit box: the usual ones plus whatever this business already uses."""
    return sorted({**{norm(u): u for u in DEFAULT_UNITS}, **used_units(db, biz)}.values(), key=str.lower)

# --- Greenhouse: seed packs are bought, sown, and the seedlings are what goes on sale ---
# An item is a seed item when its category contains the word "seed" or "seeds" ("Seeds", "Vegetable seeds").
# "Seedlings" is deliberately not matched. Stock of a seed item is counted in packs.
SEED_WORD = re.compile(r"\bseeds?\b", re.I)
SEEDLING_SUFFIX = "-SDL"


def is_seed_category(category):
    return bool(SEED_WORD.search(category or ""))


def seeds_per_pack_for(existing, category, typed):
    """The pack size to keep for a purchase. Returns (seeds per pack or None, problem or None).

    Non-seed items have none. For a seed item the form or file must say how many seeds a pack holds,
    unless the item is already in stock with a pack size. Stock is counted in packs, so one item code
    cannot switch pack size without muddling the count.
    """
    stored = existing["seeds_per_pack"] if existing else None
    if not is_seed_category(existing["category"] if existing else category):
        return None, None
    typed = (typed or "").strip()
    if typed:
        try:
            size = parse_whole(typed)
            if size < 1:
                raise ValueError
        except ValueError:
            return None, f"seeds per pack '{typed}' must be a whole number above zero"
        if stored and stored != size:
            return None, f"this item is already counted in packs of {stored:,} seeds; use a new item code for packs of {size:,}"
        return size, None
    if stored:
        return stored, None
    return None, "seeds per pack is needed for a seed item (how many seeds are in one pack)"


def seedling_name_for(seed_name):
    """'Tomato Seed' becomes 'Tomato seedlings'; a name without the word gets 'seedlings' added."""
    renamed, count = SEED_WORD.subn("seedlings", seed_name, count=1)
    return renamed if count else f"{seed_name} seedlings"


def round_up_to_cent(value):
    return math.ceil(round(value * 100, 6)) / 100


def seedling_plan(packs, seeds_per_pack, germination_pct, seed_cost):
    """What sowing these packs is expected to give. Seedlings are rounded down: better to under-promise stock."""
    seeds = packs * seeds_per_pack
    seedlings = math.floor(seeds * germination_pct / 100 + 1e-9)
    cost_per_seedling = seed_cost / (germination_pct / 100)
    return {
        "seeds": seeds, "seedlings": seedlings, "cost_per_seedling": cost_per_seedling,
        "break_even_price": round_up_to_cent(cost_per_seedling),
    }


def backfill_units():
    """Seed items are counted in packs, but older ones have no unit recorded. Safe to repeat, and one query when there is nothing to do."""
    db = Database()
    try:
        for row in db.all("SELECT DISTINCT business_id, category FROM products WHERE unit IS NULL OR unit = ''"):
            if is_seed_category(row["category"]):
                db.execute(
                    "UPDATE products SET unit=? WHERE business_id=? AND category=? AND (unit IS NULL OR unit = '')", (SEED_UNIT, row["business_id"], row["category"])
                )
        db.commit()
    except DB_ERRORS:
        db.rollback()
    finally:
        db.close()



def get_item(db, item_id):
    item = db.one(f"SELECT {ITEM_COLUMNS} FROM products WHERE id=? AND business_id=?", (item_id, g.user["business_id"]))
    if item is None:
        abort(404)
    return item


def chosen_department(db, biz):
    dept = text("department")
    return dept if dept in department_names(db, biz) else "General"


INV_PARAMS = ("category", "department", "view", "q", "period", "from", "to", "use")


def inv_link(endpoint, **changes):
    """A link to an inventory page that keeps the filters in force and changes only what is asked."""
    params = {key: value for key, value in request.args.items() if key in INV_PARAMS}
    params.update(changes)
    return url_for(endpoint, **{key: value for key, value in params.items() if value not in (None, "")})


def facets(db, biz, rows, measure):
    """The category tiles and department chips for the rows on screen, plus which of each is selected.

    Each tile counts the rows left after the *other* filter, so a number on a tile is exactly what clicking
    it shows. A name in the address that matches nothing is ignored. Rows are dicts with category and department.
    """
    cats = {norm(r["name"]): r["name"] for r in category_rows(db, biz)}
    depts = {norm(name): name for name in department_names(db, biz)}
    for r in rows:
        cats.setdefault(norm(r["category"] or "General"), clean_name(r["category"]) or "General")
        depts.setdefault(norm(r["department"] or "General"), clean_name(r["department"]) or "General")

    want_cat, want_dept = norm(request.args.get("category")), norm(request.args.get("department"))
    want_cat, want_dept = (want_cat if want_cat in cats else ""), (want_dept if want_dept in depts else "")

    def in_cat(row, key):
        return not key or norm(row["category"] or "General") == key

    def in_dept(row, key):
        return not key or norm(row["department"] or "General") == key

    return {
        "tiles": [
            dict(measure([r for r in rows if in_cat(r, key) and in_dept(r, want_dept)]), name=name, active=key == want_cat, seed=is_seed_category(name))
            for key, name in sorted(cats.items(), key=lambda pair: pair[1].lower())
        ],
        "chips": [
            dict(measure([r for r in rows if in_dept(r, key) and in_cat(r, want_cat)]), name=name, active=key == want_dept)
            for key, name in sorted(depts.items(), key=lambda pair: pair[1].lower())
        ],
        "all_categories": measure([r for r in rows if in_dept(r, want_dept)]),
        "all_departments": measure([r for r in rows if in_cat(r, want_cat)]),
        "category": cats.get(want_cat, ""), "department": depts.get(want_dept, ""),
        "match": lambda r: in_cat(r, want_cat) and in_dept(r, want_dept),
    }


def stock_measure(subset):
    return {"items": len(subset), "units": sum(r["quantity"] for r in subset), "value": sum(r["quantity"] * r["unit_cost"] for r in subset)}


def cost_measure(subset):
    return {"purchases": len(subset), "cost": sum(r["total_cost"] or 0.0 for r in subset)}


@app.get("/inventory")
@need(None)
def inventory():
    db, biz = get_db(), g.user["business_id"]
    everything = db.all(f"SELECT {ITEM_COLUMNS} FROM products WHERE business_id=? ORDER BY name", (biz,))
    # What each item has on the POS, so an item bought for sale does not look out of stock here. An item's level is low
    # by what it is for: one that is only for sale is judged by its POS stock, everything else by what is in inventory.
    on_pos = {r["item_code"]: r["quantity"] for r in db.all("SELECT item_code, quantity FROM pos_products WHERE business_id=?", (biz,))}
    for i in everything:
        i["pos_qty"] = on_pos.get(i["item_code"], 0)
        i["low"] = (i["pos_qty"] if i["purpose"] == "sale" else i["quantity"]) < g.user["low_stock_threshold"]
    search = request.args.get("q", "").strip()
    found = [i for i in everything if not search or norm(search) in norm(i["name"]) or norm(search) in norm(i["item_code"])]
    # The "what it is for" chips: each counts the items on screen before this filter. Items never marked count as not set.
    use = request.args.get("use", "") if request.args.get("use", "") in tuple(ITEM_PURPOSES) + ("unset",) else ""
    uses = [{"key": key, "name": label, "items": sum(1 for i in found if (i["purpose"] or "unset") == key)}
            for key, label in list(ITEM_PURPOSES.items()) + [("unset", "Not set")]]
    all_uses = len(found)
    if use:
        found = [i for i in found if (i["purpose"] or "unset") == use]

    # Tiles and chips narrow the list; each item is in exactly one category and one department
    facet = facets(db, biz, found, stock_measure)
    items = [i for i in found if facet["match"](i)]

    # Grouped view: "Fertiliser", "fertiliser " and "FERTILISER" are one group, under its most used spelling
    by_category = {}
    for item in items:
        label = clean_name(item["category"]) or "Uncategorised"
        group = by_category.setdefault(norm(label), {"spellings": {}, "entries": [], "units": 0, "value": 0.0, "low": 0})
        group["spellings"][label] = group["spellings"].get(label, 0) + 1
        group["name"] = max(group["spellings"], key=lambda spelling: (group["spellings"][spelling], spelling[:1].isupper(), spelling))
        group["entries"].append(item)
        group["units"] += item["quantity"]
        group["value"] += item["quantity"] * item["unit_cost"]
        group["low"] += 1 if item["low"] else 0
    categories = [group for _, group in sorted(by_category.items())]

    return render_template(
        "inventory.html", items=items, categories=categories, view="list" if request.args.get("view") == "list" else "category",
        facet=facet, search=search, has_items=bool(everything), mode="stock", use=use, uses=uses, all_uses=all_uses, purposes=ITEM_PURPOSES,
    )


@app.get("/inventory/new")
@need("procure")
def inventory_new():
    """The purchase form opened from Inventory: the 'also an inventory item' box starts ticked."""
    return purchase_form("inventory")


@app.get("/procurements/new")
@need("procure")
def procurement_new():
    """The same form opened from Procurements: the box starts unticked, so a purchase is only an expense unless it is ticked."""
    return purchase_form("procurements")


def purchase_form(origin):
    """The guided purchase form: is it an inventory item, which category, which department, then the details."""
    db, biz = get_db(), g.user["business_id"]
    items = db.all("SELECT item_code, name, category, department, seeds_per_pack, unit, purpose FROM products WHERE business_id=? ORDER BY name LIMIT 5000", (biz,))
    # How the latest purchase of each item was split, so a restock of an item bought for both starts from the same share for sale
    shares = {}
    for r in db.all("SELECT item_code, quantity, sale_qty FROM procurements WHERE business_id=? AND sale_qty IS NOT NULL AND quantity > 0 ORDER BY id", (biz,)):
        shares[r["item_code"]] = round(r["sale_qty"] / r["quantity"], 4)
    per_category, per_department = {}, {}
    for i in items:
        per_category[norm(i["category"] or "General")] = per_category.get(norm(i["category"] or "General"), 0) + 1
        per_department[norm(i["department"] or "General")] = per_department.get(norm(i["department"] or "General"), 0) + 1

    # Lets the page recognise an item that is already in stock by its code
    known = {
        i["item_code"]: {
            "name": i["name"], "category": i["category"] or "General", "department": i["department"] or "General",
            "seeds": i["seeds_per_pack"] or 0, "unit": i["unit"] or "", "purpose": i["purpose"] or "", "share": shares.get(i["item_code"]),
        }
        for i in items
    }
    categories = [{"name": r["name"], "items": per_category.get(norm(r["name"]), 0), "seed": is_seed_category(r["name"])} for r in category_rows(db, biz)]
    departments = [{"name": n, "items": per_department.get(norm(n), 0)} for n in department_names(db, biz)]

    code = request.args.get("code", "").strip()
    start = known.get(code)
    want_cat = norm(start["category"] if start else request.args.get("category"))
    want_dept = norm(start["department"] if start else request.args.get("department"))
    # Restocking a known item (?code=) is always an inventory purchase; otherwise the entry point decides, and ?inventory=1/0 can override it
    asked = request.args.get("inventory")
    as_inventory = bool(start) or (asked == "1" if asked in ("0", "1") else origin == "inventory")
    return render_template(
        "inventory_new.html", origin=origin, as_inventory=as_inventory, can_list=can("transfer"), categories=categories, methods=PAYMENT_METHODS,
        known_suppliers=db.all("SELECT name, phone FROM suppliers WHERE business_id=? ORDER BY name LIMIT 1000", (biz,)), departments=departments, known=known, today=now().strftime("%Y-%m-%d"), units=unit_choices(db, biz),
        pre_category=next((c["name"] for c in categories if norm(c["name"]) == want_cat), ""),
        pre_department=next((d["name"] for d in departments if norm(d["name"]) == want_dept), ""),
        pre_code=code, pre_name=start["name"] if start else "",
    )


@app.get("/inventory/categories")
@need(None)
def categories_page():
    db, biz = get_db(), g.user["business_id"]
    totals = {norm(r["category"] or "General"): r for r in db.all(
        "SELECT category, COUNT(*) AS items, COALESCE(SUM(quantity), 0) AS units FROM products WHERE business_id=? GROUP BY category", (biz,))}
    tiles = [
        {"id": r["id"], "name": r["name"], "items": totals.get(norm(r["name"]), {}).get("items", 0), "units": totals.get(norm(r["name"]), {}).get("units", 0),
         "seed": is_seed_category(r["name"])}
        for r in category_rows(db, biz)
    ]
    return render_template("categories.html", tiles=tiles)


@app.post("/inventory/categories")
@need("procure")
def add_category():
    db, biz = get_db(), g.user["business_id"]
    from_wizard = text("next") == "new"

    def back(category=""):
        return redirect(url_for("inventory_new", category=category) if from_wizard else url_for("categories_page"))

    name = clean_name(text("category_name"))
    if not name:
        flash("Type a name for the new category.", "error")
        return back()
    existing = next((r["name"] for r in category_rows(db, biz) if norm(r["name"]) == norm(name)), None)
    if existing:
        flash(f"The category '{existing}' already exists." + (" It is selected for you." if from_wizard else ""), "ok" if from_wizard else "error")
        return back(existing)
    try:
        db.execute("INSERT INTO categories (business_id, name) VALUES (?, ?)", (biz, name))
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The category could not be added. Please try again.", "error")
        return back()
    note = " Items in it are counted in packs, so each purchase asks how many seeds a pack holds." if is_seed_category(name) else ""
    flash(f"Category '{name}' added.{note}", "ok")
    return back(name)


def retag(db, table, biz, old, new, column="category"):
    """Moves every row of a table from one name to another in a category or department column.

    The table and column come from the code, never from the request. Returns how many rows moved.
    """
    moved = 0
    for row in db.all(f"SELECT DISTINCT {column} AS value FROM {table} WHERE business_id=?", (biz,)):
        if row["value"] is not None and norm(row["value"]) == norm(old):
            moved += db.execute(f"UPDATE {table} SET {column}=? WHERE business_id=? AND {column}=?", (new, biz, row["value"])).rowcount
    return moved


def plan_merge(all_rows, ids, target, kind):
    """Works out merging several categories or departments into one name. Nothing is changed here.

    Returns (row that stays, rows that go, final name, None), or (None, None, None, what is wrong).
    The name given may be one of the ticked ones (that one stays) or a new one. It may not be an unticked
    one, because that would quietly swallow it. General is the default and can only be merged *into*.
    """
    wanted = {i for i in ids}
    chosen = [r for r in all_rows if r["id"] in wanted]
    if len(wanted) < 2:
        return None, None, None, f"Tick at least two {kind}s to merge."
    if len(chosen) != len(wanted):
        return None, None, None, f"Some of the ticked {kind}s no longer exist. Refresh the page and try again."
    target = clean_name(target)
    if not target:
        return None, None, None, f"Type the name for the merged {kind}."
    if any(r["name"] == "General" for r in chosen) and norm(target) != "general":
        return None, None, None, f"General is the default {kind} and cannot be renamed or removed. To merge into it, name the result General."
    clash = next((r for r in all_rows if r["id"] not in wanted and norm(r["name"]) == norm(target)), None)
    if clash:
        return None, None, None, f"There is already a {kind} called '{clash['name']}'. Tick it as well to merge into it."
    keep = next((r for r in chosen if norm(r["name"]) == norm(target)), chosen[0])
    final = keep["name"] if norm(keep["name"]) == norm(target) else target
    return keep, [r for r in chosen if r is not keep], final, None


def read_ids():
    return [int(value) for value in request.form.getlist("ids") if value.isdigit()]


@app.post("/inventory/categories/merge")
@need("admin")
def merge_categories():
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("categories_page"))
    keep, others, final, problem = plan_merge(category_rows(db, biz), read_ids(), text("category_name"), "category")
    if problem:
        flash(problem, "error")
        return back
    # A name with the word seed is what makes a category seeds, counted in packs, so seed and other categories do not mix
    if len({is_seed_category(r["name"]) for r in [keep] + others} | {is_seed_category(final)}) > 1:
        flash("Seed categories (counted in packs) and other categories cannot be merged together, and the new name must match: use the word seed in the name only if all of them are seed categories.", "error")
        return back

    try:
        for r in others:
            for table in ("products", "procurements", "pos_products"):
                retag(db, table, biz, r["name"], final)
            db.execute("DELETE FROM categories WHERE id=? AND business_id=?", (r["id"], biz))
        if keep["name"] != final:
            db.execute("UPDATE categories SET name=? WHERE id=? AND business_id=?", (final, keep["id"], biz))
            for table in ("products", "procurements", "pos_products"):
                retag(db, table, biz, keep["name"], final)
        items = db.one("SELECT COUNT(*) AS n FROM products WHERE business_id=? AND category=?", (biz, final))["n"]
        purchases = db.one("SELECT COUNT(*) AS n FROM procurements WHERE business_id=? AND category=?", (biz, final))["n"]
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The merge could not be completed, so nothing was changed. Please try again.", "error")
        return back

    names = ", ".join(f"'{r['name']}'" for r in [keep] + others)
    flash(f"Merged {names} into '{final}'. It now holds {items} item(s) and {purchases} purchase record(s); the others were removed.", "ok")
    return redirect(url_for("categories_page"))


@app.post("/inventory/categories/<int:category_id>/rename")
@need("admin")
def rename_category(category_id):
    db, biz = get_db(), g.user["business_id"]
    row = db.one("SELECT id, name FROM categories WHERE id=? AND business_id=?", (category_id, biz))
    if row is None:
        abort(404)
    new = clean_name(text("category_name"))
    clash = next((r for r in category_rows(db, biz) if norm(r["name"]) == norm(new) and r["id"] != category_id), None)
    if not new:
        flash("Type the new name.", "error")
    elif row["name"] == "General":
        flash("The General category cannot be renamed.", "error")
    elif clash:
        flash(f"There is already a category called '{clash['name']}'.", "error")
    elif is_seed_category(new) != is_seed_category(row["name"]):
        # A name with the word seed is what makes a category seeds, counted in packs
        flash("That rename would change whether this category counts as seeds (counted in packs), because the word seed in a name is what marks it. Keep or remove the word seed in both names.", "error")
    else:
        try:
            db.execute("UPDATE categories SET name=? WHERE id=? AND business_id=?", (new, category_id, biz))
            for table in ("products", "procurements", "pos_products"):
                retag(db, table, biz, row["name"], new)
            db.commit()
        except DB_ERRORS:
            db.rollback()
            flash("The category could not be renamed. Please try again.", "error")
        else:
            flash(f"'{row['name']}' is now '{new}'. Its items and past purchases moved with it.", "ok")
    return redirect(url_for("categories_page"))


@app.post("/inventory/categories/<int:category_id>/delete")
@need("admin")
def delete_category(category_id):
    db, biz = get_db(), g.user["business_id"]
    row = db.one("SELECT id, name FROM categories WHERE id=? AND business_id=?", (category_id, biz))
    if row is None:
        abort(404)
    in_use = db.one("SELECT COUNT(*) AS n FROM products WHERE business_id=? AND category=?", (biz, row["name"]))["n"]
    if row["name"] == "General":
        flash("The General category cannot be deleted.", "error")
    elif in_use:
        flash(f"'{row['name']}' still has {in_use} item(s). Move them to another category first, then delete it.", "error")
    else:
        db.execute("DELETE FROM categories WHERE id=? AND business_id=?", (category_id, biz))
        db.commit()
        flash(f"Category '{row['name']}' deleted.", "ok")
    return redirect(url_for("categories_page"))


# --- INVENTORY ANALYSIS: what was bought, and what it cost, for any category, department, item and period ---
def analysis_rows(db, biz, start, end):
    """Every purchase in the period, each tagged with the item's *current* category and department.

    Using the item's own category means a purchase logged before an item was recategorised still lands with it;
    purchases of items that have since been deleted keep the category they were logged under.
    """
    sql = """SELECT pr.id, pr.item_code, pr.name, pr.purchase_date, pr.quantity, pr.unit_cost, pr.total_cost, pr.amount_paid, pr.supplier_name, pr.invoice_no,
                    COALESCE(p.category, pr.category) AS category, COALESCE(p.department, pr.department) AS department,
                    p.seeds_per_pack AS seeds_per_pack, p.quantity AS in_stock, p.unit AS unit
             FROM procurements pr LEFT JOIN products p ON p.business_id = pr.business_id AND p.item_code = pr.item_code
             WHERE pr.business_id=? AND COALESCE(pr.in_inventory, 1) = 1"""
    params = [biz]
    if start is not None:
        sql += " AND pr.purchase_date >= ?"
        params.append(start.isoformat())
    if end is not None:
        sql += " AND pr.purchase_date < ?"
        params.append((end + datetime.timedelta(days=1)).isoformat())
    rows = db.all(sql, params)
    for r in rows:
        r["category"] = clean_name(r["category"]) or "General"
        r["department"] = clean_name(r["department"]) or "General"
        r["quantity"] = r["quantity"] or 0
        r["total_cost"] = r["total_cost"] or 0.0
        r["amount_paid"] = r["amount_paid"] if r["amount_paid"] is not None else r["total_cost"]
        r["status"] = payment_status_for(r["total_cost"], r["amount_paid"])
    return rows


def analysis_context():
    db, biz = get_db(), g.user["business_id"]
    choice, start, end = report_period("all")
    search = request.args.get("q", "").strip()
    rows = [r for r in analysis_rows(db, biz, start, end) if not search or norm(search) in norm(r["name"]) or norm(search) in norm(r["item_code"])]
    facet = facets(db, biz, rows, cost_measure)
    chosen = [r for r in rows if facet["match"](r)]

    items = {}
    for r in sorted(chosen, key=lambda r: (r["purchase_date"] or "", r["id"])):
        it = items.setdefault(r["item_code"], {
            "code": r["item_code"], "purchases": 0, "qty": 0, "cost": 0.0, "paid": 0.0, "last": "", "seeds_per_pack": r["seeds_per_pack"],
        })
        # Rows run oldest first, so the name, place and stock left over are those of the latest purchase
        it.update(name=r["name"], category=r["category"], department=r["department"], in_stock=r["in_stock"], last=r["purchase_date"] or "", unit=r["unit"] or "")
        it["purchases"] += 1
        it["qty"] += r["quantity"]
        it["cost"] += r["total_cost"]
        it["paid"] += r["amount_paid"]
    item_list = sorted(items.values(), key=lambda i: (-i["cost"], i["name"].lower()))
    for it in item_list:
        it["avg"] = it["cost"] / it["qty"] if it["qty"] else 0.0

    totals = {
        "items": len(item_list), "purchases": len(chosen), "qty": sum(i["qty"] for i in item_list),
        "cost": sum(i["cost"] for i in item_list), "paid": sum(i["paid"] for i in item_list),
    }
    totals["owed"] = totals["cost"] - totals["paid"]
    return {
        "facet": facet, "mode": "analysis", "search": search, "periods": REPORT_PERIODS, "period": choice, "start": start, "end": end,
        "items": item_list, "totals": totals, "has_items": True,
        "history": sorted(chosen, key=lambda r: (r["purchase_date"] or "", r["id"]), reverse=True)[:300], "history_total": len(chosen),
    }


@app.get("/inventory/analysis")
@need(None)
def inventory_analysis():
    return render_template("inventory_analysis.html", **analysis_context())


@app.get("/inventory/analysis.csv")
@need(None)
def inventory_analysis_csv():
    ctx = analysis_context()
    return csv_response(
        "inventory-analysis.csv",
        ["Item Code", "Item", "Category", "Department", "Unit", "Purchases", "Quantity Bought", "Total Cost", "Average Unit Cost", "Last Purchase", "In Stock Now"],
        [(i["code"], i["name"], i["category"], i["department"], i["unit"], i["purchases"], i["qty"], round(i["cost"], 2), round(i["avg"], 4), i["last"], i["in_stock"])
         for i in ctx["items"]],
    )


# What an inventory item is bought for. Farm use stays in inventory only; what is for sale is listed on the POS.
ITEM_PURPOSES = {"farm": "Farm use", "sale": "For sale", "both": "For sale and farm use"}


def read_purchase_purpose(qty):
    """What the purchase form says the stock is for: (purpose, how many go on the POS for sale, None), or (None, 0, what is wrong).
    A form that does not say (an older page) is (None, 0, None): everything goes into inventory, as before."""
    purpose = text("purpose")
    if purpose not in ITEM_PURPOSES:
        return None, 0, None
    if purpose == "farm":
        return "farm", 0, None
    if not can("transfer"):
        return None, 0, "Your account cannot put stock on the POS. Choose farm use, or ask an admin to log what is for sale."
    if purpose == "sale":
        return "sale", qty, None
    try:
        sale_qty = number("sale_qty", int)
    except ValueError:
        return None, 0, "Say how many of them are for sale (a whole number); the rest are kept for farm use."
    if not 0 <= sale_qty <= qty:
        return None, 0, f"The number for sale must be between 0 and the {qty:,} bought."
    return "both", sale_qty, None


@app.post("/inventory/purchase")
@need("procure")
def purchase():
    db, biz = get_db(), g.user["business_id"]
    code, name = text("item_code"), text("name")
    # Every purchase is logged in Procurements. The box says whether it is also an inventory item. The form sends a 0 before the
    # tick, so the last value wins; a post with no such field at all (an older client) counts as an inventory purchase.
    ticks = request.form.getlist("in_inventory")
    inventory = not (ticks and ticks[-1] == "0")
    origin = text("origin") if text("origin") in ("inventory", "procurements") else "inventory"
    # A problem sends the user back to the form with the category and department they had picked
    again = url_for(
        "inventory_new" if origin == "inventory" else "procurement_new", category=text("category"), department=text("department"),
        inventory="1" if inventory else "0",
    )
    try:
        qty = number("quantity", int)
        unit_cost = number("unit_cost")
        price = number("price") if inventory and text("price") else None
    except ValueError:
        flash("Quantity must be a whole number; unit cost and selling price must be numbers.", "error")
        return redirect(again)
    if not name or (inventory and not code) or qty <= 0 or unit_cost < 0 or (price is not None and price < 0):
        flash(
            ("Item code and name are required" if inventory else "A name is required") + ", quantity must be above zero and prices cannot be negative.", "error"
        )
        return redirect(again)
    # What an inventory item was bought for: farm use (inventory only), for sale (straight on the POS), or both, split by quantity
    purpose, sale_qty, problem = read_purchase_purpose(qty) if inventory else (None, 0, None)
    if problem:
        flash(problem, "error")
        return redirect(again)
    if not inventory and code and db.one("SELECT 1 AS x FROM products WHERE business_id=? AND item_code=?", (biz, code)):
        flash(f"'{code}' is an item in inventory. Tick 'This is also an inventory item' to add this purchase to its stock, or use a different code.", "error")
        return redirect(again)
    try:
        amount_paid = amount_for_status(text("payment_status"), text("amount_paid"), round(qty * unit_cost, 2))
    except ValueError as e:
        flash(str(e).capitalize() + ".", "error")
        return redirect(again)
    # How it was paid goes to Accounts. Paid to a named supplier it is also kept on their statement, and then it needs its
    # reference number (for cash, the receipt number); with no supplier the reference is optional.
    method, reference = NOT_RECORDED, ""
    if amount_paid > 0.005:
        method, reference, problem = read_payment_details(strict=False) if text("supplier_name") else read_money_method(required=False)
        if problem:
            flash(problem, "error")
            return redirect(again)

    known = db.one("SELECT category, department, seeds_per_pack, price FROM products WHERE business_id=? AND item_code=?", (biz, code)) if inventory else None
    try:
        # An item already in stock keeps its own category and department; a new one is filed where the form says.
        # A category that is new is added to the list, but only once the whole purchase is saved.
        category = known["category"] if known else resolve_category(db, biz, text("category"))
        department = known["department"] if known else chosen_department(db, biz)

        # Seed items must say how many seeds a pack holds. Something that is not an inventory item has no packs, unit or selling price.
        seeds_per_pack = None
        if inventory:
            seeds_per_pack, problem = seeds_per_pack_for(known, category, text("seeds_per_pack"))
            if problem:
                flash(problem[0].upper() + problem[1:] + ".", "error")
                return redirect(again)
            if is_seed_category(category) and sale_qty:
                flash("Seed packs are not sold as they are: they are planted, and the seedlings are what goes on the POS. Choose farm use for seeds.", "error")
                return redirect(again)
            if is_seed_category(category):
                purpose = "farm"
            if sale_qty and price is None and not (known and known["price"]):
                flash("Enter the selling price: what is for sale goes on the POS at that price.", "error")
                return redirect(again)

        existing_name = record_purchase(db, biz, {
            "in_inventory": inventory, "amount_paid": amount_paid, "seeds_per_pack": seeds_per_pack, "purpose": purpose, "sale_qty": sale_qty,
            "unit": (SEED_UNIT if seeds_per_pack else resolve_unit(db, biz, text("unit"))) if inventory else "",
            "item_code": code, "name": name, "category": category, "department": department,
            "quantity": qty, "unit_cost": unit_cost, "price": price, "supplier_name": text("supplier_name"), "supplier_phone": text("supplier_phone"),
            "payment_method": method, "payment_reference": reference,
            "invoice_no": text("invoice_no"), "purchase_date": text("purchase_date") or now().strftime("%Y-%m-%d"),
            "batch_no": text("batch_no") if inventory else "", "date_planted": text("date_planted") if inventory else "", "expiry_date": text("expiry_date"),
        })
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The purchase could not be saved. Please try again.", "error")
        return redirect(again)

    seed_note = f" That is {qty * seeds_per_pack:,} seeds in {qty:,} pack(s) of {seeds_per_pack:,}." if seeds_per_pack else ""
    if not inventory:
        flash(f"Purchase logged for '{name}' in Procurements. It was not added to inventory.", "ok")
    elif sale_qty:
        kept = qty - sale_qty
        flash(f"Purchase logged for '{existing_name or name}' (code {code}): {sale_qty:,} for sale, now on the POS"
              + (f", and {kept:,} for farm use, in inventory only." if kept else ". None was kept for farm use.") + " It is recorded in Procurements too.", "ok")
    elif existing_name:
        flash(f"Added {qty} unit(s) to the existing stock of '{existing_name}' (code {code}). It is logged in Procurements too.{seed_note}", "ok")
    else:
        flash(f"Purchase logged for '{name}', added to inventory and recorded in Procurements.{seed_note}", "ok")
    # Back to where the user started, unless that page would not show what they just logged
    return redirect(url_for("inventory" if inventory and origin == "inventory" else "procurements"))


def record_purchase(db, biz, p):
    """Logs one purchase in the procurement log and, when it is an inventory item, adds it to stock too. Shared by the purchase form and the CSV import.

    p["in_inventory"] (default True) says whether the purchase is also an inventory item; if not, only the procurement record is made.
    A blank price (None) keeps an existing item's selling price and uses the unit cost for a new item.
    Returns the existing item's name when the code was already in stock, otherwise None. The caller commits.
    """
    inventory = p.get("in_inventory", True)
    existing = db.one("SELECT id, name, price, category, department, unit, purpose, batch_no, date_planted FROM products WHERE business_id=? AND item_code=?", (biz, p["item_code"])) if inventory else None
    # What is for sale goes straight on the POS; only the rest is kept in inventory (farm use). The item remembers what it is bought
    # for; one bought both ways over time is 'both'. A purchase that does not say (an import, the assistant) changes neither.
    sale_qty = min(max(int(p.get("sale_qty") or 0), 0), p["quantity"]) if inventory else 0
    kept_qty = p["quantity"] - sale_qty
    purpose = p.get("purpose") if inventory and p.get("purpose") in ITEM_PURPOSES else None
    if existing and purpose and existing["purpose"] and existing["purpose"] != purpose:
        purpose = "both"
    if existing:
        # A restock is filed where the item already lives, so analysis by category and department stays consistent.
        # It is counted in the item's own unit too; only an item with no unit yet takes the one given.
        p = dict(p, category=existing["category"], department=existing["department"])
        unit = existing["unit"] or p.get("unit") or ""
        price = existing["price"] if p["price"] is None else p["price"]
        db.execute(
            """UPDATE products SET quantity = quantity + ?, unit_cost=?, price=?, batch_no=?, supplier_name=?, invoice_no=?, purchase_date=?, expiry_date=?,
                                  seeds_per_pack = COALESCE(?, seeds_per_pack), unit=?, purpose = COALESCE(?, purpose)
               WHERE id=? AND business_id=?""",
            (kept_qty, p["unit_cost"], price, p["batch_no"], p["supplier_name"], p["invoice_no"], p["purchase_date"], p["expiry_date"],
             p.get("seeds_per_pack"), unit, purpose, existing["id"], biz),
        )
    elif inventory:
        price = p["unit_cost"] if p["price"] is None else p["price"]
        db.execute(
            """INSERT INTO products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, unit_cost, price, supplier_name, invoice_no, purchase_date, expiry_date, seeds_per_pack, unit, purpose)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, p["item_code"], p["name"], p["category"], p["department"], p["batch_no"], p["date_planted"], kept_qty,
                p["unit_cost"], price, p["supplier_name"], p["invoice_no"], p["purchase_date"], p["expiry_date"], p.get("seeds_per_pack"), p.get("unit") or "", purpose,
            ),
        )
    total_cost = round(p["quantity"] * p["unit_cost"], 2)
    paid_now = total_cost if p.get("amount_paid") is None else p["amount_paid"]
    # The supplier named on the purchase gets an account (or the existing one is used), so the purchase lands on their statement
    supplier_id = supplier_for(db, biz, p["supplier_name"], p.get("supplier_phone"), p["purchase_date"] or now().strftime("%Y-%m-%d"))
    procurement_id = db.insert(
        """INSERT INTO procurements (business_id, item_code, name, category, department, supplier_name, invoice_no, purchase_date, quantity, unit_cost, total_cost, expiry_date, amount_paid, in_inventory, supplier_id, sale_qty)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            biz, p["item_code"], existing["name"] if existing else p["name"], p["category"], p["department"], p["supplier_name"],
            p["invoice_no"], p["purchase_date"], p["quantity"], p["unit_cost"], total_cost, p["expiry_date"],
            paid_now, 1 if inventory else 0, supplier_id, sale_qty if inventory and (sale_qty or purpose) else None,
        ),
    )
    if supplier_id and paid_now > 0.005:
        # What was paid when the purchase was logged is a payment to the supplier too, with its method and reference
        add_supplier_payment(
            db, biz, supplier_id, paid_now, p.get("payment_method") or NOT_RECORDED, p.get("payment_reference") or "", p["purchase_date"],
            "Paid when the purchase was logged", [(procurement_id, paid_now)], apply=False,
        )
    elif paid_now > 0.005:
        # Bought with no supplier account: the money still left the business
        record_money(
            db, biz, "out", paid_now, p.get("payment_method") or NOT_RECORDED, "purchase", p.get("payment_reference") or "", "",
            f"{existing['name'] if existing else p['name']} x {p['quantity']:,}", "purchase", procurement_id, stamp_for_day(p["purchase_date"]),
        )
    if inventory:
        db.execute("UPDATE pos_products SET price=? WHERE business_id=? AND item_code=?", (price, biz, p["item_code"]))
    if sale_qty:
        # The part for sale is listed on the POS under the item's own code, as a transfer from inventory would list it
        listed = db.execute("UPDATE pos_products SET quantity = quantity + ? WHERE business_id=? AND item_code=?", (sale_qty, biz, p["item_code"])).rowcount
        if not listed:
            db.execute(
                """INSERT INTO pos_products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (biz, p["item_code"], existing["name"] if existing else p["name"], p["category"], p["department"], p["batch_no"],
                 (existing["date_planted"] if existing else p["date_planted"]) or "", sale_qty, price),
            )
    requests_arrived(db, biz, existing["name"] if existing else p["name"], p["item_code"] if inventory else "", f"{p['quantity']:,} bought on {p['purchase_date']}")
    return existing["name"] if existing else None


@app.get("/inventory/<int:item_id>")
@need(None)
def item(item_id):
    db, biz = get_db(), g.user["business_id"]
    current = get_item(db, item_id)

    # Greenhouse details for seed items: what the seedlings are listed as now, the cost of one seed, past sowings
    seed = is_seed_category(current["category"])
    listing, batches, per_seed, loose, plantings = None, [], None, 0, []
    if seed:
        loose = db.one("SELECT loose_seeds FROM products WHERE id=? AND business_id=?", (item_id, biz))["loose_seeds"] or 0
        plantings = db.all(
            "SELECT id, name, seeds_planted, planted_on, ready_on, actual_ready_on FROM plantings WHERE business_id=? AND seed_item_id=? ORDER BY id DESC LIMIT 10",
            (biz, item_id),
        )
        size = current["seeds_per_pack"]
        per_seed = current["unit_cost"] / size if size else None
        listing = db.one(
            "SELECT name, price, quantity FROM pos_products WHERE business_id=? AND item_code=?", (biz, current["item_code"] + SEEDLING_SUFFIX)
        )
        batches = db.all(
            """SELECT created_at, packs, seeds_sown, germination_pct, seed_unit_cost, seedlings, cost_per_seedling, selling_price, created_by
               FROM seedling_batches WHERE business_id=? AND seed_code=? ORDER BY id DESC LIMIT 20""",
            (biz, current["item_code"]),
        )

    # The category list for the edit form; an item's own category is always offered, even if it was never added to the list
    category_choices = [r["name"] for r in category_rows(db, biz)]
    if norm(current["category"]) not in {norm(n) for n in category_choices}:
        category_choices.append(current["category"] or "General")

    return render_template(
        "item.html", item=current, purposes=ITEM_PURPOSES, departments=department_names(db, biz), category_choices=category_choices, units=unit_choices(db, biz),
        access={action: item_access_state(db, item_id, action) for action, _ in ITEM_ACTIONS}, actions=ITEM_ACTIONS,
        seed=seed, listing=listing, batches=batches, per_seed=per_seed, loose=loose, plantings=plantings, seedling_name=(listing or {}).get("name") or seedling_name_for(current["name"]),
    )


@app.post("/inventory/<int:item_id>/update")
@need(None)
def update_item(item_id):
    db, biz = get_db(), g.user["business_id"]
    current = get_item(db, item_id)
    grant = item_grant(db, item_id, "procure")
    if grant is None:
        return refuse_item_action(item_id)
    name = text("name")
    try:
        unit_cost = number("unit_cost")
        price = number("price", default=unit_cost)
    except ValueError:
        flash("Unit cost and selling price must be numbers.", "error")
        return redirect(url_for("item", item_id=item_id))
    if not name or unit_cost < 0 or price < 0:
        flash("Item name is required and prices cannot be negative.", "error")
        return redirect(url_for("item", item_id=item_id))

    # A field that was not sent stays as it is, so an edit never clears what it was not asked to change
    category = resolve_category(db, biz, text("category")) if "category" in request.form else current["category"]
    dept = chosen_department(db, biz) if "department" in request.form else current["department"]

    # A seed item always carries its pack size; here an admin can correct a wrong one
    seeds_per_pack = None
    if is_seed_category(category):
        try:
            seeds_per_pack = parse_whole(text("seeds_per_pack")) if text("seeds_per_pack") else current["seeds_per_pack"]
            if not seeds_per_pack or seeds_per_pack < 1:
                raise ValueError
        except ValueError:
            flash("A seed item needs the number of seeds in one pack (a whole number above zero).", "error")
            return redirect(url_for("item", item_id=item_id))

    # The packaging unit. Seed items are always counted in packs; otherwise it is whatever was typed (blank = none set)
    if is_seed_category(category):
        unit = SEED_UNIT
    elif "unit" in request.form:
        unit = resolve_unit(db, biz, text("unit"))
    else:
        unit = current["unit"] or ""

    # What the item is for; left as it is when the form does not send it. Seeds are always for the farm (they are planted)
    purpose = current["purpose"]
    if is_seed_category(category):
        purpose = "farm"
    elif "purpose" in request.form:
        purpose = text("purpose") if text("purpose") in ITEM_PURPOSES else None

    re_filed = norm(category) != norm(current["category"]) or norm(dept) != norm(current["department"])

    # Item code and quantity are not editable: stock only moves through purchases, transfers and sales
    try:
        db.execute(
            """UPDATE products SET name=?, category=?, department=?, batch_no=?, date_planted=?, unit_cost=?, price=?, supplier_name=?, invoice_no=?, purchase_date=?, expiry_date=?,
                                  seeds_per_pack = COALESCE(?, seeds_per_pack), unit=?, purpose=?
               WHERE id=? AND business_id=?""",
            (
                name, category, dept, text("batch_no"), text("date_planted"), unit_cost, price, text("supplier_name"),
                text("invoice_no"), text("purchase_date"), text("expiry_date"), seeds_per_pack, unit, purpose, item_id, biz,
            ),
        )
        db.execute(
            "UPDATE pos_products SET name=?, category=?, department=?, price=? WHERE business_id=? AND item_code=?",
            (name, category, dept, price, biz, current["item_code"]),
        )
        # Re-filing an item moves everything that belongs to it: the seedlings grown from it are listed in its department,
        # and its purchase history follows it so Procurements, Reports and the analysis all agree. Past sales stay where
        # they were made, because a sale is something that happened in that department at the time.
        db.execute("UPDATE pos_products SET department=? WHERE business_id=? AND item_code=?", (dept, biz, current["item_code"] + SEEDLING_SUFFIX))
        purchases = db.execute(
            "UPDATE procurements SET category=?, department=? WHERE business_id=? AND item_code=? AND COALESCE(in_inventory, 1) = 1",
            (category, dept, biz, current["item_code"]),
        ).rowcount
        if re_filed:
            # Planting records made from this seed, and the batches already on the POS, follow it too
            db.execute("UPDATE plantings SET department=? WHERE business_id=? AND seed_item_id=?", (dept, biz, item_id))
            db.execute(
                "UPDATE pos_products SET department=? WHERE business_id=? AND item_code IN (SELECT pos_code FROM plantings WHERE business_id=? AND seed_item_id=?)",
                (dept, biz, biz, item_id),
            )
        use_grant(db, grant)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The item could not be updated. Please try again.", "error")
        return redirect(url_for("item", item_id=item_id))

    moved = f" It is now filed under {category} / {dept}, with its {purchases} purchase record(s) and any listings." if re_filed else ""
    flash(f"Details saved for '{name}'.{moved}", "ok")
    return redirect(url_for("item", item_id=item_id))   # stay on the item that was just edited


@app.post("/inventory/<int:item_id>/adjust-quantity")
@need(None)
def adjust_quantity(item_id):
    """Corrects how much of an item is in stock by hand - a stock count, a loss, anything a purchase or transfer didn't capture.

    Always needs a note saying where the difference went (reduced) or came from (increased); kept on record either way.
    """
    db, biz = get_db(), g.user["business_id"]
    current = get_item(db, item_id)
    grant = item_grant(db, item_id, "procure")
    if grant is None:
        return refuse_item_action(item_id)
    back = redirect(url_for("item", item_id=item_id))

    try:
        new_qty = number("quantity", int)
        if new_qty < 0:
            raise ValueError
    except ValueError:
        flash("Enter the new quantity as a whole number, 0 or more.", "error")
        return back
    note = " ".join(text("note").split())[:300]
    old_qty = current["quantity"]
    if new_qty == old_qty:
        flash("That is already the quantity on record. Nothing was changed.", "ok")
        return back
    if len(note) < 5:
        flash("Say, in a few words, where the difference went (if reducing) or came from (if increasing). Nothing was changed.", "error")
        return back

    try:
        db.execute("UPDATE products SET quantity=? WHERE id=? AND business_id=?", (new_qty, item_id, biz))
        record_stock_adjustment(
            db, biz, "inventory_edit", current["item_code"], current["name"], current["department"], old_qty, new_qty,
            "Increase" if new_qty > old_qty else "Decrease", note, product_id=item_id,
        )
        low_stock_alert(db, current["name"], current["item_code"], old_qty, new_qty, "in inventory")
        use_grant(db, grant)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The quantity could not be saved. Please try again.", "error")
        return back

    change = f"increased by {new_qty - old_qty:,}" if new_qty > old_qty else f"reduced by {old_qty - new_qty:,}"
    flash(f"Quantity {change}, from {old_qty:,} to {new_qty:,}. Noted: {note}", "ok")
    return back


@app.post("/inventory/<int:item_id>/transfer")
@need(None)
def transfer(item_id):
    db, biz = get_db(), g.user["business_id"]
    p = get_item(db, item_id)
    grant = item_grant(db, item_id, "transfer")
    if grant is None:
        return refuse_item_action(item_id)
    destination = text("destination")
    try:
        qty = number("quantity", int)
    except ValueError:
        qty = 0
    if destination not in ("farm", "pos") or qty <= 0 or qty > p["quantity"]:
        flash(f"Choose a destination and a quantity between 1 and {p['quantity']}.", "error")
        return redirect(url_for("item", item_id=item_id))

    # Seed packs are not sold as they are: they are sown, and the seedlings that come up are what goes on sale
    sowing = None
    if destination == "pos" and is_seed_category(p["category"]):
        sowing, problem = read_sowing_form(p, qty)
        if problem:
            flash(problem, "error")
            return redirect(url_for("item", item_id=item_id))

    invoice_id = None
    try:
        moved = db.execute(
            "UPDATE products SET quantity = quantity - ? WHERE id=? AND business_id=? AND quantity >= ?",
            (qty, item_id, biz, qty),
        )
        if moved.rowcount != 1:
            raise StockError(p["name"])
        low_stock_alert(db, p["name"], p["item_code"], p["quantity"], p["quantity"] - qty, "in inventory")

        if destination == "farm":
            invoice_id = db.insert(
                """INSERT INTO invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at)
                   VALUES (?, ?, 'FARM_USE', ?, ?, 'INTERNAL', 0, 0, 0, ?, ?)""",
                (biz, next_invoice_no(db, biz), text("recipient") or "Farm Internal", text("phone"), g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            db.execute(
                """INSERT INTO invoice_items (invoice_id, business_id, item_code, item_name, department, batch_no, qty, unit_price, line_total)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (invoice_id, biz, p["item_code"], p["name"], p["department"], p["batch_no"], qty, p["price"]),
            )
        elif sowing:
            list_seedlings(db, biz, p, qty, sowing)
        else:
            listed = db.one("SELECT id FROM pos_products WHERE business_id=? AND item_code=?", (biz, p["item_code"]))
            if listed:
                db.execute("UPDATE pos_products SET quantity = quantity + ?, price=? WHERE id=?", (qty, p["price"], listed["id"]))
            else:
                db.execute(
                    """INSERT INTO pos_products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, price)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (biz, p["item_code"], p["name"], p["category"], p["department"], p["batch_no"], p["date_planted"], qty, p["price"]),
                )
        use_grant(db, grant)
        db.commit()
    except StockError:
        db.rollback()
        flash("The stock changed while you were working. Nothing was transferred; please try again.", "error")
        return redirect(url_for("item", item_id=item_id))
    except DB_ERRORS:
        db.rollback()
        flash("The transfer could not be saved. Please try again.", "error")
        return redirect(url_for("item", item_id=item_id))

    if invoice_id:
        return redirect(url_for("invoice", invoice_id=invoice_id))
    if sowing:
        flash(
            f"Sowed {qty:,} pack(s) = {sowing['seeds']:,} seeds. About {sowing['seedlings']:,} seedlings are now listed for sale as "
            f"'{sowing['name']}' at {money(sowing['price'])} each (cost {money_precise(sowing['cost_per_seedling'])} per seedling).",
            "ok",
        )
    else:
        flash(f"Transferred {qty} unit(s) of '{p['name']}' to Sales & POS.", "ok")
    return redirect(url_for("inventory"))


def read_sowing_form(item, packs):
    """Reads the greenhouse questions asked when seed packs are moved to the point of sale.

    Returns (details, None) or (None, what is wrong). Nothing is saved here.
    """
    size = item["seeds_per_pack"]
    if not size:
        # A seed item bought before pack sizes were recorded: ask for it now
        try:
            size = number("seeds_per_pack", int)
            if size < 1:
                raise ValueError
        except ValueError:
            return None, "Say how many seeds are in one pack (a whole number above zero)."
    try:
        germination = number("germination_pct")
        if not 0 < germination <= 100:
            raise ValueError
    except ValueError:
        return None, "Enter the estimated germination rate as a percentage above 0 and up to 100."
    try:
        seed_cost = number("seed_cost")
        if seed_cost < 0:
            raise ValueError
    except ValueError:
        return None, "Enter the cost of one seed (0 or more)."

    plan = seedling_plan(packs, size, germination, seed_cost)
    if plan["seedlings"] < 1:
        return None, "At that germination rate these seeds would give less than one seedling."
    try:
        # Left blank, the seedlings are priced at what they cost to raise, rounded up to the next cent
        price = number("seedling_price") if text("seedling_price") else plan["break_even_price"]
        if price < 0:
            raise ValueError
    except ValueError:
        return None, "The selling price per seedling must be a number, 0 or more."
    return dict(
        plan, seeds_per_pack=size, germination=germination, seed_cost=seed_cost, price=round(price, 2),
        name=text("seedling_name")[:100] or seedling_name_for(item["name"]),
    ), None


def list_seedlings(db, biz, item, packs, s):
    """Puts the seedlings expected from a sowing on sale and logs the batch. The caller commits."""
    pos_code = item["item_code"] + SEEDLING_SUFFIX
    sown_on = now().strftime("%Y-%m-%d")
    listed = db.one("SELECT id FROM pos_products WHERE business_id=? AND item_code=?", (biz, pos_code))
    if listed:
        db.execute(
            "UPDATE pos_products SET quantity = quantity + ?, price=?, name=?, department=?, batch_no=?, date_planted=? WHERE id=?",
            (s["seedlings"], s["price"], s["name"], item["department"], item["batch_no"], sown_on, listed["id"]),
        )
    else:
        db.execute(
            """INSERT INTO pos_products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, price)
               VALUES (?, ?, ?, 'Seedlings', ?, ?, ?, ?, ?)""",
            (biz, pos_code, s["name"], item["department"], item["batch_no"], sown_on, s["seedlings"], s["price"]),
        )
    if not item["seeds_per_pack"]:
        db.execute("UPDATE products SET seeds_per_pack=? WHERE id=? AND business_id=?", (s["seeds_per_pack"], item["id"], biz))
    db.execute(
        """INSERT INTO seedling_batches (business_id, seed_item_id, seed_code, seed_name, pos_code, seedling_name, packs, seeds_per_pack, seeds_sown,
                                         germination_pct, seed_unit_cost, seedlings, cost_per_seedling, selling_price, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            biz, item["id"], item["item_code"], item["name"], pos_code, s["name"], packs, s["seeds_per_pack"], s["seeds"],
            s["germination"], s["seed_cost"], s["seedlings"], s["cost_per_seedling"], s["price"], g.user["username"],
            now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )


@app.post("/inventory/<int:item_id>/delete")
@need(None)
def delete_item(item_id):
    db = get_db()
    p = get_item(db, item_id)
    grant = item_grant(db, item_id, "delete")
    if grant is None:
        return refuse_item_action(item_id)
    db.execute("DELETE FROM products WHERE id=? AND business_id=?", (item_id, g.user["business_id"]))
    use_grant(db, grant)
    db.commit()
    flash(f"'{p['name']}' was deleted from inventory.", "ok")
    return redirect(url_for("inventory"))


# --- SALES & INVOICES HISTORY ---
INVOICE_PERIODS = (
    ("all", "All dates"), ("today", "Today"), ("yesterday", "Yesterday"), ("last_7", "Last 7 days"),
    ("this_week", "This week"), ("last_week", "Last week"), ("this_month", "This month"), ("last_month", "Last month"),
    ("this_year", "This year"), ("last_year", "Last year"), ("custom", "Custom range"),
)
HISTORY_PARAMS = ("q", "period", "from", "to", "collect")


def parse_day(value):
    try:
        return datetime.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def invoice_period(default="all"):
    """The date filter on Sales & Invoices: (choice, first day, last day) from the query string.

    Either day can be None, meaning no limit on that side (both None for all dates). Weeks run Monday to Sunday.
    """
    today = now().date()
    day = datetime.timedelta(days=1)
    choice = request.args.get("period", default)
    start = end = None
    if choice == "today":
        start = end = today
    elif choice == "yesterday":
        start = end = today - day
    elif choice == "last_7":
        start, end = today - 6 * day, today
    elif choice == "this_week":
        start = today - today.weekday() * day
        end = start + 6 * day
    elif choice == "last_week":
        end = today - (today.weekday() + 1) * day
        start = end - 6 * day
    elif choice == "this_month":
        start = today.replace(day=1)
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    elif choice == "last_month":
        end = today.replace(day=1) - day
        start = end.replace(day=1)
    elif choice == "this_year":
        start, end = today.replace(month=1, day=1), today.replace(month=12, day=31)
    elif choice == "last_year":
        start, end = datetime.date(today.year - 1, 1, 1), datetime.date(today.year - 1, 12, 31)
    elif choice == "custom":
        # One date alone is an open-ended range: everything from that day, or everything up to it
        start, end = parse_day(request.args.get("from")), parse_day(request.args.get("to"))
        if start and end and end < start:
            start, end = end, start
    else:
        choice = "all"
    return choice, start, end


def history_filter(biz, q, start, end, waiting=False):
    """The WHERE clause shared by the invoice list, its totals and its CSV export, so all three always agree. waiting keeps only the
    sales still to be collected."""
    where, params = "business_id=?", [biz]
    if waiting:
        where += " AND kind='SALE' AND collect_on IS NOT NULL AND collected_at IS NULL AND COALESCE(payment_status, '') <> 'Cancelled'"
    if q:
        like = f"%{q.lower()}%"
        where += """ AND (LOWER(invoice_no) LIKE ? OR LOWER(customer_name) LIKE ? OR LOWER(customer_phone) LIKE ?
                     OR EXISTS (SELECT 1 FROM invoice_items ii WHERE ii.invoice_id = invoices.id AND LOWER(ii.item_name) LIKE ?))"""
        params += [like, like, like, like]
    # created_at is stored as 'YYYY-MM-DD HH:MM:SS' text, so whole days compare as text, the same way the reports do
    if start:
        where += " AND created_at >= ?"
        params.append(start.isoformat())
    if end:
        where += " AND created_at < ?"
        params.append((end + datetime.timedelta(days=1)).isoformat())
    return where, params


def history_rows(db, where, params, limit, waiting=False):
    """The invoices, newest first; the ones waiting to be collected soonest collection first."""
    order = "collect_on, id" if waiting else "id DESC"
    sql = f"""SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_at, collect_on, collected_at,
                     deliver_on, delivered_at
              FROM invoices WHERE {where} ORDER BY {order} LIMIT ?"""
    return db.all(sql, params + [limit])


def history_totals(db, where, params):
    """How many invoices match, and the money on them. Cancelled invoices and farm use are not sales, so their
    amounts are left out of the money (the same rule as the reports), though they are still counted and listed."""
    counted = "COALESCE(payment_status, '') <> 'Cancelled' AND COALESCE(kind, 'SALE') <> 'FARM_USE'"
    return db.one(
        f"""SELECT COUNT(*) AS n,
                   COALESCE(SUM(CASE WHEN {counted} THEN total ELSE 0 END), 0) AS total,
                   COALESCE(SUM(CASE WHEN {counted} THEN amount_paid ELSE 0 END), 0) AS paid,
                   COALESCE(SUM(CASE WHEN {counted} THEN balance_due ELSE 0 END), 0) AS owed
            FROM invoices WHERE {where}""",
        params,
    )


@app.get("/history")
@need(None)
def history():
    db = get_db()
    q = request.args.get("q", "").strip()
    period, start, end = invoice_period()
    waiting = request.args.get("collect") == "waiting"
    where, params = history_filter(g.user["business_id"], q, start, end, waiting)
    csv_args = {key: request.args.get(key) for key in HISTORY_PARAMS if request.args.get(key)}
    return render_template(
        "history.html", invoices=history_rows(db, where, params, 500, waiting), totals=history_totals(db, where, params),
        q=q, periods=INVOICE_PERIODS, period=period, start=start, end=end, csv_url=url_for("history_csv", **csv_args), waiting=waiting,
        waiting_count=db.one(f"SELECT COUNT(*) AS n {WAITING_SQL}", (g.user["business_id"],))["n"], today=today_str(),
        picking=request.args.get("pick") == "delivery",
    )


@app.get("/history.csv")
@need(None)
def history_csv():
    q = request.args.get("q", "").strip()
    _, start, end = invoice_period()
    waiting = request.args.get("collect") == "waiting"
    rows = history_rows(get_db(), *history_filter(g.user["business_id"], q, start, end, waiting), 50000, waiting)
    return csv_response(
        "sales-invoices.csv",
        ["Invoice No", "Date/Time", "Type", "Customer", "Phone", "Total", "Paid", "Balance", "Status", "To Be Collected On", "Collected At"],
        [
            (r["invoice_no"], r["created_at"], "Farm use" if r["kind"] == "FARM_USE" else "Sale", r["customer_name"],
             r["customer_phone"], r["total"], r["amount_paid"], r["balance_due"], r["payment_status"], r["collect_on"] or "", r["collected_at"] or "")
            for r in rows
        ],
    )



# --- Who bought a product ---
# Every product name in the app opens the list of the customers who bought it (tray.js shows it in a panel over the page; on its own
# it is a full page, which is also where a batch's "Sold" tile on its planting record leads). A product is found by its code, as on
# the invoice lines; a line with no code (old imported sales) by its name.
ITEM_SALES_SHOWN = 300


@app.template_global()
def sold_url(code, name):
    return url_for("item_sales", code=code) if code else url_for("item_sales", name=name or "")


@app.template_global()
def sold_link(code, name, label=None):
    """A product name that opens who bought it."""
    return Markup('<a class="sold-link" href="{}" data-tray data-no-prefetch>{}</a>').format(sold_url(code, name), label if label is not None else name)


def item_sales_data(db, biz, code, name, limit=ITEM_SALES_SHOWN):
    where = "ii.business_id=? AND i.kind='SALE' AND i.payment_status <> 'Cancelled'"
    params = [biz]
    if code:
        where += " AND ii.item_code=?"
        params.append(code)
    else:
        where += " AND (ii.item_code IS NULL OR ii.item_code='') AND ii.item_name=?"
        params.append(name)
    summary = db.one(
        f"""SELECT COUNT(*) AS lines, COUNT(DISTINCT ii.invoice_id) AS invoices, COALESCE(SUM(ii.qty), 0) AS units,
                   COALESCE(SUM(ii.line_total), 0) AS value, COALESCE(SUM(COALESCE(ii.discount, 0)), 0) AS discount,
                   COUNT(DISTINCT COALESCE(CAST(i.customer_id AS TEXT), i.customer_name)) AS customers, MIN(i.created_at) AS first, MAX(i.created_at) AS last
            FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id WHERE {where}""",
        params,
    )
    rows = db.all(
        f"""SELECT i.id AS invoice_id, i.invoice_no, i.created_at, i.customer_id, i.customer_name, i.customer_phone, i.payment_status,
                   ii.item_code, ii.item_name, ii.qty, ii.unit_price, COALESCE(ii.discount, 0) AS discount, ii.line_total
            FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id WHERE {where} ORDER BY i.created_at DESC, ii.id DESC LIMIT ?""",
        params + [limit],
    )
    # What it is called now: the POS listing, the inventory item or the batch with this code, else the name it was last sold under
    title = name
    if code:
        known = db.one(
            """SELECT name FROM pos_products WHERE business_id=? AND item_code=?
               UNION ALL SELECT name FROM products WHERE business_id=? AND item_code=?""",
            (biz, code, biz, code),
        )
        title = known["name"] if known else (rows[0]["item_name"] if rows else code)
    batch = db.one("SELECT id, name, batch_no, planted_on FROM plantings WHERE business_id=? AND pos_code=?", (biz, code)) if code else None
    item = db.one("SELECT id FROM products WHERE business_id=? AND item_code=?", (biz, code)) if code else None
    return {"code": code, "name": name, "title": planting_label(batch) if batch else title, "summary": summary, "rows": rows, "shown": len(rows),
            "batch": batch, "item": item, "customer_links": g.user["role"] == "admin"}


@app.get("/sales/item")
@need(None)
def item_sales():
    db, biz = get_db(), g.user["business_id"]
    code, name = request.args.get("code", "").strip(), request.args.get("name", "").strip()
    if not code and not name:
        abort(404)
    data = item_sales_data(db, biz, code, name)
    csv_url = url_for("item_sales_csv", **({"code": code} if code else {"name": name}))
    # tray.js asks for the panel's contents only; anything else gets the full page
    if request.headers.get("X-Tray") == "1":
        return render_template("_item_sales.html", s=data, csv_url=csv_url, in_tray=True)
    return render_template("item_sales.html", s=data, csv_url=csv_url, in_tray=False)


@app.get("/sales/item.csv")
@need(None)
def item_sales_csv():
    db, biz = get_db(), g.user["business_id"]
    code, name = request.args.get("code", "").strip(), request.args.get("name", "").strip()
    data = item_sales_data(db, biz, code, name, limit=100000)
    filename = "sales-of-" + (re.sub(r"[^A-Za-z0-9]+", "-", data["title"] or "item").strip("-") or "item") + ".csv"
    return csv_response(
        filename, ["Date", "Invoice No", "Customer", "Phone", "Item Code", "Item", "Quantity", "Unit Price", "Discount", "Total", "Payment Status"],
        [(r["created_at"], r["invoice_no"], r["customer_name"], r["customer_phone"], r["item_code"], r["item_name"], r["qty"], r["unit_price"], r["discount"],
          r["line_total"], r["payment_status"]) for r in data["rows"]],
    )

@app.get("/invoices/<int:invoice_id>")
@need(None)
def invoice(invoice_id):
    db, biz = get_db(), g.user["business_id"]
    inv = db.one(
        """SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at, customer_id,
                  cancelled_at, cancelled_by, cancel_reason, collect_on, collected_at, collected_by, deliver_on, delivered_at, delivered_by, offline_ref
           FROM invoices WHERE id=? AND business_id=?""",
        (invoice_id, biz),
    )
    if inv is None:
        abort(404)
    inv["waiting"] = bool(inv["collect_on"] and not inv["collected_at"] and inv["payment_status"] != "Cancelled")
    inv["collect_when"] = when_words(inv["collect_on"], today_str(), before="due ") if inv["waiting"] else ""
    inv["to_deliver"] = bool(inv["deliver_on"] and not inv["delivered_at"] and inv["payment_status"] != "Cancelled")
    inv["deliver_when"] = when_words(inv["deliver_on"], today_str(), before="due ") if inv["to_deliver"] else ""
    lines = db.all(
        "SELECT item_code, item_name, qty, unit_price, COALESCE(discount, 0) AS discount, line_total FROM invoice_items WHERE invoice_id=? AND business_id=? ORDER BY id",
        (invoice_id, biz),
    )
    inv["discount"] = round(sum(line["discount"] for line in lines), 2)
    inv["subtotal"] = round(sum(line["qty"] * line["unit_price"] for line in lines), 2)
    twins = phone_twins(db, biz, inv["customer_id"]) if inv["customer_id"] and g.user["role"] == "admin" else []
    # An operator needs an admin's one-time approval; an admin only needs to confirm with their password
    asked = None if g.user["role"] == "admin" else active_request(db, invoice_id, DELETE_INVOICE)
    if asked and asked["status"] == "approved":
        asked = {**asked, "until": (asked["expires_at"] or "")[11:16]}
    return render_template(
        "invoice.html", inv=inv, lines=lines, payments=payment_history(db, "sale", invoice_id), twins=twins, asked=asked,
        stock_to=("inventory" if inv["kind"] == "FARM_USE" else "the POS"), methods=PAYMENT_METHODS, today=today_str(),
        paid_by=money_lines(db, biz, "invoice", invoice_id),
    )


def invoice_stock_table(kind):
    """Where an invoice's items came from, and so where they go back to: farm use drew on inventory, a sale on the POS."""
    return "products" if kind == "FARM_USE" else "pos_products"


@app.post("/invoices/<int:invoice_id>/request-deletion")
@need(None)
def request_invoice_deletion(invoice_id):
    db, biz = get_db(), g.user["business_id"]
    inv = db.one("SELECT id, invoice_no, customer_name, total FROM invoices WHERE id=? AND business_id=?", (invoice_id, biz))
    if inv is None:
        abort(404)
    back = redirect(url_for("invoice", invoice_id=invoice_id))
    if g.user["role"] == "admin":
        flash("Admins delete invoices directly, with their password.", "ok")
        return back
    if active_request(db, invoice_id, DELETE_INVOICE):
        flash("You already have a request to delete this invoice waiting or approved.", "ok")
        return back
    reason = " ".join(text("reason").split())[:300]
    if len(reason) < 5:
        flash("Say why this invoice should be deleted (at least a few words). The admin reads it before answering.", "error")
        return back

    request_id = db.insert(
        """INSERT INTO permission_requests (business_id, user_id, username, action, item_id, item_name, reason, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (biz, g.user["id"], g.user["username"], DELETE_INVOICE, invoice_id, inv["invoice_no"], reason, now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    if alert_enabled("permission"):
        notify(
            db, "permission", f"Permission request from {g.user['username']}",
            f"{g.user['username']} asks to delete invoice {inv['invoice_no']} ({inv['customer_name']}, {money(inv['total'])}). Reason: {reason}. Open the app to approve or deny.",
            url_for("permission_requests") + f"#request-{request_id}", whatsapp=True,
        )
    db.commit()
    flash("Your request has been sent to the admin. You will get a popup here when they answer.", "ok")
    return back


@app.post("/invoices/<int:invoice_id>/delete")
@need(None)
def delete_invoice(invoice_id):
    db, biz = get_db(), g.user["business_id"]
    inv = db.one(
        """SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at, customer_id
           FROM invoices WHERE id=? AND business_id=?""",
        (invoice_id, biz),
    )
    if inv is None:
        abort(404)
    back = redirect(url_for("invoice", invoice_id=invoice_id))

    grant = None
    if g.user["role"] == "admin":
        reason = " ".join(text("reason").split())[:300]
        if len(reason) < 5:
            flash("Say why the invoice is being deleted (at least a few words). It is kept in the deletion records. Nothing was deleted.", "error")
            return back
        me = db.one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
        if not me or not check_password_hash(me["password_hash"], request.form.get("password", "")):
            flash("That password is not correct. Nothing was deleted.", "error")
            return back
        approved_by = g.user["username"]
    else:
        # The operator's own words are what was approved, so they are what gets recorded
        row = active_request(db, invoice_id, DELETE_INVOICE)
        if not row or row["status"] != "approved":
            flash("An admin has to approve deleting this invoice first. Use the request button.", "error")
            return back
        grant, reason, approved_by = row, row["reason"], row["decided_by"] or ""

    lines = db.all(
        "SELECT item_code, item_name, department, batch_no, qty, unit_price, COALESCE(discount, 0) AS discount, line_total FROM invoice_items WHERE invoice_id=? AND business_id=? ORDER BY id",
        (invoice_id, biz),
    )
    payments = db.all(
        "SELECT old_paid, new_paid, note, changed_by, changed_at FROM payment_log WHERE business_id=? AND kind='sale' AND ref_id=? ORDER BY id",
        (biz, invoice_id),
    )

    # Everything happens in one transaction: either the invoice is gone and recorded, or nothing changed
    try:
        table = invoice_stock_table(inv["kind"])
        missing = []
        if request.form.get("return_stock"):
            for line in lines:
                back_in = db.execute(
                    f"UPDATE {table} SET quantity = quantity + ? WHERE business_id=? AND item_code=?", (line["qty"], biz, line["item_code"]),
                ).rowcount
                if back_in != 1:
                    missing.append(line["item_code"])
            where = "inventory" if table == "products" else "POS stock"
            stock_note = f"Items returned to {where}." if not missing else f"Items returned to {where}, except {', '.join(missing)} (no longer listed)."
        else:
            stock_note = "Items were not returned to stock."

        db.execute("DELETE FROM invoice_items WHERE invoice_id=? AND business_id=?", (invoice_id, biz))
        db.execute("DELETE FROM payment_log WHERE business_id=? AND kind='sale' AND ref_id=?", (biz, invoice_id))
        # A deleted invoice was a mistake, so the money recorded against it goes from Accounts too (its payments stay in the deletion record).
        # Part of what it had been paid may have come from a lump sum on the customer's account, which Accounts holds against the account,
        # not the invoice: that share comes off as a correction on the account, for the admin to assign to the balance it is taken from.
        if inv["kind"] == "SALE" and inv["customer_id"]:
            on_invoice = db.one(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM cashbook WHERE business_id=? AND source='invoice' AND source_id=? AND direction='in'",
                (biz, invoice_id),
            )["total"]
            if inv["amount_paid"] - on_invoice > 0.005:
                record_money(
                    db, biz, "in", -round(inv["amount_paid"] - on_invoice, 2), CORRECTION, "customer_payment", "", inv["customer_name"],
                    f"Invoice {inv['invoice_no']} deleted: its share of payments on account", "customer", inv["customer_id"],
                )
        db.execute("DELETE FROM cashbook WHERE business_id=? AND source='invoice' AND source_id=?", (biz, invoice_id))
        db.execute("DELETE FROM invoices WHERE id=? AND business_id=?", (invoice_id, biz))
        db.insert(
            """INSERT INTO deleted_invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due,
                                             created_by, created_at, items_json, payments_json, stock_note, reason, deleted_by, approved_by, request_id, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, inv["invoice_no"], inv["kind"], inv["customer_name"], inv["customer_phone"], inv["payment_status"], inv["total"],
                inv["amount_paid"], inv["balance_due"], inv["created_by"], inv["created_at"], json.dumps(lines), json.dumps(payments),
                stock_note, reason, g.user["username"], approved_by, grant["id"] if grant else None, now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        use_grant(db, grant)
        if grant:
            notify(
                db, "deletion", f"Invoice {inv['invoice_no']} was deleted",
                f"{g.user['username']} deleted invoice {inv['invoice_no']} ({money(inv['total'])}) with your approval. Reason: {reason}.",
                url_for("deleted_invoices"),
            )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The invoice could not be deleted, so nothing was changed. Please try again.", "error")
        return back

    flash(f"Invoice {inv['invoice_no']} was deleted and recorded in the deletion records. {stock_note}", "ok")
    return redirect(url_for("history"))


@app.post("/invoices/<int:invoice_id>/cancel")
@need("admin")
def cancel_invoice(invoice_id):
    """Cancels a sale without erasing it: it stays visible, with its original amounts, but owes nothing and is left
    out of revenue, receivables and reports from here on. Admin only, always - no operator request/approval path.
    """
    db, biz = get_db(), g.user["business_id"]
    inv = db.one(
        "SELECT id, invoice_no, kind, customer_id, customer_name, payment_status, total, amount_paid FROM invoices WHERE id=? AND business_id=? AND kind='SALE'",
        (invoice_id, biz),
    )
    if inv is None:
        abort(404)
    back = redirect(url_for("customer_statement", customer_id=inv["customer_id"]) if text("back") == "statement" and inv["customer_id"] else url_for("invoice", invoice_id=invoice_id))
    if inv["payment_status"] == "Cancelled":
        flash("That invoice has already been cancelled.", "ok")
        return back

    reason = " ".join(text("reason").split())[:300]
    if len(reason) < 5:
        flash("Say why the invoice is being cancelled (at least a few words). Nothing was changed.", "error")
        return back
    # Cancelling moves no money by itself. What the customer had paid is refunded only when that is ticked, and then
    # Accounts records it as money out, with how it was paid back.
    refund = round(inv["amount_paid"], 2) if request.form.get("refund") and inv["amount_paid"] > 0.005 else 0.0
    method, reference = NOT_RECORDED, ""
    if refund:
        method, reference, problem = read_money_method()
        if problem or not text("payment_method"):
            flash(("Choose how the refund was paid." if not text("payment_method") else problem) + " Nothing was changed.", "error")
            return back

    lines = db.all("SELECT item_code, qty FROM invoice_items WHERE invoice_id=? AND business_id=?", (invoice_id, biz))
    try:
        stock_note = ""
        if request.form.get("return_stock"):
            table = invoice_stock_table(inv["kind"])
            missing = []
            for line in lines:
                back_in = db.execute(f"UPDATE {table} SET quantity = quantity + ? WHERE business_id=? AND item_code=?", (line["qty"], biz, line["item_code"])).rowcount
                if back_in != 1:
                    missing.append(line["item_code"])
            stock_note = "Items returned to the POS." if not missing else f"Items returned to the POS, except {', '.join(missing)} (no longer listed)."
        db.execute(
            "UPDATE invoices SET payment_status='Cancelled', balance_due=0, cancelled_at=?, cancelled_by=?, cancel_reason=? WHERE id=? AND business_id=?",
            (now().strftime("%Y-%m-%d %H:%M:%S"), g.user["username"], reason, invoice_id, biz),
        )
        record_money(
            db, biz, "out", refund, method, "refund", reference, inv["customer_name"], f"Refund on cancelled invoice {inv['invoice_no']}", "invoice", invoice_id,
        )
        notify(
            db, "cancellation", f"Invoice {inv['invoice_no']} was cancelled",
            f"{g.user['username']} cancelled invoice {inv['invoice_no']} ({inv['customer_name']}, {money(inv['total'])}). Reason: {reason}."
            + (f" {money(refund)} was refunded by {method.lower()}." if refund else ""),
            url_for("invoice", invoice_id=invoice_id),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The invoice could not be cancelled. Please try again.", "error")
        return back

    refund_note = f" {money(refund)} was refunded by {method.lower()} and recorded in Accounts." if refund else ""
    flash(f"Invoice {inv['invoice_no']} was cancelled. It stays on record but owes nothing and no longer counts toward sales.{refund_note} {stock_note}".strip(), "ok")
    return back


@app.get("/records/deleted-invoices")
@need("admin")
def deleted_invoices():
    rows = get_db().all(
        """SELECT id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at,
                  items_json, payments_json, stock_note, reason, deleted_by, approved_by, deleted_at
           FROM deleted_invoices WHERE business_id=? ORDER BY id DESC LIMIT 500""",
        (g.user["business_id"],),
    )
    for r in rows:
        r["lines"] = json.loads(r["items_json"] or "[]")
        r["payments"] = json.loads(r["payments_json"] or "[]")
    return render_template("deleted_invoices.html", rows=rows, total=sum(r["total"] for r in rows))


@app.post("/invoices/<int:invoice_id>/payment")
@need("admin")
def invoice_payment(invoice_id):
    db, biz = get_db(), g.user["business_id"]
    inv = db.one(
        "SELECT invoice_no, customer_name, total, amount_paid, payment_status FROM invoices WHERE id=? AND business_id=? AND kind='SALE'", (invoice_id, biz)
    )
    if inv is None:
        abort(404)
    if inv["payment_status"] == "Cancelled":
        flash("This invoice is cancelled, so its payment cannot be changed.", "error")
        return redirect(url_for("invoice", invoice_id=invoice_id))
    try:
        new_paid = update_payment(db, "sale", invoice_id, inv["total"], inv["amount_paid"])
    except ValueError:
        db.rollback()
        flash(f"Enter an amount between 0 and the invoice total ({money(inv['total'])}).", "error")
        return redirect(url_for("invoice", invoice_id=invoice_id))

    # More paid is money received, with how it came in; less paid is taken back out of the account it is said to come from
    change = round(new_paid - inv["amount_paid"], 2)
    method, reference = CORRECTION, ""
    if abs(change) > 0.005:
        method, reference, problem = read_money_method() if change > 0 else read_correction_method()
        if problem:
            db.rollback()
            flash(problem, "error")
            return redirect(url_for("invoice", invoice_id=invoice_id))
    db.execute(
        "UPDATE invoices SET amount_paid=?, balance_due=?, payment_status=? WHERE id=? AND business_id=?",
        (new_paid, round(inv["total"] - new_paid, 2), payment_status_for(inv["total"], new_paid), invoice_id, biz),
    )
    record_money(
        db, biz, "in", change, method, "customer_payment", reference, inv["customer_name"],
        f"Invoice {inv['invoice_no']}" + (f": {text('note')[:120]}" if text("note") else ""), "invoice", invoice_id,
    )
    db.commit()
    flash("Payment updated.", "ok")
    return redirect(url_for("invoice", invoice_id=invoice_id))


# --- PROCUREMENTS ---
PROCUREMENT_COLUMNS = "id, purchase_date, supplier_name, invoice_no, item_code, name, category, department, quantity, unit_cost, total_cost, expiry_date, amount_paid, COALESCE(in_inventory, 1) AS in_inventory, supplier_id, sale_qty"
PROCUREMENT_KINDS = (("", "All purchases"), ("inventory", "Inventory items"), ("expense", "Not in inventory"))


def with_payment_state(row):
    row["balance"] = round((row["total_cost"] or 0.0) - (row["amount_paid"] or 0.0), 2)
    row["status"] = payment_status_for(row["total_cost"] or 0.0, row["amount_paid"] or 0.0)
    return row


def procurement_rows(db, biz, category, kind=""):
    sql = f"SELECT {PROCUREMENT_COLUMNS} FROM procurements WHERE business_id=?"
    params = [biz]
    if category:
        sql += " AND category=?"
        params.append(category)
    if kind == "inventory":
        sql += " AND COALESCE(in_inventory, 1) = 1"
    elif kind == "expense":
        sql += " AND COALESCE(in_inventory, 1) = 0"
    return [with_payment_state(row) for row in db.all(sql + " ORDER BY id DESC", params)]


def get_procurement(db, purchase_id):
    row = db.one(f"SELECT {PROCUREMENT_COLUMNS} FROM procurements WHERE id=? AND business_id=?", (purchase_id, g.user["business_id"]))
    if row is None:
        abort(404)
    return with_payment_state(row)


@app.get("/procurements/<int:purchase_id>")
@need("admin")
def procurement(purchase_id):
    db = get_db()
    p = get_procurement(db, purchase_id)
    return render_template(
        "purchase.html", p=p, payments=payment_history(db, "purchase", purchase_id), methods=PAYMENT_METHODS, ref_required=bool(p["supplier_id"]),
        supplier=db.one("SELECT id, name FROM suppliers WHERE id=? AND business_id=?", (p["supplier_id"], g.user["business_id"])) if p["supplier_id"] else None,
    )


@app.post("/procurements/<int:purchase_id>/payment")
@need("admin")
def procurement_payment(purchase_id):
    db = get_db()
    p = get_procurement(db, purchase_id)
    here = redirect(url_for("procurement", purchase_id=purchase_id))
    if p["supplier_id"]:
        # A purchase from a supplier on file: the change is a payment to that supplier, kept with how it was paid
        try:
            new_paid = p["total_cost"] if request.form.get("mark_paid") else round(number("amount_paid"), 2)
            if not 0 <= new_paid <= p["total_cost"] + 0.005:
                raise ValueError("amount_paid")
            new_paid = min(new_paid, p["total_cost"])
        except ValueError:
            flash(f"Enter an amount between 0 and the purchase total ({money(p['total_cost'])}).", "error")
            return here
        change = round(new_paid - p["amount_paid"], 2)
        if abs(change) < 0.005:
            flash("The amount paid is unchanged.", "ok")
            return here
        paid_on = today_str()
        method, reference, problem = read_payment_details(strict=False) if change > 0 else read_correction_method()
        if problem:
            flash(problem, "error")
            return here
        try:
            add_supplier_payment(db, g.user["business_id"], p["supplier_id"], change, method, reference, paid_on, text("note")[:150], [(purchase_id, change)])
            db.commit()
        except DB_ERRORS:
            db.rollback()
            flash("The payment could not be saved. Please try again.", "error")
            return here
        flash("Payment updated." if change > 0 else "Payment corrected.", "ok")
        return here

    try:
        new_paid = update_payment(db, "purchase", purchase_id, p["total_cost"], p["amount_paid"])
    except ValueError:
        db.rollback()
        flash(f"Enter an amount between 0 and the purchase total ({money(p['total_cost'])}).", "error")
        return redirect(url_for("procurement", purchase_id=purchase_id))

    # No supplier account: the payment goes to Accounts on its own, with how it was paid (or, lowered, the account it goes back to)
    change = round(new_paid - p["amount_paid"], 2)
    method, reference = CORRECTION, ""
    if abs(change) > 0.005:
        method, reference, problem = read_money_method() if change > 0 else read_correction_method()
        if problem:
            db.rollback()
            flash(problem, "error")
            return here
    db.execute("UPDATE procurements SET amount_paid=? WHERE id=? AND business_id=?", (new_paid, purchase_id, g.user["business_id"]))
    record_money(
        db, g.user["business_id"], "out", change, method, "purchase", reference, p["supplier_name"] or "",
        f"{p['name']} x {p['quantity']:,}" + (f": {text('note')[:120]}" if text("note") else ""), "purchase", purchase_id,
    )
    db.commit()
    flash("Payment updated.", "ok")
    return redirect(url_for("procurement", purchase_id=purchase_id))


@app.get("/procurements")
@need(None)
def procurements():
    db, biz = get_db(), g.user["business_id"]
    category = request.args.get("category", "").strip()
    kind = request.args.get("kind", "") if request.args.get("kind") in ("inventory", "expense") else ""
    rows = procurement_rows(db, biz, category, kind)
    by_category = {}
    for r in rows:
        label = r["category"] or "Uncategorised"
        by_category[label] = by_category.get(label, 0.0) + (r["total_cost"] or 0.0)
    categories = [c["category"] for c in db.all("SELECT DISTINCT category FROM procurements WHERE business_id=? ORDER BY category", (biz,))]
    return render_template(
        "procurements.html", rows=rows, category=category, categories=categories, kind=kind, kinds=PROCUREMENT_KINDS,
        by_category=sorted(by_category.items()), grand_total=sum(by_category.values()),
        total_paid=sum(r["amount_paid"] or 0.0 for r in rows), total_owed=sum(r["balance"] for r in rows),
    )


@app.get("/procurements.csv")
@need(None)
def procurements_csv():
    kind = request.args.get("kind", "") if request.args.get("kind") in ("inventory", "expense") else ""
    rows = procurement_rows(get_db(), g.user["business_id"], request.args.get("category", "").strip(), kind)
    return csv_response(
        "procurements.csv",
        ["Date", "Supplier", "Invoice No", "Item Code", "Product Name", "Category", "Dept", "Qty", "Unit Cost", "Total Cost", "Expiry", "Amount Paid", "Balance", "Payment Status", "In Inventory"],
        [
            (r["purchase_date"], r["supplier_name"], r["invoice_no"], r["item_code"], r["name"], r["category"], r["department"],
             r["quantity"], r["unit_cost"], r["total_cost"], r["expiry_date"], r["amount_paid"], r["balance"], r["status"], "Yes" if r["in_inventory"] else "No")
            for r in rows
        ],
    )


# --- HR & PAYROLL (ADMIN) ---
def hr_settings_for(db, biz):
    saved = db.one(
        f"SELECT non_working_days, {', '.join(STATUTORY_RATES)}, contribution_base, holes_per_tray, tray_rate FROM hr_settings WHERE business_id=?", (biz,)
    )
    rules = dict(HR_DEFAULTS)
    rules.update({key: value for key, value in (saved or {}).items() if value is not None})
    return rules


def statutory_amounts(rules, calculated_basic, bonuses_total):
    """The employee and employer share of NAPSA and NHIMA for one payslip. Every new payslip goes through here."""
    base = calculated_basic + (bonuses_total if rules["contribution_base"] == "gross" else 0.0)
    return {rate[:-4]: round(base * rules[rate] / 100.0, 2) for rate in STATUTORY_RATES}


def expected_work_days(year, month, non_working):
    days_in_month = calendar.monthrange(year, month)[1]
    return sum(1 for day in range(1, days_in_month + 1) if WEEKDAYS[datetime.date(year, month, day).weekday()] not in non_working)


def named_amounts(prefix):
    """Reads the repeated name/amount rows of the payslip form into {name: amount}."""
    result = {}
    for name, raw in zip(request.form.getlist(prefix + "_name"), request.form.getlist(prefix + "_amount")):
        name, raw = name.strip(), raw.strip().replace(",", "")
        if not name and not raw:
            continue
        amount = float(raw or 0)
        if not name or not math.isfinite(amount) or amount < 0:
            raise ValueError(prefix)
        if amount:
            result[name] = round(result.get(name, 0.0) + amount, 2)
    return result


@app.get("/hr")
@need("admin")
def hr():
    db, biz = get_db(), g.user["business_id"]
    hr_set = hr_settings_for(db, biz)
    employees = db.all(f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE business_id=? ORDER BY full_name", (biz,))
    payslips = db.all(
        """SELECT id, pay_month, pay_date, period_start, period_end, employee_name, basic_salary, expected_days, worked_days, trays, napsa_amt, nhima_amt, net_payable
           FROM payroll WHERE business_id=? ORDER BY id DESC LIMIT 500""",
        (biz,),
    )
    summary = db.all(
        """SELECT pay_month, COUNT(id) AS slips, COALESCE(SUM(calculated_basic), 0) AS basic,
                  COALESCE(SUM(napsa_amt), 0) AS emp_napsa, COALESCE(SUM(nhima_amt), 0) AS emp_nhima,
                  COALESCE(SUM(employer_napsa), 0) AS empr_napsa, COALESCE(SUM(employer_nhima), 0) AS empr_nhima,
                  COALESCE(SUM(net_payable), 0) AS net
           FROM payroll WHERE business_id=? GROUP BY pay_month ORDER BY pay_month DESC""",
        (biz,),
    )
    # What each person is owed at their next pay date (or today, when no pay date is set): soonest first, people with no date last
    today = today_str()
    due = []
    for e in employees:
        e["rate"] = rate_for(e, hr_set)
        e["schedule_words"] = schedule_words(e)
        p = pay_preview(db, biz, e, hr_set, e["next_pay_date"] or today)
        due.append(dict(p, employee=e, when=when_words(e["next_pay_date"], today, before="due ") if e["next_pay_date"] else "",
                        late=bool(e["next_pay_date"]) and e["next_pay_date"] < today))
    due.sort(key=lambda d: (d["employee"]["next_pay_date"] or "9999-99-99", d["employee"]["full_name"].lower()))
    return render_template(
        "hr.html", employees=employees, payslips=payslips, summary=summary, today=today, due=due, tray_rate=hr_set["tray_rate"],
        schedules=PAY_SCHEDULES, bases=PAY_BASES, e=None, reminders_on=alert_enabled("payroll"),
    )


def read_employee_form(hr_set, current=None):
    """The employee form: (values, None), or (None, what is wrong). A casual worker is paid by the tray at their own rate (blank = the
    business's rate); the form sends no salary for them, so one already on record is kept (it lets them still be paid for days worked).
    Everyone else is paid a salary for each pay period. A monthly schedule with no next pay date set
    takes the next time its day of the month comes round."""
    name, nrc = clean_name(text("full_name"), 100), text("nrc_number")[:40]
    kind = text("employment_type") if text("employment_type") in ("Permanent", "Casual Worker") else "Permanent"
    try:
        age = number("age", int, default=0) or None
        days_week = number("expected_days_week", int, default=5)
        if kind == "Casual Worker" and not text("basic_salary"):
            salary = (current or {}).get("basic_salary") or 0.0
        else:
            salary = number("basic_salary", default=0.0)
        rate = number("tray_rate") if kind == "Casual Worker" and text("tray_rate") else None
        pay_day = number("pay_day", int) if text("pay_day") else None
    except ValueError:
        return None, "Salary, rate per tray, age, days per week and the day of the month must be numbers."
    if not name or not nrc:
        return None, "Full name and NRC number are required."
    if salary < 0 or (rate is not None and not 0 <= rate <= 10_000_000) or not 1 <= days_week <= 7:
        return None, "Salary and the rate per tray cannot be negative, and days per week must be 1 to 7."
    schedule = text("pay_schedule") if text("pay_schedule") in dict(PAY_SCHEDULES) else "manual"
    next_day = day_or_none(text("next_pay_date")) if text("next_pay_date") else None
    if text("next_pay_date") and next_day is None:
        return None, "The next pay date is not a date."
    if schedule == "monthly":
        if pay_day is None and next_day:
            pay_day = int(next_day[8:])
        if pay_day is None or not 1 <= pay_day <= 31:
            return None, "Choose the day of the month this person is paid (1 to 31)."
        if next_day is None:
            next_day = month_day_on_or_after(today_str(), pay_day)
    elif schedule in ("weekly", "biweekly") and next_day is None:
        return None, "Choose the first pay date: the schedule counts on from it."
    return {
        "full_name": name, "nrc_number": nrc, "employment_type": kind, "basic_salary": round(salary, 2), "address": text("address")[:200], "age": age,
        "start_date": day_or_none(text("start_date")) or "", "expected_days_week": days_week,
        "pay_basis": "trays" if kind == "Casual Worker" else None, "tray_rate": round(rate, 2) if rate is not None else None,
        "pay_schedule": schedule, "pay_day": pay_day if schedule == "monthly" else None, "next_pay_date": next_day,
        "plants": 1 if checked("plants", default=kind == "Casual Worker") else 0,
    }, None


EMPLOYEE_FIELDS = ("full_name", "nrc_number", "employment_type", "basic_salary", "address", "age", "start_date", "expected_days_week",
                   "pay_basis", "tray_rate", "pay_schedule", "pay_day", "next_pay_date", "plants")


@app.post("/hr/employees")
@need("admin")
def add_employee():
    db, biz = get_db(), g.user["business_id"]
    values, problem = read_employee_form(hr_settings_for(db, biz))
    if problem:
        flash(problem + " Nothing was saved.", "error")
        return redirect(url_for("hr"))
    db.insert(f"INSERT INTO employees (business_id, {', '.join(EMPLOYEE_FIELDS)}) VALUES (?, {', '.join('?' for _ in EMPLOYEE_FIELDS)})",
              [biz] + [values[f] for f in EMPLOYEE_FIELDS])
    db.commit()
    flash(f"Employee '{values['full_name']}' registered" + (f", next paid on {values['next_pay_date']}." if values["next_pay_date"] else "."), "ok")
    return redirect(url_for("hr"))


def get_employee(db, employee_id):
    e = db.one(f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE id=? AND business_id=?", (employee_id, g.user["business_id"]))
    if e is None:
        abort(404)
    return e


@app.route("/hr/employees/<int:employee_id>/edit", methods=["GET", "POST"])
@need("admin")
def edit_employee(employee_id):
    db, biz = get_db(), g.user["business_id"]
    e = get_employee(db, employee_id)
    hr_set = hr_settings_for(db, biz)
    if request.method == "POST":
        values, problem = read_employee_form(hr_set, e)
        if problem:
            flash(problem + " Nothing was changed.", "error")
            return redirect(url_for("edit_employee", employee_id=employee_id))
        moved = values["next_pay_date"] != e["next_pay_date"]
        db.execute(f"UPDATE employees SET {'=?, '.join(EMPLOYEE_FIELDS)}=?{', pay_reminded_for=NULL' if moved else ''} WHERE id=? AND business_id=?",
                   [values[f] for f in EMPLOYEE_FIELDS] + [employee_id, biz])
        db.commit()
        flash(f"'{values['full_name']}' saved." + (" Unpaid trays are paid at the new rate on their next payslip." if values["tray_rate"] != e["tray_rate"] and e["employment_type"] == "Casual Worker" else ""), "ok")
        return redirect(url_for("hr"))
    today = today_str()
    absences = db.all("SELECT * FROM employee_absences WHERE business_id=? AND employee_id=? ORDER BY day DESC, id DESC LIMIT 200", (biz, employee_id))
    preview = pay_preview(db, biz, e, hr_set, e["next_pay_date"] or today)
    return render_template("employee_edit.html", e=e, absences=absences, preview=preview, schedules=PAY_SCHEDULES, bases=PAY_BASES,
                           tray_rate=hr_set["tray_rate"], today=today, rate=rate_for(e, hr_set), schedule=schedule_words(e))


@app.post("/hr/employees/<int:employee_id>/absences")
@need("admin")
def add_absence(employee_id):
    db, biz = get_db(), g.user["business_id"]
    e = get_employee(db, employee_id)
    back = redirect(url_for("edit_employee", employee_id=employee_id) + "#absences")
    day = day_or_none(text("day")) if text("day") else None
    days = 0.5 if text("days") == "0.5" else 1.0
    if day is None:
        flash("Choose the day they were absent.", "error")
        return back
    if db.one("SELECT id FROM employee_absences WHERE business_id=? AND employee_id=? AND day=?", (biz, employee_id, day)):
        flash(f"An absence on {day} is already recorded for {e['full_name']}.", "error")
        return back
    db.insert("INSERT INTO employee_absences (business_id, employee_id, day, days, reason, recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (biz, employee_id, day, days, text("reason")[:120], g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()
    flash(f"{e['full_name']}: {'half a day' if days == 0.5 else 'a day'} absent on {day} recorded. It comes off the salary for the pay period it falls in.", "ok")
    return back


@app.post("/hr/absences/<int:absence_id>/delete")
@need("admin")
def delete_absence(absence_id):
    db, biz = get_db(), g.user["business_id"]
    row = db.one("SELECT employee_id, day FROM employee_absences WHERE id=? AND business_id=?", (absence_id, biz))
    if row is None:
        abort(404)
    db.execute("DELETE FROM employee_absences WHERE id=? AND business_id=?", (absence_id, biz))
    db.commit()
    flash(f"The absence on {row['day']} was removed.", "ok")
    return redirect(url_for("edit_employee", employee_id=row["employee_id"]) + "#absences")


@app.post("/hr/employees/<int:employee_id>/delete")
@need("admin")
def delete_employee(employee_id):
    db = get_db()
    # Past payslips and logged activities keep the employee's name, so the history stays intact; they come off their plots
    db.execute("DELETE FROM employees WHERE id=? AND business_id=?", (employee_id, g.user["business_id"]))
    db.execute("DELETE FROM plot_workers WHERE employee_id=? AND business_id=?", (employee_id, g.user["business_id"]))
    db.commit()
    flash("Employee removed. Their past payslips are kept.", "ok")
    return redirect(url_for("hr"))


@app.route("/hr/settings", methods=["GET", "POST"])
@need("admin")
def hr_settings():
    db, biz = get_db(), g.user["business_id"]
    if request.method == "POST":
        non_working = ",".join(day for day in WEEKDAYS if request.form.get("day_" + day))
        base = text("contribution_base")
        try:
            rates = [number(rate) for rate in STATUTORY_RATES]
        except ValueError:
            rates = [-1.0]
        if not all(0 <= rate <= 100 for rate in rates) or base not in ("basic", "gross"):
            flash("Every rate must be a percentage between 0 and 100. Use 0 where a side pays nothing.", "error")
        elif len(non_working.split(",")) == 7:
            flash("At least one day of the week must be a working day.", "error")
        else:
            # napsa_pct, nhima_pct and the two bearer columns belong to the old single-rate rule; they are
            # still filled in because the table requires them, but nothing reads them any more
            values = (non_working, *rates, base, rates[0], rates[2], biz)
            if db.one("SELECT business_id FROM hr_settings WHERE business_id=?", (biz,)):
                db.execute(
                    f"""UPDATE hr_settings SET non_working_days=?, {'=?, '.join(STATUTORY_RATES)}=?, contribution_base=?,
                               napsa_pct=?, nhima_pct=?, napsa_bearer='employee', nhima_bearer='employee' WHERE business_id=?""",
                    values,
                )
            else:
                db.execute(
                    f"""INSERT INTO hr_settings (non_working_days, {', '.join(STATUTORY_RATES)}, contribution_base, napsa_pct, nhima_pct, business_id, napsa_bearer, nhima_bearer)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'employee', 'employee')""",
                    values,
                )
            db.commit()
            flash("Statutory rules saved. They apply to every payslip computed or imported from now on; existing payslips are unchanged.", "ok")
            return redirect(url_for("hr"))
        return redirect(url_for("hr_settings"))

    settings_row = hr_settings_for(db, biz)
    return render_template("hr_settings.html", s=settings_row, weekdays=WEEKDAYS, non_working=settings_row["non_working_days"].split(","), max_holes=MAX_TRAY_HOLES)


@app.post("/hr/settings/trays")
@need("admin")
def hr_tray_settings():
    """The usual holes in a seedling tray (filled into every new planting) and what a casual worker is paid for each tray planted."""
    db, biz = get_db(), g.user["business_id"]
    try:
        holes = number("holes_per_tray", int, default=0)
        rate = number("tray_rate", default=0.0)
    except ValueError:
        holes, rate = -1, -1.0
    if not 0 <= holes <= MAX_TRAY_HOLES or not 0 <= rate <= 10_000_000:
        flash(f"Holes per tray must be a whole number up to {MAX_TRAY_HOLES:,} (0 for no usual number), and the pay for each tray cannot be negative.", "error")
        return redirect(url_for("hr_settings"))
    rate = round(rate, 2)
    if db.one("SELECT business_id FROM hr_settings WHERE business_id=?", (biz,)):
        db.execute("UPDATE hr_settings SET holes_per_tray=?, tray_rate=? WHERE business_id=?", (holes, rate, biz))
    else:
        # The row also holds the statutory rules, so a business that has never saved those starts from the defaults
        d = HR_DEFAULTS
        db.execute(
            f"""INSERT INTO hr_settings (business_id, non_working_days, {', '.join(STATUTORY_RATES)}, contribution_base, napsa_pct, nhima_pct,
                                         napsa_bearer, nhima_bearer, holes_per_tray, tray_rate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'employee', 'employee', ?, ?)""",
            (biz, d["non_working_days"], *(d[r] for r in STATUTORY_RATES), d["contribution_base"], d["napsa_employee_pct"], d["nhima_employee_pct"], holes, rate),
        )
    db.commit()
    flash("Planting tray settings saved. A casual worker's unpaid trays are paid at this rate when their payslip is made; payslips already made are unchanged.", "ok")
    return redirect(url_for("hr_settings"))


def unpaid_trays(db, biz, employee_id, until):
    """The trays a worker planted, on plantings dated up to a day (or the end of a month, given as YYYY-MM), that no payslip has paid yet:
    ([{id, name, batch_no, planted_on, trays}], their total). Earlier unpaid trays are included, so nothing is missed."""
    if len(until) == 7:
        until += "-31"   # dates are YYYY-MM-DD text, so any day of the month sorts at or before the 31st
    rows = db.all(
        """SELECT t.id, t.trays, p.name, p.batch_no, p.planted_on
           FROM planting_trays t JOIN plantings p ON p.id = t.planting_id AND p.business_id = t.business_id
           WHERE t.business_id=? AND t.employee_id=? AND t.paid_payslip_id IS NULL AND p.planted_on <= ?
           ORDER BY p.planted_on, t.id""",
        (biz, employee_id, until),
    )
    return rows, round(sum(r["trays"] for r in rows), 2)


# --- How and when each employee is paid ---
# A casual worker is paid for the trays they planted, at their own rate (or the business's usual rate when they have none): all their
# unpaid trays at the rate in force when the payslip is made. Everyone else is paid their salary for each pay period, less days absent.
# Each person has a pay schedule and a next pay date; a payslip covers the days since the last one, and moves the next pay date on.
PAY_SCHEDULES = (("monthly", "Monthly, on a day of the month"), ("biweekly", "Every two weeks"), ("weekly", "Every week"), ("manual", "On a date set each time"))
PAY_BASES = (("trays", "Per tray planted"),)
EMPLOYEE_COLUMNS = ("id, full_name, nrc_number, employment_type, basic_salary, address, age, start_date, expected_days_week, pay_basis, tray_rate, "
                    "pay_schedule, pay_day, next_pay_date, paid_through, pay_reminded_for, plants")


def rate_for(employee, hr_set):
    """What a casual worker is paid for a tray: their own rate, or the business's usual one."""
    own = employee.get("tray_rate")
    return own if own is not None and own > 0 else (hr_set["tray_rate"] or 0.0)


def is_casual(employee):
    return employee.get("employment_type") == "Casual Worker"


# Who plants trays and so gets a tile on Planting Records: those ticked in HR. Someone never set either way (registered before the
# choice existed, or by an import) counts as a planter when they are a casual worker.
PLANTER_SQL = "COALESCE(plants, CASE WHEN employment_type='Casual Worker' THEN 1 ELSE 0 END) = 1"


@app.template_global()
def is_planter(employee):
    plants = employee.get("plants")
    return bool(plants) if plants is not None else is_casual(employee)


def month_day(year, month, day):
    """That day of a month, or its last day when the month is shorter (the 31st in April is the 30th)."""
    return datetime.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def month_day_on_or_after(day_str, pay_day):
    d = datetime.date.fromisoformat(day_str)
    candidate = month_day(d.year, d.month, pay_day)
    if candidate < d:
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        candidate = month_day(y, m, pay_day)
    return candidate.isoformat()


def shift_months(day_str, months, pay_day=None):
    d = datetime.date.fromisoformat(day_str)
    index = d.year * 12 + d.month - 1 + months
    return month_day(index // 12, index % 12 + 1, pay_day or d.day).isoformat()


def following_pay_date(employee, pay_date):
    """The pay date after this one on the person's schedule; None when dates are set by hand."""
    schedule = employee.get("pay_schedule") or "manual"
    if schedule == "monthly":
        return shift_months(pay_date, 1, employee.get("pay_day"))
    if schedule in ("weekly", "biweekly"):
        return add_days(pay_date, 7 if schedule == "weekly" else 14)
    return None


def pay_period(employee, pay_date):
    """The days a payslip on this pay date covers: from the day after the last payslip's period (or, before any payslip, one schedule
    step back), never before the person started, up to the pay date."""
    if employee.get("paid_through"):
        start = add_days(employee["paid_through"], 1)
    else:
        schedule = employee.get("pay_schedule") or "manual"
        if schedule == "monthly":
            start = add_days(shift_months(pay_date, -1, employee.get("pay_day")), 1)
        elif schedule in ("weekly", "biweekly"):
            start = add_days(pay_date, -(6 if schedule == "weekly" else 13))
        else:
            start = pay_date[:8] + "01"
    began = day_or_none(employee.get("start_date") or "")
    if began and began > start:
        start = began
    return min(start, pay_date), pay_date


def working_days(start, end, non_working):
    first, last = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    return sum(1 for n in range((last - first).days + 1) if WEEKDAYS[(first + datetime.timedelta(days=n)).weekday()] not in non_working)


def absent_days(db, biz, employee_id, start, end):
    return db.one("SELECT COALESCE(SUM(days), 0) AS n FROM employee_absences WHERE business_id=? AND employee_id=? AND day >= ? AND day <= ?",
                  (biz, employee_id, start, end))["n"] or 0.0


def schedule_words(employee):
    schedule = employee.get("pay_schedule")
    if schedule == "monthly" and employee.get("pay_day"):
        n = employee["pay_day"]
        suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"Monthly, on the {n}{suffix}"
    return {"biweekly": "Every two weeks", "weekly": "Every week", "manual": "On a date set each time"}.get(schedule, "No pay date set")


def pay_preview(db, biz, employee, hr_set, pay_date, start=None, end=None):
    """What a payslip on this pay date would pay, worked out the way the payslip form works it out: the period, and either the unpaid
    trays at the person's rate or the salary for the working days less absences. The payslip form, the HR page and the alert all use it."""
    period_start, period_end = pay_period(employee, pay_date)
    start, end = start or period_start, end or period_end
    non_working = hr_set["non_working_days"].split(",")
    out = {"pay_date": pay_date, "start": start, "end": end, "next_pay_date": following_pay_date(employee, pay_date)}
    if is_casual(employee):
        rows, trays = unpaid_trays(db, biz, employee["id"], end)
        rate = rate_for(employee, hr_set)
        out.update(basis="trays", rows=rows, trays=trays, rate=rate, amount=round(trays * rate, 2))
    else:
        expected = working_days(start, end, non_working) if start <= end else 0
        absent = min(float(absent_days(db, biz, employee["id"], start, end)), float(expected))
        worked = expected - absent
        salary = employee["basic_salary"] or 0.0
        out.update(basis="days", expected=expected, absent=absent, worked=worked, salary=salary,
                   amount=round(salary * worked / expected, 2) if expected else 0.0)
    return out


@app.get("/hr/trays")
@need("admin")
def hr_trays():
    """What a payslip would pay (the period, the trays or the days), for the payslip form to show before it is saved."""
    db, biz = get_db(), g.user["business_id"]
    employee = db.one(f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE id=? AND business_id=?", (request.args.get("employee_id", type=int), biz))
    if employee is None:
        return jsonify(error="Choose an employee."), 400
    month = request.args.get("month", "")
    if month and not request.args.get("pay_date"):   # an older form: a pay month, paid on its last day
        try:
            first = datetime.datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            return jsonify(error="Choose the pay month."), 400
        pay_date, start = month_day(first.year, first.month, 31).isoformat(), first.isoformat()
    else:
        pay_date, start = day_or_none(request.args.get("pay_date", "")), day_or_none(request.args.get("from", "")) if request.args.get("from") else None
        if pay_date is None:
            return jsonify(error="Choose the pay date."), 400
    end = day_or_none(request.args.get("to", "")) if request.args.get("to") else None
    if start and end and start > end:
        return jsonify(error="The period's first day is after its last day."), 400
    p = pay_preview(db, biz, employee, hr_settings_for(db, biz), pay_date, start, end)
    p["casual"] = is_casual(employee)
    p["rows"] = [{"planting": planting_label(r), "planted_on": r["planted_on"], "trays": r["trays"]} for r in p.get("rows", [])]
    p["pay"] = p["amount"]
    return jsonify(p)


@app.route("/hr/payslips/new", methods=["GET", "POST"])
@need("admin")
def new_payslip():
    db, biz = get_db(), g.user["business_id"]
    employees = db.all(f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE business_id=? ORDER BY full_name", (biz,))
    if not employees:
        flash("Register an employee first.", "error")
        return redirect(url_for("hr"))
    hr_set = hr_settings_for(db, biz)
    non_working = hr_set["non_working_days"].split(",")

    if request.method == "POST":
        again = redirect(url_for("new_payslip", employee=request.form.get("employee_id", type=int)))
        employee = next((e for e in employees if e["id"] == request.form.get("employee_id", type=int)), None)
        if employee is None:
            flash("Choose an employee.", "error")
            return again
        # The pay date and the period it covers. An older form (and the assistant) sends only a pay month: the whole month, paid today.
        if text("pay_date"):
            pay_date = day_or_none(text("pay_date"))
            start = day_or_none(text("period_start")) if text("period_start") else None
            end = day_or_none(text("period_end")) if text("period_end") else None
            if pay_date is None or (text("period_start") and start is None) or (text("period_end") and end is None):
                flash("Check the pay date and the period's first and last days.", "error")
                return again
            default_start, default_end = pay_period(employee, pay_date)
            start, end = start or default_start, end or default_end
            pay_month = pay_date[:7]
        else:
            try:
                first = datetime.datetime.strptime(text("pay_month"), "%Y-%m").date()
            except ValueError:
                flash("Choose the pay date.", "error")
                return again
            start, end, pay_date = first.isoformat(), month_day(first.year, first.month, 31).isoformat(), today_str()
            pay_month = first.strftime("%Y-%m")   # filed under the month it pays for, as before
        if start > end:
            flash("The period's first day is after its last day.", "error")
            return again
        # A casual worker is paid by the tray; one with a salary on record (from before) can still be paid for days worked
        by_tray = is_casual(employee) and (text("pay_basis") or "trays") == "trays"
        try:
            if by_tray:
                expected = worked = 0.0
            else:
                expected = number("expected_days", default=float(working_days(start, end, non_working)))
                default_worked = max(0.0, expected - float(absent_days(db, biz, employee["id"], start, end)))
                worked = number("worked_days", default=default_worked)
            bonuses, deductions = named_amounts("bonus"), named_amounts("deduction")
        except ValueError:
            flash("Check the day counts, and that every bonus or deduction has a name and a positive amount.", "error")
            return again
        if not by_tray and (expected <= 0 or worked < 0 or worked > expected):
            flash("Expected days must be above zero, and days worked between zero and the expected days.", "error")
            return again
        next_pay = None
        if "next_pay_date" in request.form:
            next_pay = day_or_none(text("next_pay_date")) if text("next_pay_date") else None
            if text("next_pay_date") and (next_pay is None or next_pay <= pay_date):
                flash("The next pay date must be a date after this pay date.", "error")
                return again
        method, _, problem = read_money_method()
        if problem:
            flash(problem.replace("the money was paid", "the pay was paid"), "error")
            return again

        tray_rows, trays, tray_rate = [], None, None
        if by_tray:
            tray_rows, trays = unpaid_trays(db, biz, employee["id"], end)
            tray_rate = rate_for(employee, hr_set)
            if not tray_rows and not bonuses:
                flash(f"{employee['full_name']} has no unpaid trays planted up to {end}. Trays are recorded on each planting under Planting Records.", "error")
                return again
            if tray_rows and tray_rate <= 0:
                flash(f"Set how much {employee['full_name']} is paid for each tray (Edit, on HR & Payroll) first.", "error")
                return again
            basic = 0.0
            calculated_basic = round(trays * tray_rate, 2)
        else:
            basic = employee["basic_salary"]
            calculated_basic = round(basic / expected * worked, 2)
        # The employee share comes off their pay; the employer share is a cost on top and never touches net pay
        share = statutory_amounts(hr_set, calculated_basic, sum(bonuses.values()))
        emp_napsa, employer_napsa = share["napsa_employee"], share["napsa_employer"]
        emp_nhima, employer_nhima = share["nhima_employee"], share["nhima_employer"]
        net = round(calculated_basic + sum(bonuses.values()) - sum(deductions.values()) - emp_napsa - emp_nhima, 2)

        payslip_id = db.insert(
            """INSERT INTO payroll (business_id, employee_id, employee_name, pay_month, basic_salary, expected_days, worked_days, calculated_basic,
                                    bonuses_json, deductions_json, napsa_amt, nhima_amt, employer_napsa, employer_nhima, net_payable, created_at, trays, tray_rate,
                                    period_start, period_end, pay_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                biz, employee["id"], employee["full_name"], pay_month, basic, expected, worked, calculated_basic,
                json.dumps(bonuses), json.dumps(deductions), emp_napsa, emp_nhima, employer_napsa, employer_nhima, net,
                now().strftime("%Y-%m-%d %H:%M:%S"), trays, tray_rate, start, end, pay_date,
            ),
        )
        if tray_rows:
            # The payslip takes exactly the trays it was worked out from. If another payslip took any of them meanwhile, nothing is saved.
            marks = ",".join("?" for _ in tray_rows)
            taken = db.execute(
                f"UPDATE planting_trays SET paid_payslip_id=? WHERE business_id=? AND employee_id=? AND paid_payslip_id IS NULL AND id IN ({marks})",
                [payslip_id, biz, employee["id"]] + [r["id"] for r in tray_rows],
            ).rowcount
            if taken != len(tray_rows):
                db.rollback()
                flash("The trays changed while the payslip was being made. Nothing was saved; please try again.", "error")
                return again
        # The next period starts the day after this one; the next pay date is the one chosen on the form, or the next on the schedule
        if "next_pay_date" not in request.form:
            due_now = employee["next_pay_date"]
            next_pay = following_pay_date(employee, due_now) if due_now and due_now <= pay_date else due_now
        db.execute("UPDATE employees SET paid_through=?, next_pay_date=?, pay_reminded_for=NULL WHERE id=? AND business_id=?",
                   (max(end, employee["paid_through"] or ""), next_pay, employee["id"], biz))
        record_payslip_money(
            db, biz, payslip_id, employee["full_name"], f"{start} to {end}", net, emp_napsa + emp_nhima + employer_napsa + employer_nhima,
            method, stamp_for_day(pay_date) if pay_date != today_str() else now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        db.commit()
        flash(f"Payslip saved for {employee['full_name']}." + (f" Next paid on {next_pay}." if next_pay else " No next pay date is set."), "ok")
        return redirect(url_for("payslips", id=payslip_id))

    chosen = request.args.get("employee", type=int)
    picked = next((e for e in employees if e["id"] == chosen), employees[0])
    pay_date = day_or_none(request.args.get("pay_date", "")) if request.args.get("pay_date") else None
    return render_template(
        "payslip_new.html", employees=employees, hr=hr_set, methods=PAYMENT_METHODS, chosen=picked["id"],
        pay_date=pay_date or picked["next_pay_date"] or today_str(), rates={e["id"]: rate_for(e, hr_set) for e in employees},
    )


@app.get("/hr/payslips")
@need("admin")
def payslips():
    db, biz = get_db(), g.user["business_id"]
    ids = request.args.getlist("id", type=int)[:200]
    if not ids:
        flash("Tick at least one payslip to print.", "error")
        return redirect(url_for("hr"))
    marks = ",".join("?" for _ in ids)
    slips = db.all(
        f"""SELECT id, employee_name, pay_month, basic_salary, expected_days, worked_days, calculated_basic, bonuses_json, deductions_json,
                   napsa_amt, nhima_amt, employer_napsa, employer_nhima, net_payable, trays, tray_rate
            FROM payroll WHERE business_id=? AND id IN ({marks}) ORDER BY id""",
        [biz] + ids,
    )
    for slip in slips:
        slip["bonuses"] = json.loads(slip["bonuses_json"] or "{}")
        slip["deductions"] = json.loads(slip["deductions_json"] or "{}")
    return render_template("payslips.html", slips=slips)


@app.post("/hr/payslips/<int:payslip_id>/delete")
@need("admin")
def delete_payslip(payslip_id):
    db = get_db()
    slip = db.one("SELECT employee_id FROM payroll WHERE id=? AND business_id=?", (payslip_id, g.user["business_id"]))
    db.execute("DELETE FROM payroll WHERE id=? AND business_id=?", (payslip_id, g.user["business_id"]))
    if slip and slip["employee_id"]:
        # Their next pay period starts again after the latest payslip that is left
        last = db.one("SELECT MAX(period_end) AS d FROM payroll WHERE business_id=? AND employee_id=?", (g.user["business_id"], slip["employee_id"]))["d"]
        db.execute("UPDATE employees SET paid_through=? WHERE id=? AND business_id=?", (last, slip["employee_id"], g.user["business_id"]))
    db.execute("DELETE FROM cashbook WHERE business_id=? AND source='payslip' AND source_id=?", (g.user["business_id"], payslip_id))
    # The trays that payslip paid are owed again, to be paid on the next payslip
    db.execute("UPDATE planting_trays SET paid_payslip_id=NULL WHERE business_id=? AND paid_payslip_id=?", (g.user["business_id"], payslip_id))
    db.commit()
    flash("Payslip deleted, with its pay in Accounts. Any trays it paid are unpaid again.", "ok")
    return redirect(url_for("hr"))


# --- ALERTS: IN-APP NOTIFICATIONS AND WHATSAPP ---
# WhatsApp needs an account with a sending service, set up once through environment variables (never stored
# in the database). Two services are supported:
#   WHATSAPP_PROVIDER=meta       WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, WHATSAPP_TEMPLATE (an approved template whose body
#                                is a single {{1}} placeholder), optional WHATSAPP_TEMPLATE_LANG and WHATSAPP_API_VERSION
#   WHATSAPP_PROVIDER=callmebot  WHATSAPP_CALLMEBOT_KEY (the key CallMeBot issued to the alert phone number)
# Until one is configured, alerts still appear in the app and the WhatsApp copy is recorded as skipped.
ALERT_KINDS = (("low_stock", "Low stock"), ("permission", "Requests for admin permission"), ("preorders", "Pre-orders almost due"),
               ("collections", "Sales to be collected"), ("deliveries", "Deliveries due today"), ("requests", "Customer requests"), ("payroll", "Pay dates"))
ITEM_ACTIONS = (("procure", "edit this item's details"), ("transfer", "transfer or use this item's stock"), ("delete", "delete this item"))
GRANT_MINUTES = 60
# An operator's request to delete one invoice travels through the same request/approve/spend-once flow as the item actions
DELETE_INVOICE = "delete_invoice"
COLLECT_PREORDER = "collect_preorder"
COLLECT_GROUP = "collect_preorders"   # a customer's pre-orders collected together on one invoice
REQUEST_LABELS = {**dict(ITEM_ACTIONS), DELETE_INVOICE: "delete this invoice", COLLECT_PREORDER: "collect a pre-order and record it as a sale",
                  COLLECT_GROUP: "collect a customer's pre-orders and record them as one sale"}
WHATSAPP_TIMEOUT = 6


def whatsapp_service():
    """Returns (provider, None) when WhatsApp sending is configured, otherwise (None, why not)."""
    provider = os.environ.get("WHATSAPP_PROVIDER", "").strip().lower()
    if provider == "meta":
        missing = [name for name in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID", "WHATSAPP_TEMPLATE") if not os.environ.get(name)]
        return (None, "missing " + ", ".join(missing)) if missing else ("meta", None)
    if provider == "callmebot":
        return ("callmebot", None) if os.environ.get("WHATSAPP_CALLMEBOT_KEY") else (None, "missing WHATSAPP_CALLMEBOT_KEY")
    return None, "no WhatsApp service has been connected yet"


def whatsapp_number(phone):
    """Digits with the country code, as WhatsApp wants them. A local Zambian 09... number gains its 260."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10 and digits.startswith("0"):
        digits = "260" + digits[1:]
    return digits if 9 <= len(digits) <= 15 else ""


def send_whatsapp(phone, message):
    """Sends one WhatsApp message. Returns (status, detail) with status 'sent', 'failed' or 'skipped'; never raises."""
    import urllib.error
    import urllib.parse
    import urllib.request

    number_to = whatsapp_number(phone)
    if not number_to:
        return "skipped", "no valid WhatsApp number is saved in Settings"
    provider, problem = whatsapp_service()
    if provider is None:
        return "skipped", problem

    # One line of plain text: template parameters may not contain line breaks or long runs of spaces
    message = " ".join(message.split())[:900]
    try:
        if provider == "meta":
            version = os.environ.get("WHATSAPP_API_VERSION", "v21.0")
            payload = {
                "messaging_product": "whatsapp", "recipient_type": "individual", "to": number_to, "type": "template",
                "template": {
                    "name": os.environ["WHATSAPP_TEMPLATE"], "language": {"code": os.environ.get("WHATSAPP_TEMPLATE_LANG", "en")},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": message}]}],
                },
            }
            call = urllib.request.Request(
                f"https://graph.facebook.com/{version}/{os.environ['WHATSAPP_PHONE_ID']}/messages",
                data=json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Authorization": "Bearer " + os.environ["WHATSAPP_TOKEN"], "Content-Type": "application/json"},
            )
        else:
            query = urllib.parse.urlencode({"phone": "+" + number_to, "text": message, "apikey": os.environ["WHATSAPP_CALLMEBOT_KEY"]})
            call = urllib.request.Request("https://api.callmebot.com/whatsapp.php?" + query, method="GET")
        with urllib.request.urlopen(call, timeout=WHATSAPP_TIMEOUT) as reply:
            answer = reply.read(2000).decode("utf-8", errors="replace")
        if provider == "callmebot" and re.search(r"error|invalid|not\s+(valid|allowed|activated)", answer, re.I):
            return "failed", re.sub(r"<[^>]+>", " ", answer)[:200].strip()
        return "sent", ""
    except urllib.error.HTTPError as e:
        detail = e.read(600).decode("utf-8", errors="replace")
        return "failed", f"the WhatsApp service answered {e.code}: {' '.join(detail.split())[:200]}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return "failed", f"could not reach the WhatsApp service ({type(e).__name__})"


def alert_enabled(kind):
    """An alert kind is on unless an admin has switched it off in Settings."""
    return g.user["alert_" + kind] != 0


def notify(db, kind, title, body, link, for_user_id=None, whatsapp=False):
    """Records an in-app alert for one user, or for the business's admins when for_user_id is None.

    With whatsapp=True a copy is queued for the number in Settings. It is sent after the response is
    ready, and only if the alert was really saved, so a failed sale never sends a message.
    """
    notification_id = db.insert(
        "INSERT INTO notifications (business_id, for_user_id, kind, title, body, link, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (g.user["business_id"], for_user_id, kind, title, body, link, now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    if whatsapp:
        outbox = g.setdefault("whatsapp_outbox", [])
        outbox.append((notification_id, f"{g.user['business_name']}: {title}. {body}"))
    g.setdefault("push_outbox", []).append((notification_id, g.user["business_id"], for_user_id, title, body))
    return notification_id


def flush_whatsapp(db):
    """Sends the WhatsApp copies this request queued, for the alerts that were really saved."""
    outbox = g.pop("whatsapp_outbox", None)
    if not outbox or not g.get("user"):
        return
    for notification_id, message in outbox:
        # Skip anything whose alert was rolled back together with the action that raised it
        if not db.one("SELECT id FROM notifications WHERE id=?", (notification_id,)):
            continue
        status, detail = send_whatsapp(g.user["alert_phone"], message)
        db.execute("UPDATE notifications SET wa_status=?, wa_error=? WHERE id=?", (status, detail, notification_id))
    db.commit()


@app.after_request
def deliver_whatsapp(response):
    if not g.get("whatsapp_outbox"):
        return response
    db = get_db()
    try:
        flush_whatsapp(db)
    except DB_ERRORS:
        db.rollback()
    return response


# --- PUSH NOTIFICATIONS ---
# A phone or computer can ask (Settings > Alerts) to be told about alerts even while the app is closed. The browser gives the app an
# address at its push service (Google's for Chrome and Android, Mozilla's, Apple's, Microsoft's) and keys; every alert made for that person
# is then sent there, encrypted, and the device shows it (sw.js), with the unread count on the installed app's icon. Set up once on the
# server with VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY (made by "flask --app app push-keys") and VAPID_SUBJECT (mailto: an address).
PUSH_TIMEOUT = 5
PUSH_TTL = 24 * 3600     # an alert the device could not get within a day is dropped


def push_ready():
    return bool(os.environ.get("VAPID_PUBLIC_KEY") and os.environ.get("VAPID_PRIVATE_KEY"))


def unread_count(db, biz, user_id, admin):
    where = "business_id=? AND (for_user_id=? OR for_user_id IS NULL)" if admin else "business_id=? AND for_user_id=?"
    return db.one(f"SELECT COUNT(*) AS n FROM notifications WHERE {where} AND read_at IS NULL", (biz, user_id))["n"]


def send_pushes(db):
    """Sends the alerts this request made to the devices of the people they are for. Called after the response is ready (and by the crons),
    only for alerts that were really saved."""
    outbox = g.pop("push_outbox", None)
    if not outbox or not push_ready():
        return
    from pywebpush import WebPushException, webpush

    claims = {"sub": os.environ.get("VAPID_SUBJECT", "").strip() or "mailto:admin@example.com"}
    for notification_id, biz, for_user_id, title, body in outbox:
        if not db.one("SELECT id FROM notifications WHERE id=?", (notification_id,)):
            continue
        if for_user_id is None:
            people = db.all("SELECT id, role FROM users WHERE business_id=? AND role='admin'", (biz,))
        else:
            people = db.all("SELECT id, role FROM users WHERE business_id=? AND id=?", (biz, for_user_id))
        for person in people:
            devices = db.all("SELECT * FROM push_subscriptions WHERE business_id=? AND user_id=?", (biz, person["id"]))
            if not devices:
                continue
            payload = json.dumps({"title": title, "body": body[:300], "tag": f"alert-{notification_id}",
                                  "url": url_for("open_notification", notification_id=notification_id),
                                  "unread": unread_count(db, biz, person["id"], person["role"] == "admin")})
            for d in devices:
                try:
                    webpush({"endpoint": d["endpoint"], "keys": {"p256dh": d["p256dh"], "auth": d["auth"]}}, payload,
                            vapid_private_key=os.environ["VAPID_PRIVATE_KEY"].strip(), vapid_claims=dict(claims), timeout=PUSH_TIMEOUT, ttl=PUSH_TTL)
                    db.execute("UPDATE push_subscriptions SET last_sent_at=? WHERE id=?", (now().strftime("%Y-%m-%d %H:%M:%S"), d["id"]))
                except WebPushException as e:
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    if status in (404, 410):   # the device withdrew it, or the app was uninstalled
                        db.execute("DELETE FROM push_subscriptions WHERE id=?", (d["id"],))
                    else:
                        app.logger.warning("A push notification could not be sent: %s", e)
                except Exception as e:   # the push service unreachable: the alert is still in the app
                    app.logger.warning("A push notification could not be sent: %s", e)
    db.commit()


@app.after_request
def deliver_pushes(response):
    if g.get("push_outbox"):
        try:
            send_pushes(get_db())
        except DB_ERRORS:
            get_db().rollback()
    return response


@app.cli.command("push-keys")
def push_keys():
    """Prints a new pair of VAPID keys for push notifications, to put in the server's environment variables."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def b64(data):
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    print("VAPID_PUBLIC_KEY=" + b64(public))
    print("VAPID_PRIVATE_KEY=" + b64(raw))


def push_answer(**data):
    return Response(json.dumps(data), mimetype="application/json", headers={"Cache-Control": "no-store"})


@app.post("/push/subscribe")
@need(None)
def push_subscribe():
    """This device asks for push notifications for the person signed in. The same device signing in as someone else moves to them."""
    endpoint, p256dh, auth = (request.form.get(k, "").strip() for k in ("endpoint", "p256dh", "auth"))
    if not push_ready():
        return push_answer(ok=False, error="Push notifications are not set up on this server yet.")
    if not endpoint.startswith("https://") or len(endpoint) > 1000 or not p256dh or not auth or len(p256dh) > 200 or len(auth) > 100:
        return push_answer(ok=False, error="The browser did not give a usable push address.")
    db, biz, stamp = get_db(), g.user["business_id"], now().strftime("%Y-%m-%d %H:%M:%S")
    device = (request.form.get("device", "") or request.headers.get("User-Agent", ""))[:120]
    db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
    db.execute("INSERT INTO push_subscriptions (business_id, user_id, endpoint, p256dh, auth, device, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (biz, g.user["id"], endpoint, p256dh, auth, device, stamp))
    db.commit()
    return push_answer(ok=True, devices=db.one("SELECT COUNT(*) AS n FROM push_subscriptions WHERE business_id=? AND user_id=?", (biz, g.user["id"]))["n"])


@app.post("/push/unsubscribe")
@need(None)
def push_unsubscribe():
    db = get_db()
    db.execute("DELETE FROM push_subscriptions WHERE endpoint=? AND business_id=? AND user_id=?",
               (request.form.get("endpoint", ""), g.user["business_id"], g.user["id"]))
    db.commit()
    return push_answer(ok=True)


@app.post("/push/test")
@need(None)
def push_test():
    db = get_db()
    if not db.one("SELECT id FROM push_subscriptions WHERE business_id=? AND user_id=?", (g.user["business_id"], g.user["id"])):
        return push_answer(ok=False, error="This account has no device with notifications turned on.")
    notify(db, "test", "Test notification", "If this shows on your phone or computer, notifications reach you even with the app closed.",
           url_for("notifications"), for_user_id=g.user["id"])
    db.commit()
    return push_answer(ok=True)


# Each morning (vercel.json) Vercel calls this with the CRON_SECRET it was given: the alerts that are due by the day (pre-orders, collections,
# deliveries, customer requests, pay dates and the reminder report) are raised for every business even if nobody has the app open, so they
# reach phones as push notifications and WhatsApp. Each is claimed as it is raised, so the alerts poll never raises one twice.
@app.get("/cron/reminders")
def cron_reminders():
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return jsonify({"error": "CRON_SECRET is not set on the server."}), 503
    if not hmac.compare_digest(request.headers.get("Authorization", "").encode("utf-8"), f"Bearer {secret}".encode("utf-8")):
        abort(401)
    db, done, failed = get_db(), 0, 0
    for b in db.all("SELECT id FROM businesses WHERE demo_expires_at IS NULL ORDER BY id"):
        admin = db.one("SELECT id FROM users WHERE business_id=? AND role='admin' ORDER BY id LIMIT 1", (b["id"],))
        if not admin:
            continue
        g.user = user_row(db, admin["id"])
        try:
            for remind in (remind_preorders_due, remind_collections_due, remind_deliveries_due, remind_requests_due, remind_pay_due, remind_report_due):
                remind(db, b["id"])
            flush_whatsapp(db)
            send_pushes(db)
            done += 1
        except Exception:   # one business's failure must not stop the others
            app.logger.exception("The morning reminders of business %s failed", b["id"])
            db.rollback()
            failed += 1
        finally:
            g.pop("whatsapp_outbox", None)
            g.pop("push_outbox", None)
            g.user = None
    return jsonify({"businesses": done, "failed": failed})


def low_stock_alert(db, name, code, before, after, where):
    """Raises the alert once, at the moment a quantity drops below the threshold, not on every later sale."""
    threshold = g.user["low_stock_threshold"]
    if alert_enabled("low_stock") and before >= threshold > after:
        notify(
            db, "low_stock", f"Low stock: {name}",
            f"{name} (code {code}) is down to {after} {where}. The alert level is {threshold}.",
            url_for("inventory"), whatsapp=True,
        )


def my_notifications_clause():
    """SQL and parameters selecting the alerts the signed-in user should see."""
    if g.user["role"] == "admin":
        return "business_id=? AND (for_user_id=? OR for_user_id IS NULL)", [g.user["business_id"], g.user["id"]]
    return "business_id=? AND for_user_id=?", [g.user["business_id"], g.user["id"]]


@app.get("/notifications/poll")
@need(None)
def notifications_poll():
    remind_preorders_due(get_db(), g.user["business_id"])
    remind_collections_due(get_db(), g.user["business_id"])
    remind_deliveries_due(get_db(), g.user["business_id"])
    remind_requests_due(get_db(), g.user["business_id"])
    remind_pay_due(get_db(), g.user["business_id"])
    remind_report_due(get_db(), g.user["business_id"])
    where, params = my_notifications_clause()
    rows = get_db().all(
        f"SELECT id, kind, title, body, created_at FROM notifications WHERE {where} AND read_at IS NULL ORDER BY id DESC LIMIT 20", params
    )
    for row in rows:
        row["open"] = url_for("open_notification", notification_id=row["id"])
    return {"unread": len(rows), "items": rows}


@app.get("/notifications/<int:notification_id>/open")
@need(None)
def open_notification(notification_id):
    """Marks one alert as read and goes to what it is about."""
    db = get_db()
    where, params = my_notifications_clause()
    row = db.one(f"SELECT id, link FROM notifications WHERE {where} AND id=?", params + [notification_id])
    if row is None:
        abort(404)
    db.execute("UPDATE notifications SET read_at=? WHERE id=?", (now().strftime("%Y-%m-%d %H:%M:%S"), notification_id))
    db.commit()
    # Links are written by this app; anything that is not a local path is ignored
    link = row["link"] or ""
    return redirect(link if link.startswith("/") and not link.startswith("//") else url_for("notifications"))


@app.route("/notifications", methods=["GET", "POST"])
@need(None)
def notifications():
    db = get_db()
    where, params = my_notifications_clause()
    if request.method == "POST":
        db.execute(f"UPDATE notifications SET read_at=? WHERE {where} AND read_at IS NULL", [now().strftime("%Y-%m-%d %H:%M:%S")] + params)
        db.commit()
        return redirect(url_for("notifications"))
    rows = db.all(
        f"SELECT id, kind, title, body, created_at, read_at, wa_status, wa_error FROM notifications WHERE {where} ORDER BY id DESC LIMIT 100", params
    )
    return render_template("notifications.html", rows=rows)


@app.post("/settings/alerts")
@need("admin")
def save_alerts():
    db = get_db()
    phone = text("alert_phone")
    if phone and not whatsapp_number(phone):
        flash("Enter the WhatsApp number with its country code, for example +260 972 047 233.", "error")
        return redirect(url_for("settings"))
    db.execute(
        "UPDATE businesses SET alert_phone=?, alert_low_stock=?, alert_permission=?, alert_preorders=?, alert_collections=?, alert_deliveries=?, alert_requests=?, alert_payroll=? WHERE id=?",
        (phone, 1 if request.form.get("alert_low_stock") else 0, 1 if request.form.get("alert_permission") else 0,
         1 if request.form.get("alert_preorders") else 0, 1 if request.form.get("alert_collections") else 0,
         1 if request.form.get("alert_deliveries") else 0, 1 if request.form.get("alert_requests") else 0, 1 if request.form.get("alert_payroll") else 0, g.user["business_id"]),
    )
    every, stale = request.form.get("reminder_every", type=int), request.form.get("reminder_stale_days", type=int)
    if every in dict(REMINDER_EVERY):
        db.execute("UPDATE businesses SET reminder_every=? WHERE id=?", (every, g.user["business_id"]))
    if stale is not None and 1 <= stale <= 365:
        db.execute("UPDATE businesses SET reminder_stale_days=? WHERE id=?", (stale, g.user["business_id"]))
    db.commit()
    flash("Alert settings saved.", "ok")
    return redirect(url_for("settings"))


@app.post("/settings/alerts/test")
@need("admin")
def test_alert():
    db = get_db()
    notification_id = notify(db, "test", "Test alert", "This is a test. If you can read it, alerts are reaching you.", url_for("notifications"))
    db.commit()
    status, detail = send_whatsapp(g.user["alert_phone"], f"{g.user['business_name']}: Test alert. If you can read this, WhatsApp alerts are working.")
    db.execute("UPDATE notifications SET wa_status=?, wa_error=? WHERE id=?", (status, detail, notification_id))
    db.commit()
    if status == "sent":
        flash(f"Test sent to WhatsApp number {g.user['alert_phone']}. A popup will also appear here shortly.", "ok")
    else:
        flash(f"The popup alert works, but the WhatsApp message was not sent: {detail}.", "error")
    return redirect(url_for("settings"))


# --- PERMISSION REQUESTS ---
def active_request(db, item_id, action):
    """The signed-in user's latest request for this action on this item that is still pending or approved and unexpired."""
    row = db.one(
        """SELECT id, status, expires_at, reason, decided_by FROM permission_requests
           WHERE business_id=? AND user_id=? AND item_id=? AND action=? AND status IN ('pending', 'approved') ORDER BY id DESC LIMIT 1""",
        (g.user["business_id"], g.user["id"], item_id, action),
    )
    if row and row["status"] == "approved" and (row["expires_at"] or "") < now().strftime("%Y-%m-%d %H:%M:%S"):
        return None
    return row


def item_grant(db, item_id, action):
    """'own' when the user's account allows the action, the approved request when an admin allowed it once, else None."""
    if can(action):
        return "own"
    row = active_request(db, item_id, action)
    return row if row and row["status"] == "approved" else None


def use_grant(db, grant):
    """An admin's approval covers one use: it is spent when the action goes through."""
    if isinstance(grant, dict):
        db.execute("UPDATE permission_requests SET status='used' WHERE id=?", (grant["id"],))


def item_access_state(db, item_id, action):
    """For the item page: 'own', 'approved', 'pending' or 'none', plus when an approval runs out."""
    if can(action):
        return {"state": "own", "until": ""}
    row = active_request(db, item_id, action)
    return {"state": row["status"], "until": (row["expires_at"] or "")[11:16]} if row else {"state": "none", "until": ""}


def refuse_item_action(item_id):
    flash("Your account does not have permission to do that. Use the request button to ask an admin.", "error")
    return redirect(url_for("item", item_id=item_id))


@app.post("/inventory/<int:item_id>/request-permission")
@need(None)
def request_permission(item_id):
    db, biz = get_db(), g.user["business_id"]
    target = get_item(db, item_id)
    action = text("action")
    back = redirect(url_for("item", item_id=item_id))
    if action not in dict(ITEM_ACTIONS):
        abort(400)
    if can(action):
        flash("Your account already has that permission.", "ok")
        return back
    if active_request(db, item_id, action):
        flash("You already have a request for this waiting or approved.", "ok")
        return back

    reason = text("reason")[:200]
    request_id = db.insert(
        """INSERT INTO permission_requests (business_id, user_id, username, action, item_id, item_name, reason, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (biz, g.user["id"], g.user["username"], action, item_id, target["name"], reason, now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    if alert_enabled("permission"):
        notify(
            db, "permission", f"Permission request from {g.user['username']}",
            f"{g.user['username']} asks to {dict(ITEM_ACTIONS)[action]}: {target['name']} (code {target['item_code']})."
            + (f" Reason: {reason}." if reason else "") + " Open the app to approve or deny.",
            url_for("permission_requests") + f"#request-{request_id}", whatsapp=True,
        )
    db.commit()
    flash("Your request has been sent to the admin. You will get a popup here when they answer.", "ok")
    return back


@app.get("/requests")
@need("admin")
def permission_requests():
    rows = get_db().all(
        """SELECT id, username, action, item_id, item_name, reason, status, created_at, decided_by, decided_at, expires_at
           FROM permission_requests WHERE business_id=? ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, id DESC LIMIT 200""",
        (g.user["business_id"],),
    )
    return render_template("requests.html", rows=rows, actions=REQUEST_LABELS, delete_invoice=DELETE_INVOICE, minutes=GRANT_MINUTES)


@app.post("/requests/<int:request_id>/decide")
@need("admin")
def decide_request(request_id):
    db, biz = get_db(), g.user["business_id"]
    row = db.one("SELECT id, user_id, action, item_id, item_name, status FROM permission_requests WHERE id=? AND business_id=?", (request_id, biz))
    approve = text("decision") == "approve"
    if row is None or row["status"] != "pending" or text("decision") not in ("approve", "deny"):
        flash("That request has already been answered.", "error")
        return redirect(url_for("permission_requests"))

    stamp = now()
    expires = (stamp + datetime.timedelta(minutes=GRANT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S") if approve else None
    db.execute(
        "UPDATE permission_requests SET status=?, decided_by=?, decided_at=?, expires_at=? WHERE id=?",
        ("approved" if approve else "denied", g.user["username"], stamp.strftime("%Y-%m-%d %H:%M:%S"), expires, request_id),
    )
    what = REQUEST_LABELS[row["action"]]
    if row["action"] == DELETE_INVOICE:
        target = url_for("invoice", invoice_id=row["item_id"])
    elif row["action"] in (COLLECT_PREORDER, COLLECT_GROUP):
        target = url_for("up_next") + "#preorders"
    else:
        target = url_for("item", item_id=row["item_id"])
    if approve:
        notify(
            db, "decision", "Request approved",
            f"{g.user['username']} approved your request to {what}: {row['item_name']}. It covers one use and lasts {GRANT_MINUTES} minutes.",
            target, for_user_id=row["user_id"],
        )
    else:
        notify(
            db, "decision", "Request denied",
            f"{g.user['username']} did not approve your request to {what}: {row['item_name']}.",
            target, for_user_id=row["user_id"],
        )
    db.commit()
    flash("Request approved. The operator has been notified." if approve else "Request denied. The operator has been notified.", "ok")
    return redirect(url_for("permission_requests"))


# --- EMPLOYEE STATEMENTS (ADMIN) ---
STATEMENT_PERIODS = (
    ("this_year", "This year"), ("last_6", "Last 6 months"), ("last_12", "Last 12 months"), ("all", "All time"), ("custom", "Custom range"),
)


def shift_month(year, month, by):
    index = year * 12 + (month - 1) + by
    return index // 12, index % 12 + 1


def statement_months():
    """Returns (choice, first month, last month) as 'YYYY-MM' strings; both are None for 'all time'."""
    today = now().date()
    this_month = today.strftime("%Y-%m")
    choice = request.args.get("period", "this_year")
    if choice == "custom":
        try:
            start = datetime.datetime.strptime(request.args.get("from", ""), "%Y-%m").strftime("%Y-%m")
            end = datetime.datetime.strptime(request.args.get("to", ""), "%Y-%m").strftime("%Y-%m")
            return choice, min(start, end), max(start, end)
        except ValueError:
            choice = "this_year"
    if choice == "all":
        return choice, None, None
    if choice in ("last_6", "last_12"):
        year, month = shift_month(today.year, today.month, -(5 if choice == "last_6" else 11))
        return choice, f"{year:04d}-{month:02d}", this_month
    return "this_year", f"{today.year:04d}-01", f"{today.year:04d}-12"


@app.get("/hr/statements")
@need("admin")
def employee_statements():
    db, biz = get_db(), g.user["business_id"]
    q = request.args.get("q", "").strip()
    sql = """SELECT e.id, e.full_name, e.nrc_number, e.employment_type, e.basic_salary,
                    COUNT(p.id) AS payslips, MIN(p.pay_month) AS first_month, MAX(p.pay_month) AS last_month
             FROM employees e LEFT JOIN payroll p ON p.employee_id = e.id AND p.business_id = e.business_id
             WHERE e.business_id=?"""
    params = [biz]
    if q:
        like = f"%{q.lower()}%"
        sql += " AND (LOWER(e.full_name) LIKE ? OR LOWER(e.nrc_number) LIKE ?)"
        params += [like, like]
    sql += " GROUP BY e.id, e.full_name, e.nrc_number, e.employment_type, e.basic_salary ORDER BY e.full_name LIMIT 100"
    return render_template("employee_statements.html", results=db.all(sql, params), q=q)


@app.get("/hr/employees/<int:employee_id>/statement")
@need("admin")
def employee_statement(employee_id):
    db, biz = get_db(), g.user["business_id"]
    employee = db.one(
        "SELECT id, full_name, nrc_number, employment_type, basic_salary, start_date, expected_days_week FROM employees WHERE id=? AND business_id=?",
        (employee_id, biz),
    )
    if employee is None:
        abort(404)

    choice, start, end = statement_months()
    slips = db.all(
        """SELECT pay_month, expected_days, worked_days, trays, calculated_basic, bonuses_json, deductions_json,
                  napsa_amt, nhima_amt, employer_napsa, employer_nhima, net_payable
           FROM payroll WHERE employee_id=? AND business_id=? AND pay_month >= ? AND pay_month <= ? ORDER BY pay_month, id""",
        (employee_id, biz, start or "0000-00", end or "9999-99"),
    )

    money_keys = ("basic", "bonuses", "deductions", "napsa", "nhima", "employer_napsa", "employer_nhima", "net")
    blank = dict.fromkeys(("payslips", "expected", "worked", "absent", "trays") + money_keys, 0)
    months, totals, named = {}, dict(blank), {"bonus": {}, "deduction": {}}
    for slip in slips:
        bonuses, deductions = json.loads(slip["bonuses_json"] or "{}"), json.loads(slip["deductions_json"] or "{}")
        for kind, items in (("bonus", bonuses), ("deduction", deductions)):
            for name, amount in items.items():
                named[kind][name] = named[kind].get(name, 0.0) + (amount or 0.0)
        figures = {
            "payslips": 1, "expected": slip["expected_days"], "worked": slip["worked_days"], "trays": slip["trays"] or 0,
            # Absence is counted payslip by payslip: the days expected that were not worked
            "absent": max(0.0, slip["expected_days"] - slip["worked_days"]),
            "basic": slip["calculated_basic"], "bonuses": sum(bonuses.values()), "deductions": sum(deductions.values()),
            "napsa": slip["napsa_amt"], "nhima": slip["nhima_amt"], "employer_napsa": slip["employer_napsa"] or 0.0,
            "employer_nhima": slip["employer_nhima"] or 0.0, "net": slip["net_payable"],
        }
        month = months.setdefault(slip["pay_month"], dict(blank, month=slip["pay_month"]))
        for key, value in figures.items():
            month[key] += value or 0
            totals[key] += value or 0

    worked_months = len(months)
    summary = {
        "months": worked_months,
        "average_absent": totals["absent"] / worked_months if worked_months else 0.0,
        "attendance": 100.0 * totals["worked"] / totals["expected"] if totals["expected"] else None,
        "average_net": totals["net"] / worked_months if worked_months else 0.0,
    }
    return render_template(
        "employee_statement.html", employee=employee, months=list(months.values()), totals=totals, summary=summary,
        bonuses=sorted(named["bonus"].items()), deductions=sorted(named["deduction"].items()),
        periods=STATEMENT_PERIODS, period=choice, start=start, end=end, printed=now().strftime("%d %b %Y %H:%M"),
    )


# --- CUSTOMER STATEMENTS (ADMIN) ---
@app.get("/customers")
@need("admin")
def customers():
    db, biz = get_db(), g.user["business_id"]
    q = request.args.get("q", "").strip()
    sql = """SELECT c.id, c.name, c.phone, COUNT(i.id) AS invoices, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.balance_due), 0) AS owed
             FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id AND i.kind='SALE' AND i.payment_status <> 'Cancelled'
             WHERE c.business_id=?"""
    params = [biz]
    if q:
        sql += " AND (c.name_key LIKE ?"
        params.append(f"%{' '.join(q.lower().split())}%")
        if phone_key(q):
            sql += " OR c.phone_key LIKE ?"
            params.append(f"%{phone_key(q)}%")
        sql += ")"
    sql += " GROUP BY c.id, c.name, c.phone ORDER BY c.name LIMIT 100"
    return render_template("customers.html", results=db.all(sql, params), q=q, duplicate_count=len(find_duplicates(db, biz)))


def get_customer(db, customer_id):
    customer = db.one("SELECT id, name, phone, created_at FROM customers WHERE id=? AND business_id=?", (customer_id, g.user["business_id"]))
    if customer is None:
        abort(404)
    return customer


def statement_data(db, customer_id):
    """Everything a customer statement shows, shared by the page and the PDF."""
    biz = g.user["business_id"]
    invoices = db.all(
        """SELECT id, invoice_no, created_at, created_by, payment_status, total, amount_paid, balance_due, cancelled_at, cancelled_by, cancel_reason,
                  collect_on, collected_at
           FROM invoices WHERE customer_id=? AND business_id=? AND kind='SALE' ORDER BY created_at, id""",
        (customer_id, biz),
    )
    lines = db.all(
        """SELECT ii.invoice_id, ii.item_code, ii.item_name, ii.qty, ii.unit_price, COALESCE(ii.discount, 0) AS discount, ii.line_total
           FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
           WHERE i.customer_id=? AND i.business_id=? AND i.kind='SALE' ORDER BY ii.id""",
        (customer_id, biz),
    )
    changes = db.all(
        """SELECT pl.ref_id, pl.old_paid, pl.new_paid, pl.note, pl.changed_by, pl.changed_at
           FROM payment_log pl JOIN invoices i ON i.id = pl.ref_id
           WHERE pl.kind='sale' AND pl.business_id=? AND i.customer_id=? ORDER BY pl.id""",
        (biz, customer_id),
    )

    products, lines_of, changes_of = {}, {}, {}
    for line in lines:
        lines_of.setdefault(line["invoice_id"], []).append(line)
    for change in changes:
        changes_of.setdefault(change["ref_id"], []).append(change)
    for inv in invoices:
        inv["lines"] = lines_of.get(inv["id"], [])
        inv["changes"] = changes_of.get(inv["id"], [])
        if inv["payment_status"] == "Cancelled":
            continue  # kept on the statement for the record, but not counted as something they bought
        for line in inv["lines"]:
            p = products.setdefault((line["item_code"], line["item_name"]), {"code": line["item_code"], "name": line["item_name"], "qty": 0, "value": 0.0, "last": ""})
            p["qty"] += line["qty"]
            p["value"] += line["line_total"]
            p["last"] = max(p["last"], inv["created_at"][:10])

    # The statement: every invoice as a charge and every payment as a credit, in date order with a running balance.
    # Whatever was paid before the first logged change was paid at the time of sale. A cancelled invoice still shows
    # as a line, clearly marked, but charges and pays nothing - it no longer affects the running balance.
    entries = []
    for inv in invoices:
        cancelled = inv["payment_status"] == "Cancelled"
        items = ", ".join(f"{line['qty']} x {line['item_name']}" for line in inv["lines"])
        what = f"Invoice {inv['invoice_no']} (cancelled)" if cancelled else f"Invoice {inv['invoice_no']}"
        entries.append({"at": inv["created_at"], "order": 0, "what": what, "detail": items, "charge": 0.0 if cancelled else inv["total"], "paid": 0.0, "invoice_id": inv["id"]})
        if cancelled:
            continue
        at_sale = inv["changes"][0]["old_paid"] if inv["changes"] else inv["amount_paid"]
        if at_sale > 0:
            entries.append({"at": inv["created_at"], "order": 1, "what": "Payment at time of sale", "detail": inv["invoice_no"], "charge": 0.0, "paid": at_sale, "invoice_id": inv["id"]})
        for change in inv["changes"]:
            amount = round(change["new_paid"] - change["old_paid"], 2)
            if amount:
                detail = inv["invoice_no"] + (f" — {change['note']}" if change["note"] else "") + f" (recorded by {change['changed_by']})"
                entries.append({
                    "at": change["changed_at"], "order": 2, "what": "Payment received" if amount > 0 else "Payment correction",
                    "detail": detail, "charge": 0.0, "paid": amount, "invoice_id": inv["id"],
                })
    entries.sort(key=lambda e: (e["at"], e["invoice_id"], e["order"]))
    balance = 0.0
    for entry in entries:
        balance = round(balance + entry["charge"] - entry["paid"], 2)
        entry["balance"] = balance

    active = [inv for inv in invoices if inv["payment_status"] != "Cancelled"]
    summary = {
        "invoices": len(active), "total": sum(inv["total"] for inv in active),
        "paid": sum(inv["amount_paid"] for inv in active), "owed": sum(inv["balance_due"] for inv in active),
    }
    return {
        "invoices": invoices, "entries": entries, "summary": summary,
        "products": sorted(products.values(), key=lambda p: -p["value"]), "printed": now().strftime("%d %b %Y %H:%M"),
    }


@app.get("/customers/<int:customer_id>")
@need("admin")
def customer_statement(customer_id):
    db = get_db()
    return render_template(
        "customer.html", customer=get_customer(db, customer_id), twins=phone_twins(db, g.user["business_id"], customer_id),
        methods=PAYMENT_METHODS, today=today_str(), **statement_data(db, customer_id),
    )


@app.get("/customers/<int:customer_id>/statement.pdf")
@need("admin")
def customer_statement_pdf(customer_id):
    db = get_db()
    customer = get_customer(db, customer_id)
    pdf = statement_pdf(customer, statement_data(db, customer_id))
    filename = "statement-" + (re.sub(r"[^A-Za-z0-9]+", "-", customer["name"]).strip("-") or "customer") + ".pdf"
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --- Delivery lists: invoices ticked on a customer's statement, put on one delivery note ---
# Each invoice with its items, quantities and prices, the totals and what is still owed on them, the delivery date, and lines for
# whoever delivers and whoever receives to sign. A printable page, and the same as a PDF.
def delivery_data(db, customer, ids, day=None):
    """The ticked invoices of this customer that can be delivered (sales that were not cancelled), with their lines and totals."""
    biz = g.user["business_id"]
    ids = list(dict.fromkeys(ids))[:200]
    invoices = db.all(
        f"""SELECT id, invoice_no, created_at, payment_status, total, amount_paid, balance_due, collect_on, collected_at FROM invoices
            WHERE business_id=? AND customer_id=? AND kind='SALE' AND payment_status <> 'Cancelled' AND id IN ({', '.join('?' for _ in ids) or 'NULL'})
            ORDER BY created_at, id""",
        [biz, customer["id"]] + ids,
    ) if ids else []
    lines_of = {}
    if invoices:
        for line in db.all(
            f"""SELECT invoice_id, item_code, item_name, qty, unit_price, COALESCE(discount, 0) AS discount, line_total FROM invoice_items
                WHERE business_id=? AND invoice_id IN ({', '.join('?' for _ in invoices)}) ORDER BY id""",
            [biz] + [inv["id"] for inv in invoices],
        ):
            lines_of.setdefault(line["invoice_id"], []).append(line)
    for inv in invoices:
        inv["lines"] = lines_of.get(inv["id"], [])
        inv["waiting"] = bool(inv["collect_on"] and not inv["collected_at"])
    return {
        "invoices": invoices, "day": day or today_str(), "printed": now().strftime("%d %b %Y %H:%M"),
        "units": sum(line["qty"] for inv in invoices for line in inv["lines"]),
        "total": round(sum(inv["total"] for inv in invoices), 2), "paid": round(sum(inv["amount_paid"] for inv in invoices), 2),
        "owed": round(sum(inv["balance_due"] for inv in invoices), 2), "waiting": [inv for inv in invoices if inv["waiting"]],
    }


def delivery_request(customer_id):
    """The customer, the ticked invoices and the delivery day, from the address: (customer, data, query args)."""
    db = get_db()
    customer = get_customer(db, customer_id)
    ids = request.args.getlist("invoice", type=int)
    day = day_or_none(request.args.get("date", "")) if request.args.get("date") else None
    return customer, delivery_data(db, customer, ids, day), {"invoice": ids, **({"date": day} if day else {})}


@app.get("/customers/<int:customer_id>/delivery")
@need("admin")
def customer_delivery(customer_id):
    customer, data, args = delivery_request(customer_id)
    if not data["invoices"]:
        flash("Tick at least one invoice (not cancelled) to make a delivery list.", "error")
        return redirect(url_for("customer_statement", customer_id=customer_id) + "#delivery")
    return render_template("delivery.html", customer=customer, d=data, pdf_url=url_for("customer_delivery_pdf", customer_id=customer_id, **args),
                           ids=args["invoice"])


@app.get("/customers/<int:customer_id>/delivery.pdf")
@need("admin")
def customer_delivery_pdf(customer_id):
    customer, data, _ = delivery_request(customer_id)
    if not data["invoices"]:
        abort(404)
    filename = "delivery-" + (re.sub(r"[^A-Za-z0-9]+", "-", customer["name"]).strip("-") or "customer") + f"-{data['day']}.pdf"
    return Response(delivery_pdf(customer, data), mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/customers/<int:customer_id>/delivery/collected")
@need("admin")
def customer_delivery_collected(customer_id):
    """The goods on a delivery list have reached the customer: the sales on it that were waiting to be collected are marked collected."""
    db, biz = get_db(), g.user["business_id"]
    customer = get_customer(db, customer_id)
    ids = request.form.getlist("invoice", type=int)
    data = delivery_data(db, customer, ids)
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    for inv in data["waiting"]:
        db.execute("UPDATE invoices SET collected_at=?, collected_by=? WHERE id=? AND business_id=? AND collected_at IS NULL", (stamp, g.user["username"], inv["id"], biz))
    db.commit()
    flash(f"{len(data['waiting'])} sale{'s' if len(data['waiting']) != 1 else ''} marked as collected." if data["waiting"] else "Nothing on this list was waiting to be collected.", "ok")
    return redirect(url_for("customer_delivery", customer_id=customer_id, invoice=ids))


def delivery_pdf(customer, d):
    """The delivery note on A4, as PDF bytes."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    base = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=base["Normal"], fontSize=8, leading=10)
    centre = ParagraphStyle("centre", parent=base["Normal"], alignment=1, fontSize=9, leading=12)
    title = ParagraphStyle("title", parent=base["Title"], fontSize=16, spaceAfter=2)
    heading = ParagraphStyle("heading", parent=base["Heading4"], fontSize=10, spaceBefore=8, spaceAfter=3)
    grey, line = colors.HexColor("#eeeeee"), colors.HexColor("#bbbbbb")

    story = [Paragraph(escape(g.user["business_name"]), title)]
    for extra in (g.user["subtitle"], g.user["contact"]):
        if extra:
            story.append(Paragraph(escape(extra), centre))
    story += [Spacer(1, 4), Paragraph("<b>DELIVERY NOTE</b>", centre), Spacer(1, 8)]
    story.append(Table(
        [
            ["Customer", Paragraph(escape(customer["name"]), small), "Delivery date", d["day"]],
            ["Phone", customer["phone"] or "-", "Invoices", ", ".join(inv["invoice_no"] for inv in d["invoices"])[:90]],
        ],
        colWidths=[24 * mm, 66 * mm, 28 * mm, 62 * mm],
        style=TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TEXTCOLOR", (0, 0), (0, -1), colors.grey),
            ("TEXTCOLOR", (2, 0), (2, -1), colors.grey), ("BOX", (0, 0), (-1, -1), 0.4, line),
        ]),
    ))

    style = TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8), ("LEADING", (0, 0), (-1, -1), 10), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), grey), ("LINEBELOW", (0, 0), (-1, -1), 0.4, line), ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ])
    for inv in d["invoices"]:
        rows = [["Code", "Item", "Qty", "Unit price", "Total"]]
        rows += [[Paragraph(escape(line["item_code"] or "-"), small), Paragraph(escape(line["item_name"] + (f" (less {money(line['discount'])} discount)" if line["discount"] else "")), small),
                  f"{line['qty']:,}", money(line["unit_price"]), money(line["line_total"])] for line in inv["lines"]]
        rows.append(["", "Invoice total", "", "", money(inv["total"])])
        head = f"{inv['invoice_no']} - {inv['created_at'][:10]} - {inv['payment_status']}" + (f" - balance due {money(inv['balance_due'])}" if inv["balance_due"] > 0.005 else "")
        story.append(KeepTogether([Paragraph(escape(head), heading), Table(rows, colWidths=[42 * mm, 68 * mm, 18 * mm, 26 * mm, 26 * mm], style=style)]))

    story.append(Spacer(1, 8))
    story.append(Table(
        [["Items delivered", f"{d['units']:,}"], ["Total of these invoices", money(d["total"])], ["Paid", money(d["paid"])], ["Balance due", money(d["owed"])]],
        colWidths=[130 * mm, 50 * mm],
        style=TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("LINEABOVE", (0, 0), (-1, 0), 0.8, colors.black),
                          ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("LINEBELOW", (0, -1), (-1, -1), 0.8, colors.black)]),
    ))
    story.append(Spacer(1, 18))
    sign = [["Delivered by", "", "Received by", ""], ["Name", "", "Name", ""], ["Signature", "", "Signature", ""], ["Date", "", "Date", ""]]
    story.append(Table(sign, colWidths=[24 * mm, 64 * mm, 24 * mm, 68 * mm], rowHeights=[14, 22, 22, 22], style=TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("TEXTCOLOR", (0, 1), (0, -1), colors.grey),
        ("TEXTCOLOR", (2, 1), (2, -1), colors.grey), ("LINEBELOW", (1, 1), (1, -1), 0.4, colors.black), ("LINEBELOW", (3, 1), (3, -1), 0.4, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
    ])))

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 9 * mm, f"Delivery note for {customer['name']} - printed {d['printed']}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"Delivery note - {customer['name']}", author=g.user["business_name"])
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def statement_pdf(customer, data):
    """Lays the statement out on A4 pages and returns the PDF as bytes."""
    # Imported here so the PDF library only loads when a statement is actually downloaded
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    base = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=base["Normal"], fontSize=8, leading=10)
    centre = ParagraphStyle("centre", parent=base["Normal"], alignment=1, fontSize=9, leading=12)
    title = ParagraphStyle("title", parent=base["Title"], fontSize=16, spaceAfter=2)
    heading = ParagraphStyle("heading", parent=base["Heading3"], fontSize=11, spaceBefore=10, spaceAfter=4)
    grey, line = colors.HexColor("#eeeeee"), colors.HexColor("#bbbbbb")

    def cell(value, style=small):
        return Paragraph(escape(str(value)), style)

    def amount(value, blank_zero=False):
        return "" if blank_zero and not value else money(value)

    def table(rows, widths, numeric_from, bold_last=False):
        """A ruled table whose first row is the header; columns from numeric_from onward are right-aligned."""
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 8), ("LEADING", (0, 0), (-1, -1), 10), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("BACKGROUND", (0, 0), (-1, 0), grey),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, line), ("ALIGN", (numeric_from, 0), (-1, -1), "RIGHT"),
        ]
        if bold_last:
            style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
        return Table(rows, colWidths=[w * mm for w in widths], repeatRows=1, style=TableStyle(style))

    summary = data["summary"]
    story = [Paragraph(escape(g.user["business_name"]), title)]
    for extra in (g.user["subtitle"], g.user["contact"]):
        if extra:
            story.append(Paragraph(escape(extra), centre))
    story += [
        Spacer(1, 4), Paragraph("<b>CUSTOMER STATEMENT</b>", centre), Spacer(1, 8),
        Table(
            [
                ["Customer", cell(customer["name"]), "Invoices", str(summary["invoices"])],
                ["Phone", cell(customer["phone"] or "-"), "Total bought", money(summary["total"])],
                ["Customer since", (customer["created_at"] or "")[:10] or "-", "Total paid", money(summary["paid"])],
                ["Statement date", data["printed"], "Balance owed", money(summary["owed"])],
            ],
            colWidths=[28 * mm, 62 * mm, 30 * mm, 60 * mm],
            style=TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 9), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.grey), ("TEXTCOLOR", (2, 0), (2, -1), colors.grey),
                ("ALIGN", (3, 0), (3, -1), "RIGHT"), ("FONTNAME", (2, 3), (3, 3), "Helvetica-Bold"),
                ("BOX", (0, 0), (-1, -1), 0.4, line),
            ]),
        ),
        Paragraph("Statement of account", heading),
    ]

    ledger = [["Date / time", "Entry", "Details", "Charged", "Paid", "Balance owed"]]
    for e in data["entries"]:
        ledger.append([e["at"], cell(e["what"]), cell(e["detail"]), amount(e["charge"], True), amount(e["paid"], True), money(e["balance"])])
    if data["entries"]:
        ledger.append(["Totals", "", "", money(summary["total"]), money(summary["paid"]), money(summary["owed"])])
        story.append(table(ledger, (30, 34, 50, 22, 22, 22), 3, bold_last=True))
    else:
        story.append(Paragraph("No purchases yet.", small))

    if data["products"]:
        story.append(Paragraph("What they have bought", heading))
        bought = [["Code", "Product", "Quantity", "Value", "Last bought"]]
        for p in data["products"]:
            bought.append([cell(p["code"] or "-"), cell(p["name"]), str(p["qty"]), money(p["value"]), p["last"]])
        story.append(table(bought, (30, 78, 20, 28, 24), 2))

    if data["invoices"]:
        story.append(Paragraph("Invoices in full", heading))
    for inv in data["invoices"]:
        rows = [["Code", "Item", "Qty", "Unit price", "Total"]]
        for item in inv["lines"]:
            rows.append([cell(item["item_code"] or "-"), cell(item["item_name"] + (f" (less {money(item['discount'])} discount)" if item.get("discount") else "")),
                         str(item["qty"]), money(item["unit_price"]), money(item["line_total"])])
        rows += [
            ["", "", "", "Invoice total", money(inv["total"])],
            ["", "", "", "Paid so far", money(inv["amount_paid"])],
            ["", "", "", "Still owed", money(inv["balance_due"])],
        ]
        caption = f"<b>{escape(inv['invoice_no'])}</b> &nbsp; {escape(inv['created_at'])} &nbsp; served by {escape(inv['created_by'] or '-')} &nbsp; <b>{escape(inv['payment_status'] or '')}</b>"
        story.append(KeepTogether([Paragraph(caption, small), Spacer(1, 2), table(rows, (30, 84, 14, 26, 26), 2), Spacer(1, 8)]))

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 9 * mm, f"{customer['name']} - statement printed {data['printed']}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        title=f"Statement - {customer['name']}", author=g.user["business_name"],
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


@app.post("/customers/<int:customer_id>/payment")
@need("admin")
def customer_payment(customer_id):
    """Spreads one lump-sum payment over the customer's unpaid invoices, oldest first. Accounts gets it as one payment."""
    db, biz = get_db(), g.user["business_id"]
    customer = get_customer(db, customer_id)
    back = redirect(url_for("customer_statement", customer_id=customer_id))
    unpaid = db.all(
        """SELECT id, total, amount_paid, balance_due FROM invoices
           WHERE customer_id=? AND business_id=? AND kind='SALE' AND balance_due > 0 ORDER BY created_at, id""",
        (customer_id, biz),
    )
    owed = round(sum(inv["balance_due"] for inv in unpaid), 2)
    try:
        amount = round(number("amount"), 2)
    except ValueError:
        amount = 0.0
    if amount <= 0 or amount > owed + 0.005:
        flash(f"Enter an amount above zero and no more than the {money(owed)} this customer owes.", "error")
        return back
    method, reference, problem = read_money_method()
    if problem:
        flash(problem, "error")
        return back

    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    note = f"Part of a {money(amount)} lump-sum payment" + (f": {text('note')[:150]}" if text("note") else "")
    remaining, settled = min(amount, owed), 0
    try:
        for inv in unpaid:
            if remaining <= 0.005:
                break
            part = min(remaining, inv["balance_due"])
            new_paid = min(inv["total"], round(inv["amount_paid"] + part, 2))
            db.execute(
                "INSERT INTO payment_log (business_id, kind, ref_id, old_paid, new_paid, note, changed_by, changed_at) VALUES (?, 'sale', ?, ?, ?, ?, ?, ?)",
                (biz, inv["id"], inv["amount_paid"], new_paid, note, g.user["username"], stamp),
            )
            db.execute(
                "UPDATE invoices SET amount_paid=?, balance_due=?, payment_status=? WHERE id=? AND business_id=?",
                (new_paid, round(inv["total"] - new_paid, 2), payment_status_for(inv["total"], new_paid), inv["id"], biz),
            )
            remaining = round(remaining - part, 2)
            settled += 1
        record_money(
            db, biz, "in", min(amount, owed), method, "customer_payment", reference, customer["name"],
            f"Payment on account, spread over {settled} invoice(s)" + (f": {text('note')[:120]}" if text("note") else ""), "customer", customer_id, stamp,
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The payment could not be saved. Please try again.", "error")
        return back

    flash(f"Recorded {money(amount)} across {settled} invoice(s), oldest first. Balance now owed: {money(owed - min(amount, owed))}.", "ok")
    return back


@app.post("/customers/<int:customer_id>/update")
@need("admin")
def update_customer(customer_id):
    db, biz = get_db(), g.user["business_id"]
    get_customer(db, customer_id)
    name, phone = text("name"), text("phone")
    nkey, pkey = name_key(name), phone_key(phone)
    clash = db.one("SELECT id FROM customers WHERE business_id=? AND name_key=? AND phone_key=? AND id<>?", (biz, nkey, pkey, customer_id))
    if not nkey:
        flash("The customer needs a name.", "error")
    elif clash:
        flash("Another customer account already has that name and phone number.", "error")
    else:
        db.execute("UPDATE customers SET name=?, phone=?, name_key=?, phone_key=? WHERE id=? AND business_id=?", (name, phone, nkey, pkey, customer_id, biz))
        db.commit()
        flash("Customer details saved. Invoices already issued keep the name and phone printed on them.", "ok")
    return redirect(url_for("customer_statement", customer_id=customer_id))


DUPLICATE_SCAN_LIMIT = 3000
DUPLICATES_SHOWN = 100
CUSTOMER_COLUMNS = "id, name, phone, name_key, phone_key, created_at"


@functools.lru_cache(maxsize=20000)
def letter_counts(key):
    return collections.Counter(key)


def names_alike(a, b):
    """True when two name keys look like the same person: reordered, a small typo, or an initial for a full name."""
    words_a, words_b = a.split(), b.split()
    if sorted(words_a) == sorted(words_b):
        return True
    # A typo-level match (difflib's ratio of 0.86 or more) needs at least that share of letters in common, so most pairs are told
    # apart by counting letters, without the full comparison. The count can only be higher than the ratio, never lower.
    common = sum((letter_counts(a) & letter_counts(b)).values())
    if 2.0 * common / ((len(a) + len(b)) or 1) >= 0.86 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.86:
        return True
    if len(words_a) != len(words_b) or len(words_a) < 2:
        return False
    whole_matches = 0
    for x, y in zip(words_a, words_b):
        x, y = x.strip("."), y.strip(".")
        if x == y:
            whole_matches += 1
        elif not ((len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y))):
            return False
    return whole_matches >= 1


def pair_key(a, b):
    return (min(a, b), max(a, b))


def distinct_pairs(db, biz):
    return {(r["low_id"], r["high_id"]) for r in db.all("SELECT low_id, high_id FROM customer_distinct WHERE business_id=?", (biz,))}


def find_duplicates(db, biz):
    """Pairs of customer accounts that may be the same person, strongest evidence first."""
    people = db.all(
        """SELECT c.id, c.name, c.phone, c.name_key, c.phone_key, c.created_at,
                  COUNT(i.id) AS invoices, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.balance_due), 0) AS owed
           FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id AND i.kind='SALE' AND i.payment_status <> 'Cancelled'
           WHERE c.business_id=? GROUP BY c.id, c.name, c.phone, c.name_key, c.phone_key, c.created_at ORDER BY c.id LIMIT ?""",
        (biz, DUPLICATE_SCAN_LIMIT),
    )
    return duplicate_pairs(people, distinct_pairs(db, biz))


# Matching every account against the others takes a while once there are thousands, and the Customers page shows the count on every
# visit. The answer only changes when a name, a phone number or an account does, or a pair is marked different, so the last few
# answers are kept, keyed by exactly that.
_matches_seen = collections.OrderedDict()


def duplicate_pairs(people, ruled_out):
    """Pairs of accounts that may be the same party, strongest evidence first. Shared by customers and suppliers.

    people are dicts with id, name_key and phone_key; ruled_out holds pair_key pairs an admin said are different.
    """
    by_id = {p["id"]: p for p in people}
    key = (tuple((p["id"], p["name_key"], p["phone_key"]) for p in people), frozenset(ruled_out))
    found = _matches_seen.get(key)
    if found is None:
        found = matching_pairs(people, by_id, ruled_out)
        _matches_seen[key] = found
        while len(_matches_seen) > 8:
            _matches_seen.popitem(last=False)
    pairs = [{"a": by_id[low], "b": by_id[high], "reason": reason, "rank": rank} for (low, high), (rank, reason) in found.items()]
    pairs.sort(key=lambda pair: (pair["rank"], pair["a"]["name_key"], pair["a"]["id"]))
    return pairs


def matching_pairs(people, by_id, ruled_out):
    """{pair_key: (rank, reason)} for every pair of accounts that may be the same party."""
    found = {}

    def suggest(a, b, rank, reason):
        key = pair_key(a, b)
        if a != b and key not in ruled_out and (key not in found or rank < found[key][0]):
            found[key] = (rank, reason)

    same = {}
    for p in people:
        if p["phone_key"]:
            same.setdefault(("phone", p["phone_key"]), []).append(p["id"])
        if p["name_key"]:
            same.setdefault(("name", p["name_key"]), []).append(p["id"])
    for (kind, _), ids in same.items():
        ids = ids[:30]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if kind == "phone":
                    suggest(a, b, 0, "Same phone number")
                else:
                    suggest(a, b, 1, "Same name, different phone numbers")

    # Comparing every name with every other is too slow, so only compare names that share a word or their first letters
    buckets = {}
    for p in people:
        if p["name_key"]:
            for word in set(p["name_key"].split()) | {p["name_key"][:3]}:
                if len(word) >= 2:
                    buckets.setdefault(word, []).append(p["id"])
    compared = set()   # names sharing two words meet in two buckets; each pair is compared once
    for ids in buckets.values():
        if len(ids) > 150:
            continue
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                key = pair_key(a, b)
                if key in found or key in compared:
                    continue
                compared.add(key)
                if names_alike(by_id[a]["name_key"], by_id[b]["name_key"]):
                    both_have_phones = by_id[a]["phone_key"] and by_id[b]["phone_key"]
                    suggest(a, b, 3 if both_have_phones else 2, "Similar name, different phone numbers" if both_have_phones else "Similar name")
    return found


def phone_twins(db, biz, customer_id):
    """Other accounts carrying this customer's phone number, unless an admin already said they are different people."""
    others = db.all(
        """SELECT o.id, o.name, o.phone FROM customers o JOIN customers me ON me.id=? AND me.business_id=o.business_id AND me.phone_key=o.phone_key
           WHERE o.business_id=? AND o.id<>? AND me.phone_key <> '' ORDER BY o.id""",
        (customer_id, biz, customer_id),
    )
    if not others:
        return []
    ruled_out = distinct_pairs(db, biz)
    return [other for other in others if pair_key(customer_id, other["id"]) not in ruled_out]


@app.get("/customers/duplicates")
@need("admin")
def customer_duplicates():
    pairs = find_duplicates(get_db(), g.user["business_id"])
    return render_template("duplicates.html", pairs=pairs[:DUPLICATES_SHOWN], total=len(pairs))


def customer_pair_from_form(db, biz, first, second):
    a = db.one(f"SELECT {CUSTOMER_COLUMNS} FROM customers WHERE id=? AND business_id=?", (request.form.get(first, type=int), biz))
    b = db.one(f"SELECT {CUSTOMER_COLUMNS} FROM customers WHERE id=? AND business_id=?", (request.form.get(second, type=int), biz))
    return (a, b) if a and b and a["id"] != b["id"] else (None, None)


def back_from_duplicates():
    back_to = request.form.get("back_to", type=int)
    return redirect(url_for("customer_statement", customer_id=back_to) if back_to else url_for("customer_duplicates"))


@app.post("/customers/merge")
@need("admin")
def merge_customers():
    db, biz = get_db(), g.user["business_id"]
    keep, remove = customer_pair_from_form(db, biz, "keep", "remove")
    if keep is None:
        flash("Those two accounts could not be found. One may already have been merged.", "error")
        return redirect(url_for("customer_duplicates"))

    try:
        moved = db.execute("UPDATE invoices SET customer_id=? WHERE customer_id=? AND business_id=?", (keep["id"], remove["id"], biz)).rowcount
        # Payments made on the removed account are in Accounts against it; they belong to the kept account now
        db.execute(
            "UPDATE cashbook SET source_id=?, party=? WHERE business_id=? AND source='customer' AND source_id=?", (keep["id"], keep["name"], biz, remove["id"]),
        )
        # The removed account's spelling, and any spellings merged into it earlier, now lead to the kept account
        db.execute("UPDATE customer_aliases SET customer_id=? WHERE customer_id=? AND business_id=?", (keep["id"], remove["id"], biz))
        db.execute("DELETE FROM customer_aliases WHERE business_id=? AND name_key=? AND phone_key=?", (biz, remove["name_key"], remove["phone_key"]))
        db.execute(
            "INSERT INTO customer_aliases (business_id, name_key, phone_key, customer_id) VALUES (?, ?, ?, ?)",
            (biz, remove["name_key"], remove["phone_key"], keep["id"]),
        )
        db.execute("DELETE FROM customer_distinct WHERE business_id=? AND (low_id=? OR high_id=?)", (biz, remove["id"], remove["id"]))
        db.execute("DELETE FROM customers WHERE id=? AND business_id=?", (remove["id"], biz))

        since = min(keep["created_at"] or "9999", remove["created_at"] or "9999")
        phone, pkey = keep["phone"], keep["phone_key"]
        if not pkey and remove["phone_key"]:
            taken = db.one(
                "SELECT id FROM customers WHERE business_id=? AND name_key=? AND phone_key=? AND id<>?",
                (biz, keep["name_key"], remove["phone_key"], keep["id"]),
            )
            if not taken:
                phone, pkey = remove["phone"], remove["phone_key"]
        db.execute(
            "UPDATE customers SET phone=?, phone_key=?, created_at=? WHERE id=? AND business_id=?",
            (phone, pkey, None if since == "9999" else since, keep["id"], biz),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The merge could not be saved, so nothing was changed. Please try again.", "error")
        return redirect(url_for("customer_duplicates"))

    flash(f"Merged '{remove['name']}' into '{keep['name']}': {moved} invoice(s) moved onto this statement.", "ok")
    return redirect(url_for("customer_statement", customer_id=keep["id"]))


@app.post("/customers/distinct")
@need("admin")
def mark_customers_distinct():
    db, biz = get_db(), g.user["business_id"]
    a, b = customer_pair_from_form(db, biz, "a", "b")
    if a is not None:
        low, high = pair_key(a["id"], b["id"])
        if not db.one("SELECT id FROM customer_distinct WHERE business_id=? AND low_id=? AND high_id=?", (biz, low, high)):
            db.execute("INSERT INTO customer_distinct (business_id, low_id, high_id) VALUES (?, ?, ?)", (biz, low, high))
            db.commit()
        flash(f"Noted: '{a['name']}' and '{b['name']}' are different customers. They will not be suggested again.", "ok")
    return back_from_duplicates()


def link_existing_customers():
    """Opens accounts for named customers on invoices issued before customer accounts existed."""
    db = Database()
    try:
        unlinked = db.all(
            """SELECT id, business_id, customer_name, customer_phone, created_at FROM invoices
               WHERE customer_id IS NULL AND kind='SALE' AND NOT (customer_name = ? AND (customer_phone IS NULL OR customer_phone = ''))
               ORDER BY id""",
            (WALK_IN,),
        )
        for inv in unlinked:
            customer_id = customer_for(db, inv["business_id"], inv["customer_name"], inv["customer_phone"], inv["created_at"])
            if customer_id:
                db.execute("UPDATE invoices SET customer_id=? WHERE id=?", (customer_id, inv["id"]))
        db.commit()
    except DB_ERRORS:
        db.rollback()
    finally:
        db.close()



# --- SUPPLIER STATEMENTS (ADMIN) ---
# A supplier account is opened from the supplier named on a purchase, the way a customer account is opened from a sale. Every
# payment to a supplier is kept with how it was paid and the receipt or reference number. A payment entered on a statement is
# spread over the supplier's unpaid purchases, oldest first.
# The ways money is paid, offered wherever a payment is entered: each one lands in one of the balances kept in Accounts (cash at
# hand, the bank, the Airtel Money and MTN Money wallets), except Other.
PAYMENT_METHODS = (
    ("Cash", "Receipt number"), ("Bank transfer", "Transaction reference"), ("Airtel Money", "Transaction ID"), ("MTN Money", "Transaction ID"),
    ("Cheque", "Cheque number"), ("Card", "Card slip number"), ("Other", "Reference number"),
)
# Before the two wallets were told apart, mobile money was one method. Records that say so stay valid, but it is no longer offered.
LEGACY_MOBILE = "Mobile money"
METHOD_LABELS = dict(PAYMENT_METHODS + ((LEGACY_MOBILE, "Transaction ID"),))
METHOD_NAMES = [method for method, _ in PAYMENT_METHODS]
NOT_RECORDED = "Not recorded"
METHOD_ALIASES = {
    "bank": "Bank transfer", "transfer": "Bank transfer", "eft": "Bank transfer", "bank deposit": "Bank transfer",
    "airtel": "Airtel Money", "airtel mobile money": "Airtel Money", "mtn": "MTN Money", "momo": "MTN Money", "mtn momo": "MTN Money",
    "mtn mobile money": "MTN Money", "mobile": LEGACY_MOBILE, "check": "Cheque", "credit card": "Card", "debit card": "Card",
}


def method_from_text(raw):
    """A payment method written in a file or form, matched to one of the known ones; None when it is not recognised."""
    key = " ".join((raw or "").lower().split())
    for method in METHOD_LABELS:
        if method.lower() == key:
            return method
    return METHOD_ALIASES.get(key)


def supplier_key(name):
    return " ".join((name or "").lower().split())


def supplier_for(db, biz, name, phone, when):
    """Finds or opens the supplier account for a purchase. Returns None when no supplier is named."""
    name, phone = (name or "").strip(), (phone or "").strip()
    nkey, pkey = supplier_key(name), phone_key(phone)
    if not nkey:
        return None
    exact = db.one("SELECT id FROM suppliers WHERE business_id=? AND name_key=? AND phone_key=?", (biz, nkey, pkey))
    if exact:
        return exact["id"]
    merged = db.one(
        """SELECT s.id FROM supplier_aliases a JOIN suppliers s ON s.id = a.supplier_id
           WHERE a.business_id=? AND a.name_key=? AND a.phone_key=?""",
        (biz, nkey, pkey),
    )
    if merged:
        return merged["id"]
    same = db.all("SELECT id, phone_key FROM suppliers WHERE business_id=? AND name_key=? LIMIT 2", (biz, nkey))
    if len(same) == 1:
        # No phone number given: the one account with this name. A phone number given for an account that had none is added to it.
        if not pkey:
            return same[0]["id"]
        if not same[0]["phone_key"]:
            db.execute("UPDATE suppliers SET phone=?, phone_key=? WHERE id=?", (phone, pkey, same[0]["id"]))
            return same[0]["id"]
    return db.insert(
        "INSERT INTO suppliers (business_id, name, phone, name_key, phone_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (biz, name, phone, nkey, pkey, when),
    )


def read_payment_details(strict):
    """Reads the payment method and its reference number from the form. Returns (method, reference, None) or (None, None, what is wrong).

    A reference is needed with every method; for cash the receipt number is enough. A form that sends no method at all
    (an older client) is accepted as 'Not recorded' unless strict.
    """
    if "payment_method" not in request.form and not strict:
        return NOT_RECORDED, "", None
    method = method_from_text(text("payment_method"))
    if method is None:
        return None, None, "Choose how it was paid: " + ", ".join(METHOD_NAMES) + "."
    reference = " ".join(text("payment_reference").split())[:60]
    if not reference:
        label = METHOD_LABELS[method]
        return None, None, f"Enter the {label.lower()} for this {method.lower()} payment." + (" For cash the receipt number is enough." if method == "Cash" else "")
    return method, reference, None


def add_supplier_payment(db, biz, supplier_id, amount, method, reference, paid_on, note, allocations, apply=True):
    """Records a payment to a supplier and the purchases it settled. The caller commits.

    allocations is a list of (procurement id, amount). With apply, each purchase's amount paid is raised by its share and the
    change is written to its payment history; without it (a purchase logged already paid) only the record is made.
    A negative amount is a correction.
    """
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    payment_id = db.insert(
        """INSERT INTO supplier_payments (business_id, supplier_id, amount, method, reference, paid_on, note, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (biz, supplier_id, round(amount, 2), method, reference, paid_on, note, g.user["username"], stamp),
    )
    how = method + (f", {METHOD_LABELS[method].lower()} {reference}" if reference and method in METHOD_LABELS else "")
    for procurement_id, part in allocations:
        if apply:
            row = db.one("SELECT COALESCE(amount_paid, total_cost) AS paid FROM procurements WHERE id=? AND business_id=?", (procurement_id, biz))
            new_paid = round(row["paid"] + part, 2)
            db.execute("UPDATE procurements SET amount_paid=? WHERE id=? AND business_id=?", (new_paid, procurement_id, biz))
            db.execute(
                """INSERT INTO payment_log (business_id, kind, ref_id, old_paid, new_paid, note, changed_by, changed_at, payment_id)
                   VALUES (?, 'purchase', ?, ?, ?, ?, ?, ?, ?)""",
                (biz, procurement_id, row["paid"], new_paid, (how + (f": {note}" if note else ""))[:200], g.user["username"], stamp, payment_id),
            )
        db.execute(
            "INSERT INTO supplier_payment_allocations (business_id, payment_id, procurement_id, amount) VALUES (?, ?, ?, ?)",
            (biz, payment_id, procurement_id, round(part, 2)),
        )
    supplier = db.one("SELECT name FROM suppliers WHERE id=? AND business_id=?", (supplier_id, biz))
    record_money(
        db, biz, "out", amount, method, "supplier", reference, supplier["name"] if supplier else "", note or "Payment to supplier",
        "supplier_payment", payment_id, stamp_for_day(paid_on),
    )
    return payment_id


# --- ACCOUNTS: THE CASHBOOK ---
# Every movement of money is written to the cashbook when it happens, by the page it happens on, in the same transaction:
#   in:  a sale paid at the till or when a pre-order is collected, a later payment on an invoice or on a customer's account,
#        and other income entered on the Accounts page;
#   out: payments to suppliers (when a purchase is logged, on its payment page, or on the supplier's statement), purchases
#        with no supplier account, salaries and NAPSA/NHIMA from payslips, refunds on cancelled invoices, and other expenses
#        entered on the Accounts page.
# A payment lowered later is a negative entry in the same direction with the method "Correction", so nothing is rewritten.
# The Accounts page reads from here alone.
CORRECTION = "Correction"
MONEY_IN = (
    ("sale", "Sales paid at the till"),
    ("customer_payment", "Payments on credit sales"),
    ("other_income", "Other income"),
    ("capital", "Owner's money put in"),
    ("loan_in", "Loans received"),
)
MONEY_OUT = (
    ("supplier", "Payments to suppliers"),
    ("purchase", "Purchases (no supplier account)"),
    ("salaries", "Salaries (net pay)"),
    ("statutory", "NAPSA & NHIMA"),
    ("refund", "Refunds to customers"),
    ("rent", "Rent"),
    ("utilities", "Electricity, water & internet"),
    ("transport", "Transport & fuel"),
    ("repairs", "Repairs & maintenance"),
    ("charges", "Bank & mobile money charges"),
    ("tax", "Taxes, levies & licences"),
    ("loan_out", "Loan repayments"),
    ("drawings", "Owner's drawings"),
    ("other_expense", "Other expenses"),
)
# Entered by hand on the Accounts page. The other categories are written only by the pages they belong to.
MANUAL_IN = ("other_income", "capital", "loan_in")
MANUAL_OUT = ("rent", "utilities", "transport", "repairs", "charges", "tax", "loan_out", "drawings", "other_expense")
CATEGORY_LABELS = dict(MONEY_IN + MONEY_OUT)
# Where money is held: cash, the bank (cheques and card slips are paid through it) or mobile money
CHANNELS = (
    ("Cash", ("Cash",)), ("Bank", ("Bank transfer", "Cheque", "Card")), ("Airtel Money", ("Airtel Money",)), ("MTN Money", ("MTN Money",)),
    ("Mobile money (wallet not said)", (LEGACY_MOBILE,)), ("Other", ("Other",)), (NOT_RECORDED, (NOT_RECORDED,)), ("Corrections", (CORRECTION,)),
)
CHANNEL_OF = {method: channel for channel, methods in CHANNELS for method in methods}
# The balances kept: (channel, what it is called). Money through Other, or that does not say where it went, is in none of them.
BALANCE_ACCOUNTS = (("Cash", "Cash at hand"), ("Bank", "Bank"), ("Airtel Money", "Airtel Money"), ("MTN Money", "MTN Money"))
BALANCE_FIELDS = {"Cash": "balance_cash", "Bank": "balance_bank", "Airtel Money": "balance_airtel", "MTN Money": "balance_mtn"}
BALANCE_METHODS = [method for channel, _ in BALANCE_ACCOUNTS for method in dict(CHANNELS)[channel]]
# Entries that do not say which balance they belong to, and can be assigned to one in Accounts
UNASSIGNED_METHODS = (NOT_RECORDED, LEGACY_MOBILE, CORRECTION)


def stamp_for_day(day):
    """A full timestamp for money dated by day only: now when it is today (so it sorts after today's earlier entries), else that midnight."""
    if not day or day[:10] == today_str():
        return now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{day[:10]} 00:00:00"


def record_money(db, biz, direction, amount, method, category, reference="", party="", description="", source="", source_id=None, when=None, by=None):
    """Writes one movement of money to the cashbook. The caller commits, so the entry stands or falls with what it records.

    amount is what moved; a negative amount corrects an earlier entry in the same direction. Nothing is written for zero.
    """
    amount = round(amount or 0.0, 2)
    if abs(amount) < 0.005:
        return None
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    return db.insert(
        """INSERT INTO cashbook (business_id, direction, amount, method, reference, category, party, description, source, source_id, occurred_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            biz, direction, amount, method or NOT_RECORDED, (reference or "")[:60], category, (party or "")[:100], (description or "")[:200],
            source, source_id, when or stamp, by or g.user["username"], stamp,
        ),
    )


def read_money_method(required=True):
    """How money moved, from a payment form: (method, reference, problem). The reference (receipt number, transaction ID) is optional.

    A form without the method field at all (an older page, or nothing being paid) gives 'Not recorded'. A method field sent
    blank is a problem to show, unless not required (then it is 'Not recorded' too); something unknown always is.
    """
    if "payment_method" not in request.form or (not required and not text("payment_method")):
        return NOT_RECORDED, "", None
    method = method_from_text(text("payment_method"))
    if method is None:
        return None, None, "Choose how the money was paid: " + ", ".join(METHOD_NAMES) + "."
    return method, " ".join(text("payment_reference").split())[:60], None


def read_correction_method():
    """Which balance a lowered payment is taken back from: (method, reference, problem). An older page without the field gives
    'Correction', which belongs to no balance until it is assigned in Accounts."""
    if "payment_method" not in request.form:
        return CORRECTION, "", None
    method, reference, problem = read_money_method()
    if problem:
        return None, None, "Choose the account the money is taken back from: " + ", ".join(METHOD_NAMES) + "."
    return method, reference, None


def money_lines(db, biz, source, source_id):
    """The cashbook entries belonging to one invoice, purchase or payslip, oldest first."""
    return db.all(
        """SELECT direction, amount, method, reference, category, occurred_at FROM cashbook
           WHERE business_id=? AND source=? AND source_id=? ORDER BY occurred_at, id""",
        (biz, source, source_id),
    )


def record_payslip_money(db, biz, slip_id, employee, month, net, statutory, method, when, by=None):
    """A payslip's pay leaves the business as two entries: net pay to the employee, and NAPSA/NHIMA (both shares) to the authorities."""
    record_money(db, biz, "out", net, method, "salaries", "", employee, f"Pay for {month}", "payslip", slip_id, when, by)
    record_money(db, biz, "out", statutory, method, "statutory", "", employee, f"NAPSA & NHIMA for {month}", "payslip", slip_id, when, by)


@app.get("/suppliers")
@need("admin")
def suppliers():
    db, biz = get_db(), g.user["business_id"]
    q = request.args.get("q", "").strip()
    owing = request.args.get("balance", "") if request.args.get("balance") in ("owing", "settled") else ""
    sql = """SELECT s.id, s.name, s.phone, COUNT(pr.id) AS purchases, COALESCE(SUM(pr.total_cost), 0) AS total,
                    COALESCE(SUM(pr.total_cost - COALESCE(pr.amount_paid, pr.total_cost)), 0) AS owed
             FROM suppliers s LEFT JOIN procurements pr ON pr.supplier_id = s.id
             WHERE s.business_id=?"""
    params = [biz]
    if q:
        sql += " AND (s.name_key LIKE ?"
        params.append(f"%{supplier_key(q)}%")
        if phone_key(q):
            sql += " OR s.phone_key LIKE ?"
            params.append(f"%{phone_key(q)}%")
        sql += ")"
    sql += " GROUP BY s.id, s.name, s.phone"
    if owing == "owing":
        sql += " HAVING COALESCE(SUM(pr.total_cost - COALESCE(pr.amount_paid, pr.total_cost)), 0) > 0.005"
    elif owing == "settled":
        sql += " HAVING COALESCE(SUM(pr.total_cost - COALESCE(pr.amount_paid, pr.total_cost)), 0) <= 0.005"
    sql += " ORDER BY s.name LIMIT 100"
    rows = db.all(sql, params)
    totals = db.one(
        "SELECT COALESCE(SUM(total_cost - COALESCE(amount_paid, total_cost)), 0) AS owed FROM procurements WHERE business_id=? AND supplier_id IS NOT NULL", (biz,)
    )
    return render_template(
        "suppliers.html", results=rows, q=q, balance=owing, duplicate_count=len(find_supplier_duplicates(db, biz)), owed_all=totals["owed"],
    )


def get_supplier(db, supplier_id):
    supplier = db.one("SELECT id, name, phone, created_at FROM suppliers WHERE id=? AND business_id=?", (supplier_id, g.user["business_id"]))
    if supplier is None:
        abort(404)
    return supplier


def supplier_statement_data(db, supplier_id, start="", end=""):
    """Everything a supplier statement shows, shared by the page and the PDF. start and end (YYYY-MM-DD, either may be blank) limit what is listed."""
    biz = g.user["business_id"]
    purchases = db.all(
        """SELECT id, purchase_date, invoice_no, item_code, name, quantity, unit_cost, total_cost, COALESCE(amount_paid, total_cost) AS amount_paid,
                  COALESCE(in_inventory, 1) AS in_inventory, category, department
           FROM procurements WHERE supplier_id=? AND business_id=? ORDER BY purchase_date, id""",
        (supplier_id, biz),
    )
    payments = db.all(
        """SELECT id, amount, method, reference, paid_on, note, recorded_by, recorded_at FROM supplier_payments
           WHERE supplier_id=? AND business_id=? ORDER BY paid_on, id""",
        (supplier_id, biz),
    )
    allocations = db.all(
        """SELECT a.payment_id, a.procurement_id, a.amount FROM supplier_payment_allocations a
           JOIN supplier_payments sp ON sp.id = a.payment_id WHERE sp.supplier_id=? AND a.business_id=?""",
        (supplier_id, biz),
    )
    recorded, covered = {}, {}
    for a in allocations:
        recorded[a["procurement_id"]] = recorded.get(a["procurement_id"], 0.0) + a["amount"]
        covered.setdefault(a["payment_id"], []).append(a)
    by_id = {p["id"]: p for p in purchases}
    for p in purchases:
        p["balance"] = round(p["total_cost"] - p["amount_paid"], 2)
        p["status"] = payment_status_for(p["total_cost"], p["amount_paid"])
        p["label"] = p["invoice_no"] or p["name"]

    # The statement: every purchase as a charge and every payment as a credit, in date order with a running balance. Whatever was
    # paid on a purchase before payments were recorded individually shows as one payment with no method.
    entries = []
    for p in purchases:
        what = f"{p['quantity']} x {p['name']}" + (f" (invoice {p['invoice_no']})" if p["invoice_no"] else "")
        entries.append({"at": p["purchase_date"] or "", "order": 0, "id": p["id"], "what": "Purchase", "detail": what, "charge": p["total_cost"], "paid": 0.0, "method": "", "reference": ""})
        loose = round(p["amount_paid"] - recorded.get(p["id"], 0.0), 2)
        if abs(loose) > 0.005:
            entries.append({
                "at": p["purchase_date"] or "", "order": 1, "id": p["id"], "what": "Payment" if loose > 0 else "Payment correction", "charge": 0.0, "paid": loose,
                "detail": f"{p['label']}: paid before payments were recorded one by one", "method": NOT_RECORDED, "reference": "",
            })
    pay_rows = []
    for sp in payments:
        covers = ", ".join(
            f"{by_id[a['procurement_id']]['label'] if a['procurement_id'] in by_id else 'a purchase'} ({money(a['amount'])})" for a in covered.get(sp["id"], [])
        )
        pay_rows.append(dict(sp, covers=covers))
        reference = (METHOD_LABELS.get(sp["method"], "Reference") + " " + sp["reference"]) if sp["reference"] else ""
        detail = " · ".join(part for part in (sp["method"], reference, covers, sp["note"]) if part)
        entries.append({
            "at": sp["paid_on"] or "", "order": 1, "id": sp["id"], "what": "Payment" if sp["amount"] > 0 else "Payment correction", "detail": detail,
            "charge": 0.0, "paid": sp["amount"], "method": sp["method"], "reference": sp["reference"],
        })
    entries.sort(key=lambda e: (e["at"], e["order"], e["id"]))

    balance, opening, shown = 0.0, 0.0, []
    for e in entries:
        balance = round(balance + e["charge"] - e["paid"], 2)
        e["balance"] = balance
        if start and e["at"] < start:
            opening = balance
        elif not end or e["at"] <= end:
            shown.append(e)
    by_method = {}
    for e in shown:
        if e["paid"]:
            m = by_method.setdefault(e["method"], {"method": e["method"], "count": 0, "amount": 0.0})
            m["count"] += 1
            m["amount"] = round(m["amount"] + e["paid"], 2)
    in_period = [p for p in pay_rows if (not start or (p["paid_on"] or "") >= start) and (not end or (p["paid_on"] or "") <= end)]

    summary = {
        "purchases": len(purchases), "total": round(sum(p["total_cost"] for p in purchases), 2),
        "paid": round(sum(p["amount_paid"] for p in purchases), 2), "owed": round(sum(p["balance"] for p in purchases), 2),
    }
    return {
        "purchases": purchases, "entries": shown, "payments": sorted(in_period, key=lambda p: (p["paid_on"] or "", p["id"]), reverse=True),
        "by_method": sorted(by_method.values(), key=lambda m: -m["amount"]), "summary": summary, "printed": now().strftime("%d %b %Y %H:%M"),
        "period": {
            "start": start, "end": end, "on": bool(start or end), "opening": opening,
            "charged": round(sum(e["charge"] for e in shown), 2), "paid": round(sum(e["paid"] for e in shown), 2),
        },
    }


def statement_period():
    """The from/to dates of a statement filter; a value that is not a date is ignored."""
    start = day_or_none(request.args.get("from", "")) if request.args.get("from") else None
    end = day_or_none(request.args.get("to", "")) if request.args.get("to") else None
    if start and end and start > end:
        start, end = end, start
    return start or "", end or ""


@app.get("/suppliers/<int:supplier_id>")
@need("admin")
def supplier_statement(supplier_id):
    db, biz = get_db(), g.user["business_id"]
    start, end = statement_period()
    data = supplier_statement_data(db, supplier_id, start, end)
    # What a new payment would settle, oldest first, for the preview in the Record Payment overlay
    unpaid = [
        {"label": f"{p['purchase_date'] or 'No date'} · " + (f"{p['invoice_no']} · {p['name']}" if p["invoice_no"] else p["name"]), "owed": p["balance"]}
        for p in data["purchases"] if p["balance"] > 0.005
    ]
    return render_template(
        "supplier.html", supplier=get_supplier(db, supplier_id), twins=supplier_twins(db, biz, supplier_id), methods=PAYMENT_METHODS, methods_labels=METHOD_LABELS,
        today=today_str(), start=start, end=end, unpaid=unpaid, **data,
    )


@app.get("/suppliers/<int:supplier_id>/statement.pdf")
@need("admin")
def supplier_statement_pdf(supplier_id):
    db = get_db()
    supplier = get_supplier(db, supplier_id)
    start, end = statement_period()
    pdf = supplier_pdf(supplier, supplier_statement_data(db, supplier_id, start, end))
    filename = "supplier-statement-" + (re.sub(r"[^A-Za-z0-9]+", "-", supplier["name"]).strip("-") or "supplier") + ".pdf"
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def supplier_pdf(supplier, data):
    """Lays the supplier statement out on A4 pages and returns the PDF as bytes."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    base = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=base["Normal"], fontSize=8, leading=10)
    centre = ParagraphStyle("centre", parent=base["Normal"], alignment=1, fontSize=9, leading=12)
    title = ParagraphStyle("title", parent=base["Title"], fontSize=16, spaceAfter=2)
    heading = ParagraphStyle("heading", parent=base["Heading3"], fontSize=11, spaceBefore=10, spaceAfter=4)
    grey, line = colors.HexColor("#eeeeee"), colors.HexColor("#bbbbbb")

    def cell(value):
        return Paragraph(escape(str(value)), small)

    def amount(value, blank_zero=False):
        return "" if blank_zero and not value else money(value)

    def table(rows, widths, numeric_from, bold_last=False):
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 8), ("LEADING", (0, 0), (-1, -1), 10), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("BACKGROUND", (0, 0), (-1, 0), grey),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, line), ("ALIGN", (numeric_from, 0), (-1, -1), "RIGHT"),
        ]
        if bold_last:
            style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
        return Table(rows, colWidths=[w * mm for w in widths], repeatRows=1, style=TableStyle(style))

    summary, period = data["summary"], data["period"]
    story = [Paragraph(escape(g.user["business_name"]), title)]
    for extra in (g.user["subtitle"], g.user["contact"]):
        if extra:
            story.append(Paragraph(escape(extra), centre))
    covering = f"{period['start'] or 'the start'} to {period['end'] or 'today'}" if period["on"] else "all dates"
    story += [
        Spacer(1, 4), Paragraph("<b>SUPPLIER STATEMENT</b>", centre), Spacer(1, 8),
        Table(
            [
                ["Supplier", cell(supplier["name"]), "Purchases", str(summary["purchases"])],
                ["Phone", cell(supplier["phone"] or "-"), "Total bought", money(summary["total"])],
                ["Covering", cell(covering), "Total paid", money(summary["paid"])],
                ["Statement date", data["printed"], "Balance owed", money(summary["owed"])],
            ],
            colWidths=[28 * mm, 62 * mm, 30 * mm, 60 * mm],
            style=TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 9), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.grey), ("TEXTCOLOR", (2, 0), (2, -1), colors.grey),
                ("ALIGN", (3, 0), (3, -1), "RIGHT"), ("FONTNAME", (2, 3), (3, 3), "Helvetica-Bold"), ("BOX", (0, 0), (-1, -1), 0.4, line),
            ]),
        ),
        Paragraph("Statement of account", heading),
    ]
    ledger = [["Date", "Entry", "Details", "Charged", "Paid", "Balance owed"]]
    if period["on"]:
        ledger.append(["", cell("Balance brought forward"), "", "", "", money(period["opening"])])
    for e in data["entries"]:
        ledger.append([e["at"], cell(e["what"]), cell(e["detail"]), amount(e["charge"], True), amount(e["paid"], True), money(e["balance"])])
    if data["entries"]:
        ledger.append(["Totals", "", "", money(period["charged"]), money(period["paid"]), money(summary["owed"])])
        story.append(table(ledger, (22, 26, 70, 22, 22, 22), 3, bold_last=True))
    else:
        story.append(Paragraph("No purchases or payments in this period.", small))

    if data["payments"]:
        story.append(Paragraph("Payments made", heading))
        rows = [["Date", "Method", "Reference", "Amount", "Recorded by", "Covers"]]
        for p in data["payments"]:
            rows.append([p["paid_on"] or "", cell(p["method"]), cell(p["reference"] or "-"), money(p["amount"]), cell(p["recorded_by"] or "-"), cell(p["covers"] or "-")])
        story.append(table(rows, (22, 26, 30, 22, 24, 56), 3))
    if data["by_method"]:
        story.append(Paragraph("Payments by method", heading))
        rows = [["Method", "Payments", "Amount"]] + [[cell(m["method"]), str(m["count"]), money(m["amount"])] for m in data["by_method"]]
        story.append(table(rows, (60, 30, 40), 1))

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 9 * mm, f"{supplier['name']} - statement printed {data['printed']}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        title=f"Supplier statement - {supplier['name']}", author=g.user["business_name"],
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


@app.post("/suppliers/<int:supplier_id>/payment")
@need("admin")
def supplier_payment(supplier_id):
    """Spreads one payment to the supplier over their unpaid purchases, oldest first, and records how it was paid."""
    db, biz = get_db(), g.user["business_id"]
    get_supplier(db, supplier_id)
    back = redirect(url_for("supplier_statement", supplier_id=supplier_id))
    unpaid = [
        u for u in db.all(
            """SELECT id, total_cost, COALESCE(amount_paid, total_cost) AS paid FROM procurements
               WHERE supplier_id=? AND business_id=? ORDER BY purchase_date, id""",
            (supplier_id, biz),
        ) if u["total_cost"] - u["paid"] > 0.005
    ]
    owed = round(sum(u["total_cost"] - u["paid"] for u in unpaid), 2)
    try:
        amount = round(number("amount"), 2)
    except ValueError:
        amount = 0.0
    if amount <= 0 or amount > owed + 0.005:
        flash(f"Enter an amount above zero and no more than the {money(owed)} owed to this supplier.", "error")
        return back
    method, reference, problem = read_payment_details(strict=True)
    if problem:
        flash(problem, "error")
        return back
    paid_on = day_or_none(text("paid_on")) if text("paid_on") else today_str()
    if paid_on is None:
        flash("Enter the date the payment was made.", "error")
        return back

    if paid_on > today_str():
        flash("The payment date cannot be in the future.", "error")
        return back

    allocations = spread_over_unpaid(db, biz, supplier_id, min(amount, owed))
    try:
        add_supplier_payment(db, biz, supplier_id, amount, method, reference, paid_on, text("note")[:150], allocations)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The payment could not be saved. Please try again.", "error")
        return back
    flash(f"Recorded {money(amount)} paid by {method.lower()} (reference {reference}) across {len(allocations)} purchase(s), oldest first. Balance now owed: {money(owed - min(amount, owed))}.", "ok")
    return back


def spread_over_unpaid(db, biz, supplier_id, amount):
    """How an amount paid to a supplier is shared over their unpaid purchases, oldest first: [(purchase id, part)]."""
    unpaid = db.all(
        "SELECT id, total_cost, COALESCE(amount_paid, total_cost) AS paid FROM procurements WHERE supplier_id=? AND business_id=? ORDER BY purchase_date, id",
        (supplier_id, biz),
    )
    allocations, remaining = [], round(amount, 2)
    for u in unpaid:
        if remaining <= 0.005:
            break
        owed = round(u["total_cost"] - u["paid"], 2)
        if owed > 0.005:
            part = round(min(remaining, owed), 2)
            allocations.append((u["id"], part))
            remaining = round(remaining - part, 2)
    return allocations


def supplier_owed(db, biz, supplier_id):
    rows = db.all("SELECT total_cost - COALESCE(amount_paid, total_cost) AS owed FROM procurements WHERE supplier_id=? AND business_id=?", (supplier_id, biz))
    return round(sum(r["owed"] for r in rows if r["owed"] > 0.005), 2)


def payment_how(method, reference):
    return method + (f", {METHOD_LABELS[method].lower()} {reference}" if reference and method in METHOD_LABELS else "")


def move_payment_share(db, biz, payment_id, procurement_id, part, note, stamp):
    """Changes what one purchase has had paid by one supplier payment (a negative part takes some back), with its payment history.
    Never takes a purchase below nothing paid. Returns the part actually moved. The caller commits."""
    row = db.one("SELECT COALESCE(amount_paid, total_cost) AS paid FROM procurements WHERE id=? AND business_id=?", (procurement_id, biz))
    if row is None:
        return 0.0
    part = round(max(part, -row["paid"]), 2)
    new_paid = round(row["paid"] + part, 2)
    db.execute("UPDATE procurements SET amount_paid=? WHERE id=? AND business_id=?", (new_paid, procurement_id, biz))
    db.execute(
        """INSERT INTO payment_log (business_id, kind, ref_id, old_paid, new_paid, note, changed_by, changed_at, payment_id)
           VALUES (?, 'purchase', ?, ?, ?, ?, ?, ?, ?)""",
        (biz, procurement_id, row["paid"], new_paid, note[:200], g.user["username"], stamp, payment_id),
    )
    return part


def take_back(db, biz, payment_id, amount, note, stamp):
    """Takes an amount off what a payment paid, from the newest purchase it settled first (the reverse of how it was spread)."""
    remaining = round(amount, 2)
    shares = db.all(
        """SELECT a.id, a.procurement_id, a.amount FROM supplier_payment_allocations a JOIN procurements pr ON pr.id = a.procurement_id
           WHERE a.payment_id=? AND a.business_id=? AND a.amount > 0 ORDER BY pr.purchase_date DESC, pr.id DESC, a.id DESC""",
        (payment_id, biz),
    )
    for share in shares:
        if remaining <= 0.005:
            break
        taken = -move_payment_share(db, biz, payment_id, share["procurement_id"], -min(remaining, share["amount"]), note, stamp)
        left = round(share["amount"] - taken, 2)
        if left <= 0.005:
            db.execute("DELETE FROM supplier_payment_allocations WHERE id=?", (share["id"],))
        else:
            db.execute("UPDATE supplier_payment_allocations SET amount=? WHERE id=?", (left, share["id"]))
        remaining = round(remaining - taken, 2)


def get_supplier_payment(db, biz, supplier_id, payment_id):
    pay = db.one(
        "SELECT id, amount, method, reference, paid_on, note FROM supplier_payments WHERE id=? AND supplier_id=? AND business_id=?",
        (payment_id, supplier_id, biz),
    )
    if pay is None:
        abort(404)
    return pay


@app.post("/suppliers/<int:supplier_id>/payments/<int:payment_id>/update")
@need("admin")
def update_supplier_payment(supplier_id, payment_id):
    """Raises or lowers a recorded payment, or corrects its date, method or reference. More paid is spread over the unpaid purchases
    oldest first; less is taken back from the newest purchase it paid. Its entry in Accounts follows."""
    db, biz = get_db(), g.user["business_id"]
    supplier = get_supplier(db, supplier_id)
    back = redirect(url_for("supplier_statement", supplier_id=supplier_id))
    pay = get_supplier_payment(db, biz, supplier_id, payment_id)
    if pay["amount"] <= 0.005:
        flash("A payment correction cannot be edited. Edit the payment it corrected, or record a new payment.", "error")
        return back
    try:
        amount = round(number("amount"), 2)
    except ValueError:
        amount = 0.0
    if amount <= 0:
        flash("Enter the amount paid, above zero. To take the whole payment off the statement, use Remove payment.", "error")
        return back
    method, reference, problem = read_payment_details(strict=True)
    if problem:
        flash(problem, "error")
        return back
    paid_on = day_or_none(text("paid_on")) if text("paid_on") else None
    if paid_on is None or paid_on > today_str():
        flash("Enter the date the payment was made (today or earlier).", "error")
        return back
    change = round(amount - pay["amount"], 2)
    owed = supplier_owed(db, biz, supplier_id)
    if change > owed + 0.005:
        flash(f"That adds {money(change)} to the payment, but only {money(owed)} is still owed to {supplier['name']}. Nothing was changed.", "error")
        return back

    note = " ".join(text("note").split())[:150]
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        if change > 0.005:
            for procurement_id, part in spread_over_unpaid(db, biz, supplier_id, change):
                move_payment_share(db, biz, payment_id, procurement_id, part, f"{payment_how(method, reference)}: payment raised by {money(change)}", stamp)
                db.execute(
                    "INSERT INTO supplier_payment_allocations (business_id, payment_id, procurement_id, amount) VALUES (?, ?, ?, ?)",
                    (biz, payment_id, procurement_id, part),
                )
        elif change < -0.005:
            take_back(db, biz, payment_id, -change, f"{payment_how(method, reference)}: payment lowered by {money(-change)}", stamp)
        db.execute(
            "UPDATE supplier_payments SET amount=?, method=?, reference=?, paid_on=?, note=? WHERE id=? AND business_id=?",
            (amount, method, reference, paid_on, note, payment_id, biz),
        )
        # Its entry in Accounts is the same payment, so it changes with it (keeping its time of day if the date did not change)
        entry = db.one("SELECT id, occurred_at FROM cashbook WHERE business_id=? AND source='supplier_payment' AND source_id=?", (biz, payment_id))
        when = entry["occurred_at"] if entry and paid_on == pay["paid_on"] else stamp_for_day(paid_on)
        if entry:
            db.execute(
                "UPDATE cashbook SET amount=?, method=?, reference=?, occurred_at=?, party=?, description=? WHERE id=?",
                (amount, method, reference, when, supplier["name"], note or "Payment to supplier", entry["id"]),
            )
        else:
            record_money(db, biz, "out", amount, method, "supplier", reference, supplier["name"], note or "Payment to supplier", "supplier_payment", payment_id, when)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The payment could not be updated, so nothing was changed. Please try again.", "error")
        return back

    if abs(change) < 0.005:
        flash(f"Payment of {paid_on} updated.", "ok")
    elif change > 0:
        flash(f"Payment raised from {money(pay['amount'])} to {money(amount)}; the extra {money(change)} went to the oldest unpaid purchases. Balance now owed: {money(owed - change)}.", "ok")
    else:
        flash(f"Payment lowered from {money(pay['amount'])} to {money(amount)}; {money(-change)} is owed again on the newest purchases it had paid. Balance now owed: {money(owed - change)}.", "ok")
    return back


@app.post("/suppliers/<int:supplier_id>/payments/<int:payment_id>/delete")
@need("admin")
def remove_supplier_payment(supplier_id, payment_id):
    """Takes a payment off the statement: what it paid on each purchase is owed again, and it leaves Accounts."""
    db, biz = get_db(), g.user["business_id"]
    get_supplier(db, supplier_id)
    back = redirect(url_for("supplier_statement", supplier_id=supplier_id))
    pay = get_supplier_payment(db, biz, supplier_id, payment_id)
    if pay["amount"] <= 0.005:
        flash("A payment correction cannot be removed here.", "error")
        return back
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        take_back(db, biz, payment_id, pay["amount"], f"{payment_how(pay['method'], pay['reference'])}: payment of {pay['paid_on'] or 'an earlier date'} removed", stamp)
        db.execute("DELETE FROM supplier_payment_allocations WHERE payment_id=? AND business_id=?", (payment_id, biz))
        db.execute("DELETE FROM supplier_payments WHERE id=? AND business_id=?", (payment_id, biz))
        db.execute("DELETE FROM cashbook WHERE business_id=? AND source='supplier_payment' AND source_id=?", (biz, payment_id))
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The payment could not be removed, so nothing was changed. Please try again.", "error")
        return back
    flash(f"The {money(pay['amount'])} payment of {pay['paid_on'] or 'an earlier date'} was removed. What it paid is owed again. Balance now owed: {money(supplier_owed(db, biz, supplier_id))}.", "ok")
    return back


@app.post("/suppliers/<int:supplier_id>/update")
@need("admin")
def update_supplier(supplier_id):
    db, biz = get_db(), g.user["business_id"]
    get_supplier(db, supplier_id)
    name, phone = text("name"), text("phone")
    nkey, pkey = supplier_key(name), phone_key(phone)
    clash = db.one("SELECT id FROM suppliers WHERE business_id=? AND name_key=? AND phone_key=? AND id<>?", (biz, nkey, pkey, supplier_id))
    if not nkey:
        flash("The supplier needs a name.", "error")
    elif clash:
        flash("Another supplier account already has that name and phone number.", "error")
    else:
        db.execute("UPDATE suppliers SET name=?, phone=?, name_key=?, phone_key=? WHERE id=? AND business_id=?", (name, phone, nkey, pkey, supplier_id, biz))
        db.commit()
        flash("Supplier details saved. Purchases already logged keep the supplier name typed on them.", "ok")
    return redirect(url_for("supplier_statement", supplier_id=supplier_id))


SUPPLIER_COLUMNS = "id, name, phone, name_key, phone_key, created_at"


def supplier_distinct_pairs(db, biz):
    return {(r["low_id"], r["high_id"]) for r in db.all("SELECT low_id, high_id FROM supplier_distinct WHERE business_id=?", (biz,))}


def find_supplier_duplicates(db, biz):
    """Pairs of supplier accounts that may be the same supplier, strongest evidence first."""
    people = db.all(
        """SELECT s.id, s.name, s.phone, s.name_key, s.phone_key, s.created_at,
                  COUNT(pr.id) AS purchases, COALESCE(SUM(pr.total_cost), 0) AS total,
                  COALESCE(SUM(pr.total_cost - COALESCE(pr.amount_paid, pr.total_cost)), 0) AS owed
           FROM suppliers s LEFT JOIN procurements pr ON pr.supplier_id = s.id
           WHERE s.business_id=? GROUP BY s.id, s.name, s.phone, s.name_key, s.phone_key, s.created_at ORDER BY s.id LIMIT ?""",
        (biz, DUPLICATE_SCAN_LIMIT),
    )
    return duplicate_pairs(people, supplier_distinct_pairs(db, biz))


def supplier_twins(db, biz, supplier_id):
    """Other accounts sharing this supplier's phone number, unless an admin already said they are different suppliers."""
    me = db.one("SELECT phone_key FROM suppliers WHERE id=? AND business_id=?", (supplier_id, biz))
    if not me or not me["phone_key"]:
        return []
    others = db.all("SELECT id, name, phone FROM suppliers WHERE business_id=? AND phone_key=? AND id<>? ORDER BY id", (biz, me["phone_key"], supplier_id))
    ruled_out = supplier_distinct_pairs(db, biz)
    return [other for other in others if pair_key(supplier_id, other["id"]) not in ruled_out]


@app.get("/suppliers/duplicates")
@need("admin")
def supplier_duplicates():
    pairs = find_supplier_duplicates(get_db(), g.user["business_id"])
    return render_template("supplier_duplicates.html", pairs=pairs[:DUPLICATES_SHOWN], total=len(pairs))


def supplier_pair_from_form(db, biz, first, second):
    a = db.one(f"SELECT {SUPPLIER_COLUMNS} FROM suppliers WHERE id=? AND business_id=?", (request.form.get(first, type=int), biz))
    b = db.one(f"SELECT {SUPPLIER_COLUMNS} FROM suppliers WHERE id=? AND business_id=?", (request.form.get(second, type=int), biz))
    return (a, b) if a and b and a["id"] != b["id"] else (None, None)


def absorb_supplier(db, biz, keep, remove):
    """Moves one supplier account into another: its purchases, its payments and the spellings that led to it. keep is updated
    in place (a phone number it lacked, the earliest start date). The caller commits. Returns (purchases moved, payments moved)."""
    moved = db.execute("UPDATE procurements SET supplier_id=? WHERE supplier_id=? AND business_id=?", (keep["id"], remove["id"], biz)).rowcount
    paid = db.execute("UPDATE supplier_payments SET supplier_id=? WHERE supplier_id=? AND business_id=?", (keep["id"], remove["id"], biz)).rowcount
    # Accounts lists supplier payments under the account's name
    db.execute(
        """UPDATE cashbook SET party=? WHERE business_id=? AND source='supplier_payment'
           AND source_id IN (SELECT id FROM supplier_payments WHERE supplier_id=? AND business_id=?)""",
        (keep["name"], biz, keep["id"], biz),
    )
    # The removed account's spelling, and any spellings merged into it earlier, now lead to the kept account
    db.execute("UPDATE supplier_aliases SET supplier_id=? WHERE supplier_id=? AND business_id=?", (keep["id"], remove["id"], biz))
    db.execute("DELETE FROM supplier_aliases WHERE business_id=? AND name_key=? AND phone_key=?", (biz, remove["name_key"], remove["phone_key"]))
    db.execute(
        "INSERT INTO supplier_aliases (business_id, name_key, phone_key, supplier_id) VALUES (?, ?, ?, ?)",
        (biz, remove["name_key"], remove["phone_key"], keep["id"]),
    )
    db.execute("DELETE FROM supplier_distinct WHERE business_id=? AND (low_id=? OR high_id=?)", (biz, remove["id"], remove["id"]))
    db.execute("DELETE FROM suppliers WHERE id=? AND business_id=?", (remove["id"], biz))

    since = min(keep["created_at"] or "9999", remove["created_at"] or "9999")
    phone, pkey = keep["phone"], keep["phone_key"]
    if not pkey and remove["phone_key"]:
        taken = db.one(
            "SELECT id FROM suppliers WHERE business_id=? AND name_key=? AND phone_key=? AND id<>?",
            (biz, keep["name_key"], remove["phone_key"], keep["id"]),
        )
        if not taken:
            phone, pkey = remove["phone"], remove["phone_key"]
    keep.update(phone=phone, phone_key=pkey, created_at=None if since == "9999" else since)
    db.execute(
        "UPDATE suppliers SET phone=?, phone_key=?, created_at=? WHERE id=? AND business_id=?",
        (phone, pkey, keep["created_at"], keep["id"], biz),
    )
    return moved, paid


@app.post("/suppliers/combine")
@need("admin")
def combine_suppliers():
    """Combines two or more ticked supplier statements into one, under the name given: every purchase and payment moves onto it,
    the other accounts are removed, and their spellings lead to it from now on."""
    db, biz = get_db(), g.user["business_id"]
    # Back to the list as it was: the search and balance filter come with the form
    back = redirect(url_for("suppliers", **{key: text(key) for key in ("q", "balance") if text(key)}))
    ids = set(read_ids())
    chosen = [r for r in db.all(f"SELECT {SUPPLIER_COLUMNS} FROM suppliers WHERE business_id=? ORDER BY id", (biz,)) if r["id"] in ids]
    final = clean_name(text("supplier_name"), 100)
    if len(ids) < 2:
        flash("Tick at least two supplier statements to combine.", "error")
        return back
    if len(chosen) != len(ids):
        flash("Some of the ticked suppliers no longer exist. Refresh the page and try again.", "error")
        return back
    if not final:
        flash("Type the name for the combined statement.", "error")
        return back

    # The account already called that stays, keeping its own spelling; otherwise the oldest one does, renamed
    keep = next((r for r in chosen if norm(r["name"]) == norm(final)), chosen[0])
    if norm(keep["name"]) == norm(final):
        final = keep["name"]
    others = [r for r in chosen if r is not keep]
    # The keys the kept account was found by before anything changed (it may take a phone number from another below)
    spellings = {(keep["name_key"], keep["phone_key"])}
    moved = paid = 0
    try:
        for other in others:
            m, p = absorb_supplier(db, biz, keep, other)
            moved, paid = moved + m, paid + p
        nkey = supplier_key(final)
        spellings.add((keep["name_key"], keep["phone_key"]))
        if final != keep["name"]:
            clash = db.one("SELECT name FROM suppliers WHERE business_id=? AND name_key=? AND phone_key=? AND id<>?", (biz, nkey, keep["phone_key"], keep["id"]))
            if clash:
                db.rollback()
                flash(f"There is already a supplier account called '{clash['name']}' with the same phone number. Tick it as well to combine it too. Nothing was changed.", "error")
                return back
            db.execute("UPDATE suppliers SET name=?, name_key=? WHERE id=? AND business_id=?", (final, nkey, keep["id"], biz))
        # The kept account's old spellings still lead to it; a remembered spelling that is now its own name is not needed
        for name_key_, phone_key_ in spellings:
            db.execute("DELETE FROM supplier_aliases WHERE business_id=? AND name_key=? AND phone_key=?", (biz, name_key_, phone_key_))
            if (name_key_, phone_key_) != (nkey, keep["phone_key"]):
                db.execute(
                    "INSERT INTO supplier_aliases (business_id, name_key, phone_key, supplier_id) VALUES (?, ?, ?, ?)",
                    (biz, name_key_, phone_key_, keep["id"]),
                )
        # Accounts lists supplier payments under the account's name
        db.execute(
            """UPDATE cashbook SET party=? WHERE business_id=? AND source='supplier_payment'
               AND source_id IN (SELECT id FROM supplier_payments WHERE supplier_id=? AND business_id=?)""",
            (final, biz, keep["id"], biz),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The statements could not be combined, so nothing was changed. Please try again.", "error")
        return back

    names = ", ".join(f"'{r['name']}'" for r in chosen)
    flash(f"Combined {names} into one statement, '{final}': {moved} purchase(s) and {paid} payment(s) moved onto it.", "ok")
    return redirect(url_for("supplier_statement", supplier_id=keep["id"]))


@app.post("/suppliers/merge")
@need("admin")
def merge_suppliers():
    db, biz = get_db(), g.user["business_id"]
    keep, remove = supplier_pair_from_form(db, biz, "keep", "remove")
    if keep is None:
        flash("Those two accounts could not be found. One may already have been merged.", "error")
        return redirect(url_for("supplier_duplicates"))

    try:
        moved, paid = absorb_supplier(db, biz, keep, remove)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The merge could not be saved, so nothing was changed. Please try again.", "error")
        return redirect(url_for("supplier_duplicates"))

    flash(f"Merged '{remove['name']}' into '{keep['name']}': {moved} purchase(s) and {paid} payment(s) moved onto this statement.", "ok")
    return redirect(url_for("supplier_statement", supplier_id=keep["id"]))


@app.post("/suppliers/distinct")
@need("admin")
def mark_suppliers_distinct():
    db, biz = get_db(), g.user["business_id"]
    a, b = supplier_pair_from_form(db, biz, "a", "b")
    if a is not None:
        low, high = pair_key(a["id"], b["id"])
        if not db.one("SELECT id FROM supplier_distinct WHERE business_id=? AND low_id=? AND high_id=?", (biz, low, high)):
            db.execute("INSERT INTO supplier_distinct (business_id, low_id, high_id) VALUES (?, ?, ?)", (biz, low, high))
            db.commit()
        flash(f"Noted: '{a['name']}' and '{b['name']}' are different suppliers. They will not be suggested again.", "ok")
    back_to = request.form.get("back_to", type=int)
    return redirect(url_for("supplier_statement", supplier_id=back_to) if back_to else url_for("supplier_duplicates"))


def link_existing_suppliers():
    """Opens accounts for suppliers named on purchases logged before supplier accounts existed. Safe to repeat."""
    db = Database()
    try:
        unlinked = db.all(
            "SELECT id, business_id, supplier_name, purchase_date FROM procurements WHERE supplier_id IS NULL AND supplier_name IS NOT NULL AND supplier_name <> '' ORDER BY id"
        )
        for p in unlinked:
            supplier_id = supplier_for(db, p["business_id"], p["supplier_name"], "", p["purchase_date"])
            if supplier_id:
                db.execute("UPDATE procurements SET supplier_id=? WHERE id=?", (supplier_id, p["id"]))
        db.commit()
    except DB_ERRORS:
        db.rollback()
    finally:
        db.close()



# --- REPORTS ---
REPORT_PERIODS = (
    ("this_month", "This month"), ("last_month", "Last month"), ("last_30", "Last 30 days"),
    ("this_year", "This year"), ("all", "All time"), ("custom", "Custom range"),
)
REPORT_PARAMS = ("period", "from", "to", "view", "dept")


def report_period(default="this_month"):
    """Returns (choice, first day, last day) from the query string; both days are None for 'all time'."""
    today = now().date()
    choice = request.args.get("period", default)
    start = end = None
    if choice == "custom":
        try:
            start = datetime.date.fromisoformat(request.args.get("from", ""))
            end = datetime.date.fromisoformat(request.args.get("to", ""))
            if end < start:
                start, end = end, start
        except ValueError:
            choice = "this_month"

    if choice == "last_month":
        end = today.replace(day=1) - datetime.timedelta(days=1)
        start = end.replace(day=1)
    elif choice == "last_30":
        start, end = today - datetime.timedelta(days=29), today
    elif choice == "this_year":
        start, end = today.replace(month=1, day=1), today.replace(month=12, day=31)
    elif choice not in ("all", "custom"):
        choice = "this_month"
        start = today.replace(day=1)
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    return choice, start, end


def report_url(endpoint="reports", **changes):
    """Link to a report page that keeps the current period and view."""
    params = {key: value for key, value in request.args.items() if key in REPORT_PARAMS}
    params.update(changes)
    if endpoint == "reports":
        params.pop("dept", None)
    return url_for(endpoint, **{key: value for key, value in params.items() if value})


def gather_report(db, biz, start, end, dept=None):
    """Sale lines and purchases dated inside the period, optionally for one department."""
    low = start.isoformat() if start else "0000-00-00"
    high = (end + datetime.timedelta(days=1)).isoformat() if end else "9999-99-99"
    sale_sql = """SELECT ii.department, ii.item_code, ii.item_name, ii.qty, ii.unit_price, ii.line_total,
                         i.id AS invoice_id, i.invoice_no, i.kind, i.customer_name, i.created_at, i.total, i.amount_paid
                  FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
                  WHERE ii.business_id=? AND i.payment_status <> 'Cancelled' AND i.created_at >= ? AND i.created_at < ?"""
    buy_sql = """SELECT id, department, item_code, name, supplier_name, purchase_date, quantity, total_cost, amount_paid
                 FROM procurements WHERE business_id=? AND purchase_date >= ? AND purchase_date < ?"""
    params = [biz, low, high]
    if dept is not None:
        sale_sql += " AND ii.department=?"
        buy_sql += " AND department=?"
        params.append(dept)
    return db.all(sale_sql + " ORDER BY i.created_at, ii.id", params), db.all(buy_sql + " ORDER BY purchase_date, id", params)


def blank_totals():
    return {"sales": 0.0, "received": 0.0, "purchases": 0.0, "paid": 0.0, "units_in": 0, "units_sold": 0, "units_farm": 0}


def summarise(sales, buys, sale_key, buy_key):
    """Totals of money and stock in and out, grouped by whatever the key functions return."""
    groups = {}
    for r in sales:
        t = groups.setdefault(sale_key(r), blank_totals())
        if r["kind"] == "FARM_USE":
            t["units_farm"] += r["qty"]
        else:
            t["sales"] += r["line_total"]
            t["units_sold"] += r["qty"]
            # An invoice's payment is shared across its lines in proportion to their value
            t["received"] += r["line_total"] * r["amount_paid"] / r["total"] if r["total"] else 0.0
    for r in buys:
        t = groups.setdefault(buy_key(r), blank_totals())
        t["purchases"] += r["total_cost"] or 0.0
        t["paid"] += r["amount_paid"] or 0.0
        t["units_in"] += r["quantity"] or 0
    return groups


def time_buckets(start, end, sales, buys):
    """Picks daily, monthly or yearly steps for the period. Returns ([(key, label)], key length)."""
    if start is None:
        seen = [r["created_at"][:10] for r in sales] + [r["purchase_date"][:10] for r in buys]
        try:
            start, end = datetime.date.fromisoformat(min(seen)), datetime.date.fromisoformat(max(seen))
        except ValueError:
            start = end = now().date()

    months = (end.year - start.year) * 12 + end.month - start.month + 1
    if (end - start).days < 62:
        days = [start + datetime.timedelta(days=i) for i in range((end - start).days + 1)]
        return [(d.isoformat(), d.strftime("%d %b")) for d in days], 10
    if months <= 36:
        firsts = [datetime.date(start.year + (start.month - 1 + i) // 12, (start.month - 1 + i) % 12 + 1, 1) for i in range(months)]
        return [(d.strftime("%Y-%m"), d.strftime("%b %Y")) for d in firsts], 7
    return [(str(year), str(year)) for year in range(start.year, end.year + 1)], 4


def timeline_rows(start, end, sales, buys):
    buckets, length = time_buckets(start, end, sales, buys)
    by_time = summarise(sales, buys, lambda r: r["created_at"][:length], lambda r: r["purchase_date"][:length])
    return [dict(by_time.get(key, blank_totals()), label=label) for key, label in buckets]


def compact(n):
    """1250 -> 1.3K, for axis ticks."""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= limit:
            return f"{n / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{n:.2f}".rstrip("0").rstrip(".")


def nice_ceiling(value):
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    return next(step * magnitude for step in (1, 2, 4, 8, 10) if value <= step * magnitude)


def column_chart(labels, series, fmt):
    """Geometry for a grouped column chart drawn as SVG by _chart.html. series is a list of (name, values)."""
    width, height, left, right, top, bottom = 720, 250, 56, 8, 10, 30
    plot_w, plot_h, base = width - left - right, height - top - bottom, height - bottom
    peak = max([v for _, values in series for v in values] + [0])
    ceiling = nice_ceiling(peak)
    count = max(len(labels), 1)
    band = plot_w / count
    gap = 2
    bar_w = max(1.5, min(24.0, (band * 0.72 - gap * (len(series) - 1)) / len(series)))
    group_w = len(series) * bar_w + gap * (len(series) - 1)

    bars, hits = [], []
    for i, label in enumerate(labels):
        group_x = left + i * band + (band - group_w) / 2
        tip = [label]
        for j, (name, values) in enumerate(series):
            tip.append(f"{name}: {fmt(values[i])}")
            h = plot_h * values[i] / ceiling
            if h <= 0:
                continue
            h = max(h, 1.0)
            x, y, r = group_x + j * (bar_w + gap), base - h, min(4.0, h, bar_w / 2)
            # Rounded at the data end, square on the baseline
            bars.append({"series": j + 1, "d": (
                f"M{x:.1f},{base:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
                f"H{x + bar_w - r:.1f}Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f}V{base:.1f}Z"
            )})
        hits.append({"x": round(left + i * band, 1), "w": round(band, 1), "tip": "\n".join(tip)})

    every = math.ceil(count / 12)
    limit = 14 if count > 6 else 24
    return {
        "width": width, "height": height, "left": left, "right": width - right, "top": top, "plot_h": plot_h, "base": base,
        "series": [name for name, _ in series], "bars": bars, "hits": hits, "empty": peak <= 0,
        "ticks": [{"y": round(base - plot_h * i / 4, 1), "label": compact(ceiling * i / 4)} for i in range(5)],
        "xlabels": [
            {"x": round(left + i * band + band / 2, 1), "label": label if len(label) <= limit else label[:limit - 1] + "…"}
            for i, label in enumerate(labels) if i % every == 0
        ],
    }


def whole(value):
    return f"{int(value):,}"


def report_charts(rows):
    """The money and stock charts for a list of rows that each carry a 'label'."""
    labels = [r["label"] for r in rows]
    return {
        "money": column_chart(labels, [("Sales", [r["sales"] for r in rows]), ("Purchases", [r["purchases"] for r in rows])], money),
        "units": column_chart(
            labels,
            [("Units in (purchased)", [r["units_in"] for r in rows]), ("Units out (sold + farm use)", [r["units_sold"] + r["units_farm"] for r in rows])],
            whole,
        ),
    }


def report_context(choice, start, end):
    return {
        "periods": REPORT_PERIODS, "period": choice, "start": start, "end": end,
        "view": "table" if request.args.get("view") == "table" else "visual", "report_url": report_url,
    }


def sum_rows(rows):
    total = blank_totals()
    for r in rows:
        for key in total:
            total[key] += r[key]
    return total


# --- OVERVIEW: the business at a glance ---
# The first page of the menu. Read top to bottom: the figures that say how things stand (each against the period before it), why
# they look that way (sales day by day, what sells), then what needs doing and the latest sales. Every card opens the page behind it.
def change_against(now_value, before):
    """How a figure moved against the one before it: {"pct", "dir"} or None when there is nothing to compare with."""
    if before <= 0.005:
        return None
    pct = (now_value - before) / before * 100
    return {"pct": abs(round(pct, 1 if abs(pct) < 10 else 0)), "dir": "up" if pct > 0.05 else "down" if pct < -0.05 else "flat"}


def plural_sales(n):
    return f"{n:,} sale{'s' if n != 1 else ''}"


def spark_points(values, width=100, height=30):
    """A list of values as the points of a small line, left to right, highest at the top."""
    if len(values) < 2:
        return ""
    top = max(values) or 1
    step = width / (len(values) - 1)
    return " ".join(f"{i * step:.1f},{height - 2 - (v / top) * (height - 4):.1f}" for i, v in enumerate(values))


def trend_chart(days, values, before):
    """A line of daily values with the same days of the period before as a dashed line behind it, drawn as SVG by the page."""
    width, height, left, right, top, bottom = 720, 240, 46, 12, 12, 26
    peak = nice_ceiling(max(values + before + [0]))
    plot_w, plot_h = width - left - right, height - top - bottom
    step = plot_w / max(1, len(days) - 1)

    def y(v):
        return top + plot_h - (v / peak) * plot_h

    def path(series):
        return " ".join(f"{left + i * step:.1f},{y(v):.1f}" for i, v in enumerate(series))

    base = top + plot_h
    every = max(1, len(days) // 6)
    return {
        "width": width, "height": height, "left": left, "right": width - right, "base": base, "empty": not any(values) and not any(before),
        "ticks": [{"y": round(y(peak * k / 4), 1), "label": compact(peak * k / 4)} for k in range(5)],
        "line": path(values), "before": path(before),
        "area": f"{left:.1f},{base:.1f} " + path(values) + f" {left + (len(days) - 1) * step:.1f},{base:.1f}",
        "last": {"x": round(left + (len(days) - 1) * step, 1), "y": round(y(values[-1]), 1)},
        "xlabels": [{"x": round(left + i * step, 1), "label": datetime.date.fromisoformat(d).strftime("%d %b")} for i, d in enumerate(days) if i % every == 0],
        "hits": [{"x": round(left + i * step - step / 2, 1), "w": round(step, 1),
                  "tip": f"{datetime.date.fromisoformat(d).strftime('%a %d %b')}\nSales: {money(values[i])}\n30 days earlier: {money(before[i])}"} for i, d in enumerate(days)],
    }


@app.get("/overview")
@need(None)
def overview():
    db, biz, today = get_db(), g.user["business_id"], today_str()
    admin = g.user["role"] == "admin"
    sale = "business_id=? AND kind='SALE' AND COALESCE(payment_status, '') <> 'Cancelled'"
    yesterday, month_start = add_days(today, -1), today[:8] + "01"
    last_month_end = add_days(month_start, -1)
    last_month_start = last_month_end[:8] + "01"
    # The same stretch of last month as has passed of this one, so a part-month is compared with a part-month
    last_month_same_day = min(last_month_end, last_month_start[:8] + today[8:])

    by_day = {r["day"]: r for r in db.all(
        f"SELECT SUBSTR(created_at, 1, 10) AS day, COALESCE(SUM(total), 0) AS total, COUNT(*) AS n FROM invoices WHERE {sale} AND created_at >= ? GROUP BY SUBSTR(created_at, 1, 10)",
        (biz, min(add_days(today, -59), last_month_start)))}

    def total_between(first, last):
        return round(sum(r["total"] for day, r in by_day.items() if first <= day <= last), 2)

    days = [add_days(today, -n) for n in range(29, -1, -1)]
    values = [round((by_day.get(d) or {}).get("total", 0) or 0, 2) for d in days]
    before = [round((by_day.get(add_days(d, -30)) or {}).get("total", 0) or 0, 2) for d in days]
    sold_today, sold_yesterday = values[-1], values[-2]
    month, last_month = total_between(month_start, today), total_between(last_month_start, last_month_same_day)

    kpis = [
        {"label": "Sales this month", "value": month, "money": True, "lead": True, "change": change_against(month, last_month), "good_up": True,
         "against": "the same days last month", "note": plural_sales(sum(r["n"] for d, r in by_day.items() if month_start <= d <= today)) + f" since {datetime.date.fromisoformat(month_start).strftime('%d %b')}",
         "spark": spark_points(values[-14:]), "href": url_for("reports"), "tip": "Sales and purchases by department and over time"},
        {"label": "Sales today", "value": sold_today, "money": True, "change": change_against(sold_today, sold_yesterday), "good_up": True, "against": "yesterday",
         "note": plural_sales((by_day.get(today) or {}).get("n", 0)), "href": url_for("history"), "tip": "Every sale and its invoice"},
    ]
    low = db.one("SELECT COUNT(*) AS n FROM pos_products WHERE business_id=? AND quantity < ?", (biz, g.user["low_stock_threshold"]))["n"]
    if admin:
        owed = db.one(f"SELECT COUNT(*) AS n, COALESCE(SUM(balance_due), 0) AS owed FROM invoices WHERE {sale} AND balance_due > 0.005", (biz,))
        we_owe = db.one("SELECT COALESCE(SUM(total_cost - COALESCE(amount_paid, total_cost)), 0) AS n FROM procurements WHERE business_id=?", (biz,))["n"]
        kpis += [
            {"label": "Customers owe us", "value": owed["owed"], "money": True, "note": f"on {owed['n']} unpaid invoice{'s' if owed['n'] != 1 else ''}",
             "href": url_for("customers"), "tip": "Each customer's account and what they owe", "tone": "gold" if owed["owed"] > 0.005 else ""},
            {"label": "We owe suppliers", "value": we_owe, "money": True, "note": "for purchases not yet paid", "href": url_for("suppliers"),
             "tip": "Each supplier's account and what we owe", "tone": "gold" if we_owe > 0.005 else ""},
        ]
    kpis.append({"label": "Low on the POS", "value": low, "note": f"items under {g.user['low_stock_threshold']} in stock", "href": url_for("inventory"),
                 "tip": "Stock by category", "tone": "warn" if low else "ok"})

    # What needs doing: one row for each thing waiting, the overdue ones first
    todo = []
    for r in db.all(f"SELECT id, invoice_no, customer_name, deliver_on AS day {TO_DELIVER_SQL} AND deliver_on <= ? ORDER BY deliver_on, id LIMIT 6", (biz, today)):
        todo.append({"kind": "Delivery", "title": r["customer_name"], "detail": f"Deliver invoice {r['invoice_no']}", "day": r["day"], "href": url_for("invoice", invoice_id=r["id"])})
    for r in db.all(f"SELECT id, invoice_no, customer_name, collect_on AS day {WAITING_SQL} AND collect_on <= ? ORDER BY collect_on, id LIMIT 6", (biz, add_days(today, 1))):
        todo.append({"kind": "Collection", "title": r["customer_name"], "detail": f"Collects invoice {r['invoice_no']}", "day": r["day"], "href": url_for("invoice", invoice_id=r["id"])})
    for r in db.all("""SELECT po.id, po.customer_name, po.qty, po.collect_on AS day, p.name FROM preorders po JOIN plantings p ON p.id = po.planting_id
                       WHERE po.business_id=? AND po.status='pending' AND po.collect_on IS NOT NULL AND po.collect_on <= ? ORDER BY po.collect_on, po.id LIMIT 6""",
                    (biz, add_days(today, 1))):
        todo.append({"kind": "Pre-order", "title": r["customer_name"], "detail": f"{r['qty']:,} x {r['name']}", "day": r["day"], "href": url_for("up_next") + f"#preorder-{r['id']}"})
    for r in db.all("SELECT * FROM customer_requests WHERE business_id=? AND status='open' AND follow_up_on <= ? ORDER BY follow_up_on, id LIMIT 6", (biz, today)):
        todo.append({"kind": "Request", "title": r["customer_name"], "detail": "Follow up: " + request_label(r), "day": r["follow_up_on"], "href": url_for("customer_request", cr_id=r["id"])})
    if admin:
        for r in db.all("SELECT id, full_name, employment_type, next_pay_date AS day FROM employees WHERE business_id=? AND next_pay_date IS NOT NULL AND next_pay_date <= ? ORDER BY next_pay_date LIMIT 6",
                        (biz, add_days(today, 1))):
            todo.append({"kind": "Pay", "title": r["full_name"], "detail": "Pay day: their payslip is filled in", "day": r["day"],
                         "href": url_for("new_payslip", employee=r["id"], pay_date=r["day"])})
    todo.sort(key=lambda t: t["day"])
    for t in todo:
        t["late"] = t["day"] < today
        t["when"] = when_words(t["day"], today, before="due ")
    kpis.append({"label": "Needs doing", "value": len(todo), "note": f"{sum(1 for t in todo if t['late'])} overdue" if any(t["late"] for t in todo) else "due today or tomorrow",
                 "href": "#todo", "tip": "Deliveries, collections, pre-orders and requests that are due", "tone": "warn" if any(t["late"] for t in todo) else "gold" if todo else "ok"})

    top = db.all(
        f"""SELECT ii.item_code AS code, ii.item_name AS name, SUM(ii.qty) AS units, SUM(ii.line_total) AS value
            FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
            WHERE i.business_id=? AND i.kind='SALE' AND COALESCE(i.payment_status, '') <> 'Cancelled' AND i.created_at >= ?
            GROUP BY ii.item_code, ii.item_name ORDER BY value DESC LIMIT 6""", (biz, add_days(today, -29)))
    best = top[0]["value"] if top else 0
    recent = db.all(f"SELECT id, invoice_no, customer_name, total, balance_due, payment_status, created_at FROM invoices WHERE {sale} ORDER BY created_at DESC, id DESC LIMIT 7", (biz,))
    growing = [f for f in with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? AND still_in_greenhouse=1 ORDER BY ready_on, id", (biz,)))
               if f["available"] + f["pending"] > 0][:5]
    return render_template(
        "overview.html", kpis=kpis, chart=trend_chart(days, values, before), todo=todo[:8], todo_count=len(todo), admin=admin,
        top=[dict(r, pct=round(100 * (r["value"] or 0) / best, 1) if best else 0, href=sold_url(r["code"], r["name"])) for r in top],
        recent=recent, growing=growing, today=today, updated=now().strftime("%H:%M"), last_30=round(sum(values), 2), prior_30=round(sum(before), 2),
        change_30=change_against(sum(values), sum(before)),
    )


@app.get("/reports")
@need(None)
def reports():
    db, biz = get_db(), g.user["business_id"]
    choice, start, end = report_period()
    sales, buys = gather_report(db, biz, start, end)

    by_dept = summarise(sales, buys, lambda r: r["department"] or "General", lambda r: r["department"] or "General")
    names = sorted(set(department_names(db, biz)) | set(by_dept))
    departments = [dict(by_dept.get(name, blank_totals()), label=name) for name in names]
    timeline = timeline_rows(start, end, sales, buys)
    totals = sum_rows(departments)
    # Against the stretch of the same length just before (only the days that have passed, so a part-month is not set against a whole one)
    compare = None
    if start and end:
        last = min(end, now().date())
        if last >= start:
            days = (last - start).days + 1
            before_end = start - datetime.timedelta(days=1)
            old_sales, old_buys = gather_report(db, biz, before_end - datetime.timedelta(days=days - 1), before_end)
            old = summarise(old_sales, old_buys, lambda r: "all", lambda r: "all").get("all", blank_totals())
            compare = {"against": f"the {days} day{'s' if days != 1 else ''} before", "sales": change_against(totals["sales"], old["sales"]),
                       "purchases": change_against(totals["purchases"], old["purchases"])}
    return render_template(
        "reports.html", departments=departments, totals=totals, timeline=timeline, compare=compare,
        dept_charts=report_charts(departments), time_charts=report_charts(timeline), **report_context(choice, start, end),
    )


@app.get("/reports/department")
@need(None)
def report_department():
    db, biz = get_db(), g.user["business_id"]
    dept = request.args.get("dept", "").strip()
    if not dept:
        return redirect(report_url("reports"))
    choice, start, end = report_period()
    sales, buys = gather_report(db, biz, start, end, dept)

    timeline = timeline_rows(start, end, sales, buys)
    products = {}
    for r in sales:
        p = products.setdefault((r["item_code"], r["item_name"]), {"code": r["item_code"], "name": r["item_name"], "sold": 0, "value": 0.0, "farm": 0})
        if r["kind"] == "FARM_USE":
            p["farm"] += r["qty"]
        else:
            p["sold"] += r["qty"]
            p["value"] += r["line_total"]
    products = sorted(products.values(), key=lambda p: (-p["value"], -p["sold"], p["name"] or ""))
    best = [p for p in products if p["value"] > 0][:8]
    top_value = best[0]["value"] if best else 0.0

    return render_template(
        "report_department.html", dept=dept, totals=sum_rows(timeline), timeline=timeline, time_charts=report_charts(timeline),
        products=products, best=[dict(p, pct=round(100 * p["value"] / top_value, 1)) for p in best],
        purchases=[with_payment_state(dict(r)) for r in buys],
        sale_lines=[r for r in sales if r["kind"] != "FARM_USE"], farm_lines=[r for r in sales if r["kind"] == "FARM_USE"],
        **report_context(choice, start, end),
    )


# --- CSV IMPORT (ADMIN) ---
IMPORT_MAX_ROWS = 2000
IMPORT_MAX_ERRORS_SHOWN = 50

# Field -> accepted column headings (compared in lower case, with underscores read as spaces)
PURCHASE_COLUMNS = {
    "item_code": ("item code", "code", "sku", "barcode"),
    "name": ("name", "item name", "product name", "product", "item"),
    "category": ("category",),
    "department": ("department", "dept"),
    "quantity": ("quantity", "qty", "quantity bought"),
    "unit_cost": ("unit cost", "cost"),
    "price": ("selling price", "sell price", "price"),
    "supplier_name": ("supplier", "supplier name"),
    "invoice_no": ("invoice no", "invoice number", "invoice", "supplier invoice"),
    "purchase_date": ("date", "purchase date"),
    "batch_no": ("batch no", "batch number", "batch"),
    "date_planted": ("date planted",),
    "expiry_date": ("expiry", "expiry date"),
    "payment_status": ("payment status", "status"),
    "amount_paid": ("amount paid", "paid"),
    "seeds_per_pack": ("seeds per pack", "seeds in pack", "seeds in a pack", "pack size", "seeds/pack"),
    "unit": ("unit", "uom", "unit of measure", "packaging", "packaging unit"),
    "in_inventory": ("in inventory", "inventory", "inventory item", "add to inventory", "also inventory"),
    "supplier_phone": ("supplier phone", "supplier contact", "supplier phone number"),
    "payment_method": ("payment method", "method", "mode of payment", "paid by", "payment mode"),
    "payment_reference": ("payment reference", "reference", "receipt number", "receipt no", "reference number", "transaction id", "payment ref"),
}
YES_WORDS = ("yes", "y", "true", "1", "x")
NO_WORDS = ("no", "n", "false", "0")
SALES_COLUMNS = {
    "invoice_no": ("invoice no", "invoice number", "invoice"),
    "date": ("date", "date/time", "date time", "sale date"),
    "customer_name": ("customer", "customer name"),
    "customer_phone": ("phone", "customer phone", "phone number"),
    "item_code": ("item code", "code", "sku", "barcode"),
    "item_name": ("item name", "product name", "product", "item", "name"),
    "quantity": ("quantity", "qty"),
    "unit_price": ("unit price", "price"),
    "payment_status": ("payment status", "status"),
    "amount_paid": ("amount paid", "paid"),
    "payment_method": ("payment method", "method", "mode of payment", "paid by", "payment mode"),
    "payment_reference": ("payment reference", "reference", "receipt number", "receipt no", "reference number", "transaction id", "payment ref"),
}
IMPORT_TEMPLATES = {
    "purchases": (
        ["Item Code", "Name", "Category", "Department", "Quantity", "Unit Cost", "Selling Price", "Supplier", "Invoice No", "Purchase Date", "Batch No", "Date Planted", "Expiry Date", "Payment Status", "Amount Paid", "Seeds Per Pack", "Unit", "In Inventory", "Supplier Phone", "Payment Method", "Payment Reference"],
        ["SEED-001", "Tomato Seed Pack", "Seeds", "Greenhouse", 50, 450, 600, "Agro Supplies Ltd", "SUP-1001", "2026-10-01", "B-22", "", "2027-10-01", "Paid", "", 1000, "pack", "Yes", "0977123456", "Bank transfer", "TRX-5521"],
    ),
    "sales": (
        ["Invoice No", "Date", "Customer", "Phone", "Item Code", "Item Name", "Quantity", "Unit Price", "Payment Status", "Amount Paid", "Payment Method", "Payment Reference"],
        ["", "2026-10-01 09:30", "Jane Banda", "0977123456", "SEED-001", "Maize Seed 10kg", 2, 600, "Paid", "", "Airtel Money", "MP261001.0930"],
    ),
    "payroll": (
        ["Employee Name", "NRC Number", "Employment Type", "Pay Month", "Basic Salary", "Expected Days", "Worked Days", "Bonus: Transport", "Deduction: Advance", "Employee NAPSA", "Employee NHIMA", "Employer NAPSA", "Employer NHIMA", "Net Pay"],
        ["Mary Banda", "123456/10/1", "Permanent", "2026-10", 4400, 22, 22, 100, 0, "", "", "", "", ""],
    ),
}
PAYROLL_COLUMNS = {
    "employee": ("employee name", "employee", "full name", "name"),
    "nrc": ("nrc number", "nrc", "nrc no"),
    "employment_type": ("employment type", "employment status", "type"),
    "pay_month": ("pay month", "month", "pay period", "period", "date"),
    "basic_salary": ("basic salary", "basic", "salary", "basic wage", "wage"),
    "expected_days": ("expected days", "expected work days", "exp days"),
    "worked_days": ("worked days", "days worked", "work days", "actual days worked"),
    "bonuses": ("bonuses", "bonus", "allowances", "additions"),
    "deductions": ("deductions", "deduction", "other deductions"),
    "napsa": ("napsa", "employee napsa", "napsa employee"),
    "nhima": ("nhima", "employee nhima", "nhima employee"),
    "employer_napsa": ("employer napsa", "napsa employer"),
    "employer_nhima": ("employer nhima", "nhima employer"),
    "net_pay": ("net pay", "net payable", "net salary", "net"),
}
# Excel likes to turn "2026-10" into "Oct-26", so that spelling is accepted too
PAY_MONTH_FORMATS = ("%Y-%m", "%B %Y", "%b %Y", "%m/%Y", "%Y/%m", "%B-%Y", "%b-%Y", "%b-%y", "%B-%y")
DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y", "%Y/%m/%d",
)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


class ImportProblem(Exception):
    pass


def read_csv_upload(columns, required, one_of=()):
    """Returns the uploaded file's rows as dicts keyed by field name, each with its spreadsheet row number in '_row'.
    required headings must all be there; of one_of, at least one must be."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ImportProblem("Choose a CSV file first.")
    raw = upload.read()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("cp1252", errors="replace")

    # Excel saves CSV with semicolons in some regional settings
    first_line = content.split("\n", 1)[0]
    delimiter = max(",;\t", key=first_line.count)
    rows = list(csv.reader(io.StringIO(content, newline=""), delimiter=delimiter))
    if not rows:
        raise ImportProblem("That file is empty.")

    lookup = {alias: field for field, aliases in columns.items() for alias in aliases}
    fields = [lookup.get(" ".join(heading.replace("_", " ").lower().split())) for heading in rows[0]]
    # Headings such as "Bonus: Transport" or "Deduction: Loan" are free-form named amounts (payroll files)
    named = [re.fullmatch(r"\s*(bonus|deduction)\s*:\s*(.+?)\s*", heading, re.I) for heading in rows[0]]
    missing = [columns[field][0].title() for field in required if field not in fields]
    if missing:
        raise ImportProblem("The file is missing these column headings: " + ", ".join(missing) + ". Download the template to see the expected layout.")
    if one_of and not any(field in fields for field in one_of):
        raise ImportProblem("The file needs at least one of these column headings: " + ", ".join(columns[field][0].title() for field in one_of)
                            + ". Download the template to see the expected layout.")

    records = []
    for row_no, cells in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in cells):
            continue
        record = dict.fromkeys(columns, "")
        record["_named"] = []
        for field, extra, cell in zip(fields, named, cells):
            cell = cell.strip()
            if field:
                # Undo the apostrophe our own exports put in front of formula-like text
                record[field] = cell[1:] if len(cell) > 1 and cell[0] == "'" and cell[1] in "=@+-" else cell
            elif extra and cell:
                record["_named"].append((extra.group(1).lower(), extra.group(2), cell))
        record["_row"] = row_no
        records.append(record)
    if not records:
        raise ImportProblem("The file has headings but no data rows.")
    if len(records) > IMPORT_MAX_ROWS:
        raise ImportProblem(f"The file has {len(records)} rows. Import at most {IMPORT_MAX_ROWS} at a time by splitting it into smaller files.")
    return records


def parse_amount(raw):
    """Reads '1,250.50', 'ZMW 1250.5' or 'K1250.50' as a number."""
    # '12,5' or '12,50' with no dot is a decimal comma (common in semicolon-separated files); '1,250' is a thousands comma
    if "." not in raw and re.search(r",\d{1,2}$", raw.strip()):
        raw = raw.replace(".", "").replace(",", ".")
    value = float(re.sub(r"[^\d.\-]", "", raw))
    if not math.isfinite(value):
        raise ValueError(raw)
    return value


def parse_whole(raw):
    value = parse_amount(raw)
    if value != int(value):
        raise ValueError(raw)
    return int(value)


def parse_date(raw):
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(raw)


def tidy_nrc(value):
    return re.sub(r"\s+", "", value or "").upper()


def match_person(staff, name, nrc=""):
    """How a name (and NRC) in an import file relates to the registered employees. Returns (how, employees):
    "same"  - the NRC, or the whole name, is exactly one employee's
    "many"  - the name is exactly several employees' (an NRC tells them apart)
    "close" - no exact match, but the name is close to some employee's (best first): the person has to say whether it is them
    "none"  - nobody by that name
    When the file gives an NRC that is nobody's, only employees with no NRC recorded could be this person, and never without being asked."""
    key, nrc = name_key(name), tidy_nrc(nrc)
    if nrc:
        same = [e for e in staff if tidy_nrc(e["nrc_number"]) == nrc]
        if same:
            return ("same", same) if len(same) == 1 else ("many", same)
    pool = [e for e in staff if not nrc or not tidy_nrc(e["nrc_number"])]
    exact = [e for e in pool if name_key(e["full_name"]) == key]
    if exact and not nrc:
        return ("same", exact) if len(exact) == 1 else ("many", exact)
    near = sorted(
        (e for e in pool if e not in exact and names_alike(key, name_key(e["full_name"]))),
        key=lambda e: -difflib.SequenceMatcher(None, key, name_key(e["full_name"])).ratio(),
    )
    found = (exact + near)[:3]
    return ("close", found) if found else ("none", [])


def person_question(key, name, how, found, rows):
    """What the dialog box on the Import page asks about a name: who it might be, and the file rows it is on."""
    return {
        "key": key, "name": name, "how": how, "rows": rows,
        "candidates": [{"id": e["id"], "name": e["full_name"], "type": e["employment_type"] or "", "nrc": e["nrc_number"] or ""} for e in found],
    }


def read_name_choices(staff, allow_skip):
    """What was answered in that dialog box: {key: ("use", employee) | ("new",) | ("skip",)}. An answer that is not understood, or that
    names someone who is not one of this business's employees, counts as no answer at all."""
    try:
        raw = json.loads(request.form.get("name_choices") or "{}")
    except ValueError:
        return {}
    by_id = {e["id"]: e for e in staff}
    choices = {}
    for key, answer in (raw.items() if isinstance(raw, dict) else ()):
        if not isinstance(answer, str):
            continue
        if answer == "new":
            choices[key] = ("new",)
        elif answer == "skip" and allow_skip:
            choices[key] = ("skip",)
        elif answer.startswith("use:") and answer[4:].isascii() and answer[4:].isdigit() and int(answer[4:]) in by_id:
            choices[key] = ("use", by_id[int(answer[4:])])
    return choices


def name_check_reply(kind, asked, staff):
    """The answer to the Import page's check before anything is saved: the names to confirm, and everyone a name could be given to."""
    return jsonify(
        kind=kind, questions=[dict(q, rows=len(q["rows"])) for q in asked.values()],
        people=[{"id": e["id"], "name": e["full_name"]} for e in staff],
    )


def unconfirmed_names(asked):
    """Why an import stops when the dialog box was not answered (JavaScript off, or the file posted without it): never a guess."""
    return [
        f"'{q['name']}' (row{'s' if len(q['rows']) > 1 else ''} {', '.join(str(r) for r in q['rows'][:5])}{', ...' if len(q['rows']) > 5 else ''}) "
        "is not exactly the name of an employee, and nobody confirmed who it is. The Import page asks about such names in a dialog box before "
        "importing: try again with JavaScript on, or write the name as it is registered in HR & Payroll."
        for q in asked.values()
    ]


def import_errors(kind, errors):
    return render_template(
        "import.html", max_rows=IMPORT_MAX_ROWS, error_kind=kind, errors=errors[:IMPORT_MAX_ERRORS_SHOWN], error_count=len(errors), methods=PAYMENT_METHODS,
    )


@app.get("/import")
@need("admin")
def import_data():
    return render_template("import.html", max_rows=IMPORT_MAX_ROWS, methods=PAYMENT_METHODS)


@app.get("/import/template/<kind>.csv")
@need("admin")
def import_template(kind):
    if kind not in IMPORT_TEMPLATES:
        abort(404)
    header, example = IMPORT_TEMPLATES[kind]
    return csv_response(f"{kind}-import-template.csv", header, example if example and isinstance(example[0], (list, tuple)) else [example])


@app.post("/import/purchases")
@need("admin")
def import_purchases():
    db, biz = get_db(), g.user["business_id"]
    try:
        records = read_csv_upload(PURCHASE_COLUMNS, ("item_code", "quantity", "unit_cost"))
    except ImportProblem as e:
        flash(str(e), "error")
        return redirect(url_for("import_data"))
    default_method, problem = import_default_method()
    if problem:
        flash(problem, "error")
        return redirect(url_for("import_data"))

    known_names = {r["item_code"]: r["name"] for r in db.all("SELECT item_code, name FROM products WHERE business_id=?", (biz,))}
    # Items already in stock, plus new ones as the file introduces them, so a seed item's pack size is held to one value
    stocked = {r["item_code"]: r for r in db.all("SELECT item_code, category, department, seeds_per_pack FROM products WHERE business_id=?", (biz,))}
    departments = set(department_names(db, biz))
    known_categories = {norm(r["name"]): r["name"] for r in category_rows(db, biz)}
    known_units = {**{norm(u): u for u in DEFAULT_UNITS}, **used_units(db, biz)}
    today = now().strftime("%Y-%m-%d")
    errors, purchases, new_departments, new_categories = [], [], [], []

    for rec in records:
        problems = []
        code = rec["item_code"]
        # A purchase is also an inventory item unless the file says No. Without the column, every row is, as before.
        word = rec["in_inventory"].strip().lower()
        inventory = word not in NO_WORDS
        if word and word not in NO_WORDS and word not in YES_WORDS:
            problems.append(f"In Inventory '{rec['in_inventory']}' must be Yes or No")
        name = rec["name"] or (known_names.get(code, "") if inventory else "")
        if inventory and not code:
            problems.append("item code is missing")
        elif not name:
            problems.append("name is missing" + (" (needed because this item code is new)" if inventory else ""))
        elif not inventory and code and code in stocked:
            problems.append(f"'{code}' is an inventory item: answer Yes in In Inventory to add to its stock, or use a different code")

        qty = unit_cost = 0
        price = None
        try:
            qty = parse_whole(rec["quantity"])
            if qty <= 0:
                raise ValueError
        except ValueError:
            problems.append(f"quantity '{rec['quantity']}' is not a whole number above zero")
        try:
            unit_cost = parse_amount(rec["unit_cost"])
            if unit_cost < 0:
                raise ValueError
        except ValueError:
            problems.append(f"unit cost '{rec['unit_cost']}' is not a valid amount")
        if rec["price"]:
            try:
                price = parse_amount(rec["price"])
                if price < 0:
                    raise ValueError
            except ValueError:
                problems.append(f"selling price '{rec['price']}' is not a valid amount")

        dates = {}
        for field, label in (("purchase_date", "purchase date"), ("date_planted", "date planted"), ("expiry_date", "expiry date")):
            dates[field] = ""
            if rec[field]:
                try:
                    dates[field] = parse_date(rec[field]).strftime("%Y-%m-%d")
                except ValueError:
                    problems.append(f"{label} '{rec[field]}' is not a date (use YYYY-MM-DD or DD/MM/YYYY)")

        amount_paid = None
        if not problems:
            try:
                amount_paid = amount_for_status(rec["payment_status"], rec["amount_paid"], round(qty * unit_cost, 2))
            except ValueError as e:
                problems.append(str(e))

        # How it was paid. Optional in a file (older records have none). Paid to a named supplier, a method given needs its reference number.
        pay_method, pay_ref = default_method, ""
        if not problems and amount_paid and amount_paid > 0.005 and (rec["payment_method"] or rec["payment_reference"]):
            found_method = method_from_text(rec["payment_method"])
            if found_method is None:
                problems.append(f"payment method '{rec['payment_method']}' must be one of: " + ", ".join(METHOD_NAMES))
            elif rec["supplier_name"] and not rec["payment_reference"]:
                problems.append(f"a {found_method.lower()} payment needs its {METHOD_LABELS[found_method].lower()}")
            else:
                pay_method, pay_ref = found_method, rec["payment_reference"][:60]

        # A restock row is filed where the item already lives; a new item uses the category and department in the file,
        # matched to the managed lists ignoring capitals and spacing. Names that are new are added when the file is saved.
        already = stocked.get(code) if inventory else None
        typed_category = clean_name(rec["category"]) or "General"
        category = already["category"] if already else known_categories.get(norm(typed_category), typed_category)
        dept = already["department"] if already else (rec["department"] or "General")

        # Packs, units and selling prices belong to inventory items; a purchase that is only an expense has none of them
        seeds_per_pack, pack_problem = (seeds_per_pack_for(already, category, rec["seeds_per_pack"]) if inventory else (None, None))
        if pack_problem:
            problems.append(pack_problem)

        if problems:
            errors.append(f"Row {rec['_row']}: " + "; ".join(problems))
            continue

        # Seed items are counted in packs; otherwise the unit in the file, in the spelling already in use
        unit = ""
        if inventory:
            unit = SEED_UNIT if seeds_per_pack else canonical_unit(rec["unit"], known_units)
            if unit:
                known_units.setdefault(norm(unit), unit)
            known_names.setdefault(code, name)
        else:
            price = None
        if not already:
            if inventory:
                stocked[code] = {"category": category, "department": dept, "seeds_per_pack": seeds_per_pack}
            if norm(category) not in known_categories:
                known_categories[norm(category)] = category
                new_categories.append(category)
            if dept not in departments:
                departments.add(dept)
                new_departments.append(dept)
        purchases.append({
            "item_code": code, "name": name, "category": category, "department": dept,
            "quantity": qty, "unit_cost": round(unit_cost, 2), "price": None if price is None else round(price, 2),
            "supplier_name": rec["supplier_name"], "supplier_phone": rec["supplier_phone"], "payment_method": pay_method, "payment_reference": pay_ref,
            "invoice_no": rec["invoice_no"], "purchase_date": dates["purchase_date"] or today,
            "batch_no": rec["batch_no"] if inventory else "", "date_planted": dates["date_planted"] if inventory else "", "expiry_date": dates["expiry_date"],
            "amount_paid": amount_paid, "seeds_per_pack": seeds_per_pack, "unit": unit, "in_inventory": inventory,
        })

    if errors:
        return import_errors("purchases", errors)

    restocked = 0
    try:
        for dept in new_departments:
            db.execute("INSERT INTO departments (business_id, name) VALUES (?, ?)", (biz, dept))
        for category in new_categories:
            db.execute("INSERT INTO categories (business_id, name) VALUES (?, ?)", (biz, category))
        for p in purchases:
            if record_purchase(db, biz, p):
                restocked += 1
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The import failed while saving, so nothing was imported. Please try again.", "error")
        return redirect(url_for("import_data"))

    in_stock = sum(1 for p in purchases if p["in_inventory"])
    only_logged = len(purchases) - in_stock
    flash(
        f"Imported {len(purchases)} purchase row(s): {in_stock - restocked} new item(s) created, {restocked} added to existing stock"
        + (f", {only_logged} logged in Procurements only." if only_logged else "."), "ok",
    )
    return redirect(url_for("procurements"))


@app.post("/import/sales")
@need("admin")
def import_sales():
    db, biz = get_db(), g.user["business_id"]
    try:
        records = read_csv_upload(SALES_COLUMNS, ("quantity",))
    except ImportProblem as e:
        flash(str(e), "error")
        return redirect(url_for("import_data"))
    default_method, problem = import_default_method()
    if problem:
        flash(problem, "error")
        return redirect(url_for("import_data"))

    deduct = bool(request.form.get("deduct_stock"))
    listed = {r["item_code"]: r for r in db.all("SELECT id, item_code, name, department, batch_no, quantity, price FROM pos_products WHERE business_id=?", (biz,))}
    inventory_names = {r["item_code"]: r["name"] for r in db.all("SELECT item_code, name FROM products WHERE business_id=?", (biz,))}
    taken_numbers = {r["invoice_no"] for r in db.all("SELECT invoice_no FROM invoices WHERE business_id=?", (biz,))}

    # Rows sharing an invoice number form one invoice; rows without a number each become their own
    groups, by_number = [], {}
    for rec in records:
        number_given = rec["invoice_no"]
        if number_given and number_given in by_number:
            by_number[number_given].append(rec)
        else:
            rows = [rec]
            groups.append((number_given, rows))
            if number_given:
                by_number[number_given] = rows

    errors, invoices, needed = [], [], {}
    for number_given, rows in groups:
        first = rows[0]
        problems = []
        if number_given and number_given in taken_numbers:
            problems.append(f"invoice number '{number_given}' already exists (was this file imported before?)")

        when = now()
        if first["date"]:
            try:
                when = parse_date(first["date"])
            except ValueError:
                problems.append(f"date '{first['date']}' is not a date (use YYYY-MM-DD or DD/MM/YYYY)")

        lines, total = [], 0.0
        for rec in rows:
            row_problems = []
            code = rec["item_code"]
            pos_item = listed.get(code) if code else None
            name = rec["item_name"] or (pos_item["name"] if pos_item else inventory_names.get(code, ""))
            if not name:
                row_problems.append("item name is missing and the item code is not one of yours")

            qty, unit_price = 0, 0.0
            try:
                qty = parse_whole(rec["quantity"])
                if qty <= 0:
                    raise ValueError
            except ValueError:
                row_problems.append(f"quantity '{rec['quantity']}' is not a whole number above zero")
            if rec["unit_price"]:
                try:
                    unit_price = parse_amount(rec["unit_price"])
                    if unit_price < 0:
                        raise ValueError
                except ValueError:
                    row_problems.append(f"unit price '{rec['unit_price']}' is not a valid amount")
            elif pos_item:
                unit_price = pos_item["price"]
            else:
                row_problems.append("unit price is missing")
            if deduct and not pos_item:
                row_problems.append(f"item code '{code}' is not listed in the POS, so its stock cannot be deducted")

            if row_problems:
                errors.append(f"Row {rec['_row']}: " + "; ".join(row_problems))
                continue
            line_total = round(qty * unit_price, 2)
            total += line_total
            lines.append((code, name, pos_item["department"] if pos_item else "General", pos_item["batch_no"] if pos_item else "", qty, round(unit_price, 2), line_total))
            if deduct:
                needed[code] = needed.get(code, 0) + qty
        total = round(total, 2)

        status_text = first["payment_status"].lower()
        if status_text in ("", "paid"):
            status, amount_paid = "Paid", total
        elif status_text == "unpaid":
            status, amount_paid = "Unpaid", 0.0
        elif status_text.startswith("partial"):
            status, amount_paid = "Partial Payment", 0.0
            try:
                amount_paid = round(parse_amount(first["amount_paid"]), 2)
                if len(lines) == len(rows) and not 0 < amount_paid < total:
                    raise ValueError
            except ValueError:
                problems.append(f"amount paid '{first['amount_paid']}' must be more than zero and less than the invoice total for a partial payment")
        else:
            status, amount_paid = "Paid", total
            problems.append(f"payment status '{first['payment_status']}' must be Paid, Partial Payment or Unpaid")

        # How it was paid, for Accounts. Optional (older records have none); a method given must be one the app knows.
        pay_method = default_method
        if first.get("payment_method"):
            pay_method = method_from_text(first["payment_method"])
            if pay_method is None:
                problems.append(f"payment method '{first['payment_method']}' must be one of: " + ", ".join(METHOD_NAMES))
        first["pay_method"] = pay_method or NOT_RECORDED

        if problems:
            errors.append(f"Row {first['_row']}: " + "; ".join(problems))
        invoices.append((number_given, first, when, status, total, amount_paid, lines))

    for code, qty in needed.items():
        if qty > listed[code]["quantity"]:
            errors.append(f"Stock: the file sells {qty} of '{code}' but only {listed[code]['quantity']} are listed in the POS.")

    if errors:
        return import_errors("sales", errors)

    year, seq = now().strftime("%Y"), 1
    try:
        for number_given, first, when, status, total, amount_paid, lines in invoices:
            invoice_no = number_given
            while not invoice_no:
                candidate = f"INV-{year}-{seq:04d}"
                seq += 1
                if candidate not in taken_numbers:
                    invoice_no = candidate
            taken_numbers.add(invoice_no)

            sold_at = when.strftime("%Y-%m-%d %H:%M:%S")
            invoice_id = db.insert(
                """INSERT INTO invoices (business_id, invoice_no, kind, customer_name, customer_phone, payment_status, total, amount_paid, balance_due, created_by, created_at, customer_id)
                   VALUES (?, ?, 'SALE', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    biz, invoice_no, first["customer_name"] or WALK_IN, first["customer_phone"], status, total, amount_paid,
                    round(total - amount_paid, 2), g.user["username"] + " (import)", sold_at,
                    customer_for(db, biz, first["customer_name"], first["customer_phone"], sold_at),
                ),
            )
            record_money(
                db, biz, "in", amount_paid, first["pay_method"], "sale", (first.get("payment_reference") or "")[:60], first["customer_name"] or WALK_IN,
                f"Invoice {invoice_no} (imported)", "invoice", invoice_id, sold_at,
            )
            for code, name, dept, batch, qty, unit_price, line_total in lines:
                db.execute(
                    """INSERT INTO invoice_items (invoice_id, business_id, item_code, item_name, department, batch_no, qty, unit_price, line_total)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (invoice_id, biz, code, name, dept, batch, qty, unit_price, line_total),
                )
        for code, qty in needed.items():
            low_stock_alert(db, listed[code]["name"], code, listed[code]["quantity"], listed[code]["quantity"] - qty, "listed for sale in the POS")
            moved = db.execute(
                "UPDATE pos_products SET quantity = quantity - ? WHERE id=? AND business_id=? AND quantity >= ?",
                (qty, listed[code]["id"], biz, qty),
            )
            if moved.rowcount != 1:
                raise StockError(code)
        db.commit()
    except StockError:
        db.rollback()
        flash("POS stock changed during the import, so nothing was imported. Please try again.", "error")
        return redirect(url_for("import_data"))
    except DB_ERRORS:
        db.rollback()
        flash("The import failed while saving, so nothing was imported. Please try again.", "error")
        return redirect(url_for("import_data"))

    stock_note = " POS stock was reduced to match." if deduct else " Stock levels were not changed."
    flash(f"Imported {len(invoices)} invoice(s) from {len(records)} row(s).{stock_note}", "ok")
    return redirect(url_for("history"))


def import_default_method():
    """How the money in an imported file was paid, chosen on the upload form: (method, problem). A row's own Payment Method
    column overrides it. 'Not recorded' suits records from before the balances were counted, which never move them anyway."""
    if "default_method" not in request.form:
        return NOT_RECORDED, None
    raw = text("default_method")
    if raw == "not_recorded":
        return NOT_RECORDED, None
    method = method_from_text(raw)
    if method not in METHOD_NAMES:
        return None, "Choose how the money in this file was paid (a row's own Payment Method still counts first), or Not recorded for old records."
    return method, None


def parse_pay_month(raw):
    for fmt in PAY_MONTH_FORMATS:
        try:
            return datetime.datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return parse_date(raw.strip())


@app.post("/import/payroll")
@need("admin")
def import_payroll():
    db, biz = get_db(), g.user["business_id"]
    try:
        records = read_csv_upload(PAYROLL_COLUMNS, ("employee", "pay_month", "basic_salary"))
    except ImportProblem as e:
        flash(str(e), "error")
        return redirect(url_for("import_data"))
    default_method, problem = import_default_method()
    if problem:
        flash(problem, "error")
        return redirect(url_for("import_data"))

    allow_repeats = bool(request.form.get("allow_repeats"))
    hr_set = hr_settings_for(db, biz)
    non_working = hr_set["non_working_days"].split(",")
    staff = db.all("SELECT id, full_name, nrc_number, employment_type FROM employees WHERE business_id=?", (biz,))
    already_paid = {(r["employee_id"], r["pay_month"]) for r in db.all("SELECT employee_id, pay_month FROM payroll WHERE business_id=?", (biz,))}
    check = request.form.get("check") == "1"   # the Import page asks first which names need confirming; nothing is saved
    choices = read_name_choices(staff, allow_skip=False)
    asked = {}

    def optional_amount(rec, field, label, problems, minimum=0.0):
        """A blank cell means 'work it out'; anything else must be a valid amount."""
        if not rec[field]:
            return None
        try:
            value = parse_amount(rec[field])
            if minimum is not None and value < minimum:
                raise ValueError
            return value
        except ValueError:
            problems.append(f"{label} '{rec[field]}' is not a valid amount")
            return None

    errors, slips, hires = [], [], {}
    for rec in records:
        problems = []
        name, nrc = rec["employee"], tidy_nrc(rec["nrc"])
        if not name:
            problems.append("employee name is missing")

        period = None
        try:
            period = parse_pay_month(rec["pay_month"])
        except ValueError:
            problems.append(f"pay month '{rec['pay_month']}' is not a month (use YYYY-MM or October 2026)")

        basic = 0.0
        try:
            basic = parse_amount(rec["basic_salary"])
            if basic < 0:
                raise ValueError
        except ValueError:
            problems.append(f"basic salary '{rec['basic_salary']}' is not a valid amount")

        expected = optional_amount(rec, "expected_days", "expected days", problems)
        worked = optional_amount(rec, "worked_days", "worked days", problems)
        napsa = optional_amount(rec, "napsa", "NAPSA", problems)
        nhima = optional_amount(rec, "nhima", "NHIMA", problems)
        employer_napsa = optional_amount(rec, "employer_napsa", "employer NAPSA", problems)
        employer_nhima = optional_amount(rec, "employer_nhima", "employer NHIMA", problems)
        net = optional_amount(rec, "net_pay", "net pay", problems, minimum=None)
        if expected is not None and expected <= 0:
            problems.append("expected days must be above zero")

        named = {"bonus": {}, "deduction": {}}
        totals = (("bonus", "Bonuses", rec["bonuses"]), ("deduction", "Deductions", rec["deductions"]))
        for kind, label, raw in list(totals) + rec["_named"]:
            if not raw:
                continue
            try:
                value = parse_amount(raw)
                if value < 0:
                    raise ValueError
            except ValueError:
                problems.append(f"{kind} '{label}' amount '{raw}' is not a valid amount")
                continue
            if value:
                named[kind][label] = round(named[kind].get(label, 0.0) + value, 2)

        # Find the employee by NRC when the file has one, otherwise by name. A name that is only close to an employee's is asked about in a
        # dialog box on the Import page (is it them, or someone new?); anyone with no close match is registered
        employee_id, hire_key = None, None
        if name:
            how, found = match_person(staff, name, nrc)
            pkey = name_key(name) + "|" + nrc
            answer = choices.get(pkey)
            if how == "same":
                employee_id, name = found[0]["id"], found[0]["full_name"]   # the payslip carries the name the employee is registered under
            elif how == "many":
                problems.append(f"more than one employee matches '{name}'; add an NRC Number column to tell them apart")
            elif how == "none" or (answer and answer[0] == "new"):
                hire_key = nrc or " ".join(name.lower().split())
            elif answer:
                employee_id, name = answer[1]["id"], answer[1]["full_name"]
            else:
                asked.setdefault(pkey, person_question(pkey, name, how, found, []))["rows"].append(rec["_row"])

        if period and employee_id and not allow_repeats and (employee_id, period.strftime("%Y-%m")) in already_paid:
            problems.append(f"{name} already has a payslip for {period.strftime('%B %Y')} (was this file imported before?)")

        if problems:
            errors.append(f"Row {rec['_row']}: " + "; ".join(problems))
            continue

        if expected is None:
            expected = float(expected_work_days(period.year, period.month, non_working)) or 22.0
        if worked is None:
            worked = expected
        calculated_basic = round(basic / expected * worked, 2)
        # Blank cells follow the statutory rules in HR Settings, each side on its own; amounts in the file are kept as given
        share = statutory_amounts(hr_set, calculated_basic, sum(named["bonus"].values()))
        napsa = share["napsa_employee"] if napsa is None else napsa
        nhima = share["nhima_employee"] if nhima is None else nhima
        employer_napsa = share["napsa_employer"] if employer_napsa is None else round(employer_napsa, 2)
        employer_nhima = share["nhima_employer"] if employer_nhima is None else round(employer_nhima, 2)
        if net is None:
            net = calculated_basic + sum(named["bonus"].values()) - sum(named["deduction"].values()) - napsa - nhima

        if hire_key and hire_key not in hires:
            casual = rec["employment_type"].lower().startswith(("casual", "temp", "part"))
            hires[hire_key] = {"name": name, "nrc": rec["nrc"], "salary": round(basic, 2), "type": "Casual Worker" if casual else "Permanent", "since": period.strftime("%Y-%m-01")}
        slips.append({
            "employee_id": employee_id, "hire_key": hire_key, "name": name, "month": period.strftime("%Y-%m"), "basic": round(basic, 2),
            "expected": expected, "worked": worked, "calculated_basic": calculated_basic, "bonuses": named["bonus"], "deductions": named["deduction"],
            "napsa": round(napsa, 2), "nhima": round(nhima, 2), "employer_napsa": employer_napsa, "employer_nhima": employer_nhima, "net": round(net, 2),
        })

    if errors:
        return jsonify(questions=[]) if check else import_errors("salaries and wages", errors)
    if check:
        return name_check_reply("payroll", asked, staff)
    if asked:
        return import_errors("salaries and wages", unconfirmed_names(asked))

    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        new_ids = {}
        for key, hire in hires.items():
            new_ids[key] = db.insert(
                """INSERT INTO employees (business_id, full_name, nrc_number, basic_salary, employment_type, address, age, start_date, expected_days_week)
                   VALUES (?, ?, ?, ?, ?, '', NULL, ?, 5)""",
                (biz, hire["name"], hire["nrc"], hire["salary"], hire["type"], hire["since"]),
            )
        for s in slips:
            slip_id = db.insert(
                """INSERT INTO payroll (business_id, employee_id, employee_name, pay_month, basic_salary, expected_days, worked_days, calculated_basic,
                                        bonuses_json, deductions_json, napsa_amt, nhima_amt, employer_napsa, employer_nhima, net_payable, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    biz, s["employee_id"] or new_ids[s["hire_key"]], s["name"], s["month"], s["basic"], s["expected"], s["worked"], s["calculated_basic"],
                    json.dumps(s["bonuses"]), json.dumps(s["deductions"]), s["napsa"], s["nhima"], s["employer_napsa"], s["employer_nhima"], s["net"], stamp,
                ),
            )
            # Imported pay was paid out at the time, dated here at the end of its pay month, by the method chosen for the file
            year, month = int(s["month"][:4]), int(s["month"][5:7])
            record_payslip_money(
                db, biz, slip_id, s["name"], s["month"], s["net"], s["napsa"] + s["nhima"] + s["employer_napsa"] + s["employer_nhima"], default_method,
                f"{s['month']}-{calendar.monthrange(year, month)[1]:02d} 00:00:00",
            )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The import failed while saving, so nothing was imported. Please try again.", "error")
        return redirect(url_for("import_data"))

    flash(f"Imported {len(slips)} payslip(s). {len(hires)} new employee(s) were registered from the file.", "ok")
    return redirect(url_for("hr"))


@app.errorhandler(413)
def upload_too_large(error):
    if request.path.startswith("/assistant/"):   # the chat panel reads JSON
        return jsonify({"error": "That file is too big. Send a photo, or a PDF under 2 MB."}), 413
    flash("That file is too large. The limit is 2 MB; split it into smaller files.", "error")
    return redirect(url_for("import_data"))


# --- PLANTING RECORDS (GREENHOUSE) ---
# A planting is a batch of seeds put in the ground. The seedlings expected are the seeds planted less the expected mortality.
# A batch appears on the POS RELEASE_DAYS before it is expected to be ready (at once if it is marked ready sooner), under its
# own code so that its sales can be told apart from other batches. Pre-orders hold seedlings from a batch straight away but only
# become a sale when the customer collects them and an admin has approved it.
RELEASE_DAYS = 7
PLANTING_MARK = "-SDL-P"
PLANTING_STAGES = {"growing": "Growing", "due": "Due", "ready": "Ready", "record": "Historical record"}


def today_str():
    return now().strftime("%Y-%m-%d")


def add_days(day, days):
    return (datetime.datetime.strptime(day, "%Y-%m-%d") + datetime.timedelta(days=days)).strftime("%Y-%m-%d")


def day_or_none(raw):
    """A date typed or pasted in any of the accepted layouts as YYYY-MM-DD, or None when it is not a date."""
    try:
        return parse_date(raw.strip()).strftime("%Y-%m-%d")
    except ValueError:
        return None


def seedlings_from(seeds, mortality):
    return math.floor(seeds * (100 - mortality) / 100 + 1e-9)


def planting_label(p):
    """What the batch is called on the POS and on invoices, so two batches of the same plant can be told apart."""
    return f"{p['name']} ({p['batch_no'] or 'planted ' + (p['planted_on'] or '')})"


def on_pos_now(p, today):
    # A historical record is never listed, however old or "ready" its dates say it is - it is not really there
    if not p["still_in_greenhouse"]:
        return False
    return bool(p["actual_ready_on"]) or (p["ready_on"] or today) <= add_days(today, RELEASE_DAYS)


def can_plant():
    return can("procure") or can("transfer")


def with_figures(db, biz, rows):
    """Adds to each planting what has been sold, what is held by pre-orders, what is left, and where it stands."""
    today = today_str()
    sold = {
        r["code"]: r["n"]
        for r in db.all(
            """SELECT ii.item_code AS code, SUM(ii.qty) AS n FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
               WHERE ii.business_id=? AND ii.item_code LIKE ? AND i.payment_status <> 'Cancelled' GROUP BY ii.item_code""",
            (biz, "%" + PLANTING_MARK + "%"),
        )
    }
    held = {}
    # A collected pre-order counts only while its invoice exists and is not cancelled: deleting or cancelling that
    # invoice puts the seedlings back
    for r in db.all(
        """SELECT planting_id, status, SUM(qty) AS n FROM preorders po WHERE business_id=?
           AND (status='pending' OR (status='collected' AND EXISTS (SELECT 1 FROM invoices i WHERE i.id = po.invoice_id AND i.payment_status <> 'Cancelled')))
           GROUP BY planting_id, status""",
        (biz,),
    ):
        held[(r["planting_id"], r["status"])] = r["n"] or 0
    out = []
    for p in rows:
        s = int(sold.get(p["pos_code"], 0) or 0)
        pending = int(held.get((p["id"], "pending"), 0))
        collected = int(held.get((p["id"], "collected"), 0))
        ready_on = p["ready_on"] or today
        if not p["still_in_greenhouse"]:
            stage = "record"
        else:
            stage = "ready" if p["actual_ready_on"] else ("due" if ready_on <= today else "growing")
        days = (datetime.date.fromisoformat(ready_on) - datetime.date.fromisoformat(today)).days
        # A historical record cannot be sold or pre-ordered, whatever its seedling count says
        available = max(0, p["seedlings"] - s - pending) if p["still_in_greenhouse"] else 0
        out.append(dict(
            p, sold=s, pending=pending, collected=collected, direct=max(0, s - collected), available=available,
            stage=stage, stage_label=PLANTING_STAGES[stage], days=days, on_pos=on_pos_now(p, today), label=planting_label(p),
            goes_on_pos=add_days(ready_on, -RELEASE_DAYS),
        ))
    return out


def put_on_pos(db, biz, f):
    """Makes the POS listing of a released batch match the batch: its name, price and what is left to sell."""
    moved = db.execute(
        "UPDATE pos_products SET name=?, department=?, batch_no=?, date_planted=?, price=?, quantity=? WHERE business_id=? AND item_code=?",
        (f["label"], f["department"], f["batch_no"], f["planted_on"], f["price"], f["available"], biz, f["pos_code"]),
    ).rowcount
    if moved == 0:
        db.execute(
            """INSERT INTO pos_products (business_id, item_code, name, category, department, batch_no, date_planted, quantity, price)
               VALUES (?, ?, ?, 'Seedlings', ?, ?, ?, ?, ?)""",
            (biz, f["pos_code"], f["label"], f["department"], f["batch_no"], f["planted_on"], f["available"], f["price"]),
        )


def sync_planting(db, biz, planting_id):
    p = db.one("SELECT * FROM plantings WHERE id=? AND business_id=?", (planting_id, biz))
    if p and p["released"]:
        put_on_pos(db, biz, with_figures(db, biz, [p])[0])


def release_plantings(db, biz):
    """Lists on the POS every batch that is now within a week of being ready (or already marked ready). Safe to repeat.
    Returns the batches that are still not on the POS."""
    today = today_str()
    unreleased = db.all("SELECT * FROM plantings WHERE business_id=? AND released=0", (biz,))
    waiting = [p for p in unreleased if on_pos_now(p, today)]
    if not waiting:
        return unreleased
    try:
        for f in with_figures(db, biz, waiting):
            db.execute("UPDATE plantings SET released=1 WHERE id=? AND business_id=?", (f["id"], biz))
            put_on_pos(db, biz, f)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        return unreleased
    released = {p["id"] for p in waiting}
    return [p for p in unreleased if p["id"] not in released]


def up_next_summary(db, biz, unreleased=None, preorders=None):
    """For the POS tile: batches still growing that are not on the POS yet, and pre-orders waiting for collection. The POS passes
    what it has read already: the batches not on the POS (release_plantings) and the pending pre-orders' count and quantity."""
    if unreleased is None:
        unreleased = db.all("SELECT * FROM plantings WHERE business_id=? AND released=0", (biz,))
    growing = sorted((p for p in unreleased if p["still_in_greenhouse"]), key=lambda p: (p["ready_on"] or "", p["id"]))
    rows = with_figures(db, biz, growing)
    if preorders is None:
        preorders = db.one("SELECT COUNT(*) AS preorders, COALESCE(SUM(qty), 0) AS preorder_qty FROM preorders WHERE business_id=? AND status='pending'", (biz,))
    return {"batches": len(rows), "next": rows[0] if rows else None, "preorders": preorders["preorders"], "preorder_qty": preorders["preorder_qty"]}


def insert_planting(db, biz, d):
    """Saves a planting and gives it its POS code. d carries every field; the caller has already checked them."""
    planting_id = db.insert(
        """INSERT INTO plantings (business_id, seed_item_id, seed_code, seed_name, name, department, batch_no, source, seeds_planted, packs_used, loose_delta,
                                  seeds_per_pack, mortality_pct, seedlings, seed_unit_cost, cost_per_seedling, price, planted_on, ready_on, actual_ready_on,
                                  still_in_greenhouse, notes, created_by, created_at, trays, holes_per_tray)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            biz, d.get("seed_item_id"), d["seed_code"], d["seed_name"], d["name"], d["department"], d["batch_no"], d["source"], d["seeds"],
            d.get("packs", 0), d.get("loose_delta", 0), d.get("seeds_per_pack"), d["mortality"], d["seedlings"], d["seed_cost"], d["cost_per_seedling"],
            d["price"], d["planted_on"], d["ready_on"], d.get("actual_ready_on"), 1 if d.get("still_in_greenhouse", True) else 0,
            d.get("notes", ""), g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S"), d.get("trays"), d.get("holes_per_tray"),
        ),
    )
    db.execute("UPDATE plantings SET pos_code=? WHERE id=?", (f"{d['seed_code']}{PLANTING_MARK}{planting_id}", planting_id))
    if d.get("still_in_greenhouse", True):
        requests_arrived(db, biz, d["name"], "", f"{d['seedlings']:,} seedlings planted, ready about {d['ready_on']}")
    return planting_id


def seed_code_for(name):
    return re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:20] or "PLT"


def tray_count(raw):
    """A number of trays: above zero and, because a tray can be part planted, not necessarily whole. Raises ValueError otherwise."""
    value = float(str(raw).replace(",", ""))
    if not math.isfinite(value) or not 0 < value <= MAX_TRAYS:
        raise ValueError(raw)
    return round(value, 2)


def planting_staff(db, biz):
    """The employees a planting's trays can be given to: the casual workers (paid by the tray) first, with their rate for a tray, then
    everyone else. Nothing else about their pay."""
    hr_set = hr_settings_for(db, biz)
    rows = db.all("""SELECT id, full_name, employment_type, tray_rate FROM employees WHERE business_id=?
                     ORDER BY CASE WHEN employment_type='Casual Worker' THEN 0 ELSE 1 END, full_name""", (biz,))
    return [dict(r, rate=rate_for(r, hr_set) if is_casual(r) else None) for r in rows]


def read_trays_form(db, biz, paid=0.0):
    """The trays part of the planting form: how many trays, how many holes in each, and who planted them.
    Returns (info, None) with info = {trays, holes, rows: [(employee, trays)]}, or (None, what is wrong).
    The trays given to the people are added up, and that sum is the number of trays planted; if a total was typed as well it must agree.
    paid is the trays already paid on a payslip, which stay as they are: they count towards the total but are not in the rows."""
    typed, holes = None, None
    try:
        if text("trays_planted"):
            typed = tray_count(text("trays_planted"))
        if text("holes_per_tray"):
            holes = number("holes_per_tray", int)
            if not 1 <= holes <= MAX_TRAY_HOLES:
                raise ValueError
    except ValueError:
        return None, f"Trays planted must be a number above zero, and the holes in a tray a whole number from 1 to {MAX_TRAY_HOLES:,}."
    staff = {e["id"]: e for e in planting_staff(db, biz)}
    pairs = [(who.strip(), amount.strip()) for who, amount in zip(request.form.getlist("planter_id"), request.form.getlist("planter_trays")) if who.strip() or amount.strip()]
    given = {}
    for who, amount in pairs:
        employee = staff.get(int(who)) if who.isascii() and who.isdigit() else None
        if employee is None:
            return None, "Choose the employee for each row of who planted the trays, or remove the row."
        if not amount and len(pairs) == 1 and typed:
            amount = str(typed)   # one person and the total typed: they planted all of it
        try:
            share = tray_count(amount)
        except ValueError:
            return None, f"Enter how many trays {employee['full_name']} planted (a number above zero), or remove the row."
        given[employee["id"]] = [employee, round(given[employee["id"]][1] + share, 2) if employee["id"] in given else share]
    rows = [(employee, share) for employee, share in given.values()]
    if rows or paid:
        total = round(paid + sum(share for _, share in rows), 2)
        if typed is not None and abs(typed - total) > 0.005:
            return None, f"The trays given to each person add up to {tray_figure(total)}, but {tray_figure(typed)} trays were entered in total. Make them agree, or leave the total empty."
        typed = total
    return {"trays": typed, "holes": holes, "rows": rows}, None


def save_tray_rows(db, biz, planting_id, rows):
    """Records who planted how many trays of a planting."""
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    for employee, share in rows:
        db.execute(
            """INSERT INTO planting_trays (business_id, planting_id, employee_id, employee_name, trays, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (biz, planting_id, employee["id"], employee["full_name"], share, g.user["username"], stamp),
        )


class NotEnoughSeeds(Exception):
    """A planting needs more seeds than the item has in stock; the message says what there is."""


def take_seeds(db, biz, item, size, seeds):
    """Takes the seeds for a planting out of a seed item: loose seeds left from an earlier opened pack first, then whole packs (the
    rest of an opened pack is kept as loose seeds). Returns (packs taken, change in loose seeds, loose seeds left).
    Raises NotEnoughSeeds when the stock is short, StockError when it changed underneath. The caller commits or rolls back."""
    loose = item["loose_seeds"] or 0
    from_loose = min(loose, seeds)
    need_seeds = seeds - from_loose
    packs = -(-need_seeds // size)
    if packs > item["quantity"]:
        available = item["quantity"] * size + loose
        raise NotEnoughSeeds(f"Only {available:,} seeds of {item['name']} are in stock ({item['quantity']} pack(s) of {size:,}"
                             + (f" and {loose:,} loose seeds" if loose else "") + ").")
    loose_left = loose - from_loose + (packs * size - need_seeds)
    moved = db.execute(
        "UPDATE products SET quantity = quantity - ?, loose_seeds=?, seeds_per_pack = COALESCE(seeds_per_pack, ?) WHERE id=? AND business_id=? AND quantity >= ?",
        (packs, loose_left, size, item["id"], biz, packs),
    )
    if moved.rowcount != 1:
        raise StockError(item["name"])
    if packs:
        low_stock_alert(db, item["name"], item["item_code"], item["quantity"], item["quantity"] - packs, "in inventory")
    return packs, loose_left - loose, loose_left


@app.get("/planting")
@need(None)
def planting_list():
    db, biz = get_db(), g.user["business_id"]
    release_plantings(db, biz)
    f = planting_filters(db, biz)
    where, params = "business_id=?", [biz]
    for word in norm(f["q"]).split():
        like = f"%{word}%"
        where += """ AND (LOWER(name) LIKE ? OR LOWER(seed_name) LIKE ? OR LOWER(seed_code) LIKE ? OR LOWER(COALESCE(batch_no, '')) LIKE ?
                          OR LOWER(COALESCE(pos_code, '')) LIKE ? OR LOWER(COALESCE(notes, '')) LIKE ? OR LOWER(COALESCE(department, '')) LIKE ?)"""
        params += [like] * 7
    # "Ready" is the day it was marked ready, or the day it is expected to be until then
    for column, low, high in (("planted_on", "planted_from", "planted_to"), ("COALESCE(actual_ready_on, ready_on)", "ready_from", "ready_to")):
        if f[low]:
            where += f" AND {column} >= ?"
            params.append(f[low])
        if f[high]:
            where += f" AND {column} <= ?"
            params.append(f[high])
    if f["department"]:
        where += " AND department=?"
        params.append(f["department"])
    rows = with_figures(db, biz, db.all(f"SELECT * FROM plantings WHERE {where} ORDER BY ready_on DESC, id DESC LIMIT 500", params))
    show = request.args.get("show", "all")
    counts = {
        "all": len(rows), "growing": sum(1 for r in rows if r["stage"] in ("growing", "due")), "ready": sum(1 for r in rows if r["stage"] == "ready"),
        "records": sum(1 for r in rows if r["stage"] == "record"),
    }
    if show == "growing":
        rows = [r for r in rows if r["stage"] in ("growing", "due")]
    elif show == "ready":
        rows = [r for r in rows if r["stage"] == "ready"]
    elif show == "records":
        rows = [r for r in rows if r["stage"] == "record"]
    else:
        show = "all"
    keep = {key: value for key, value in f.items() if value and key != "filtered"}
    today = today_str()
    planted_today = trays_by_planter(db, biz, today)
    return render_template(
        "plantings.html", rows=rows, show=show, counts=counts, editable=can_plant(), f=f, keep=keep, departments=department_names(db, biz),
        planters=planters(db, biz), planted_today=planted_today, today=today,
        tab=lambda name: url_for("planting_list", **({"show": name} if name != "all" else {}), **keep),
    )


def planting_filters(db, biz):
    """The search and date filters on Planting Records. A range typed back to front is turned round."""
    f = {"q": " ".join(request.args.get("q", "").split())[:100]}
    for low, high in (("planted_from", "planted_to"), ("ready_from", "ready_to")):
        f[low] = day_or_none(request.args.get(low, "")) if request.args.get(low) else None
        f[high] = day_or_none(request.args.get(high, "")) if request.args.get(high) else None
        if f[low] and f[high] and f[high] < f[low]:
            f[low], f[high] = f[high], f[low]
    f["department"] = request.args.get("department", "") if request.args.get("department", "") in department_names(db, biz) else ""
    f["filtered"] = any(f[key] for key in ("q", "planted_from", "planted_to", "ready_from", "ready_to", "department"))
    return f


# --- Planting by planter ---
# Planting Records shows a tile for each person who plants trays (ticked in HR). A tile opens that person's day: every entry is a seed
# item from inventory and the trays they planted of it. An entry joins the batch already planted that day from the same item in trays
# with the same holes (still growing, not marked ready), so several people planting tomatoes on one day make one batch, with each
# person's trays recorded against it; otherwise it starts a new batch. The seeds come out of inventory as on the full planting form.
def planters(db, biz):
    return db.all(f"SELECT id, full_name, employment_type, plants FROM employees WHERE business_id=? AND {PLANTER_SQL} ORDER BY full_name", (biz,))


def trays_by_planter(db, biz, day):
    """{employee_id: {trays, batches}} for the plantings dated on a day."""
    rows = db.all(
        """SELECT t.employee_id, SUM(t.trays) AS trays, COUNT(DISTINCT t.planting_id) AS batches
           FROM planting_trays t JOIN plantings p ON p.id = t.planting_id AND p.business_id = t.business_id
           WHERE t.business_id=? AND p.planted_on=? AND t.employee_id IS NOT NULL GROUP BY t.employee_id""",
        (biz, day),
    )
    return {r["employee_id"]: r for r in rows}


def day_batch(db, biz, item_id, day, holes):
    """The batch an entry joins: planted on that day from the item, in trays with these holes, still growing and not counted yet."""
    return db.one(
        """SELECT * FROM plantings WHERE business_id=? AND seed_item_id=? AND planted_on=? AND COALESCE(holes_per_tray, 0)=?
           AND still_in_greenhouse=1 AND actual_ready_on IS NULL ORDER BY id DESC LIMIT 1""",
        (biz, item_id, day, holes),
    )


def grow_days(p):
    try:
        return (datetime.date.fromisoformat(p["ready_on"]) - datetime.date.fromisoformat(p["planted_on"])).days
    except (TypeError, ValueError):
        return None


def planter_seed_items(db, biz, day):
    """The seed items an entry can be planted from, and what the form fills in for each: the stock, the mortality and growing time of
    the item's latest planting, and the batches already planted from it on the day (by holes per tray), which an entry would join."""
    latest = {}
    for p in db.all("SELECT seed_item_id, mortality_pct, planted_on, ready_on, price FROM plantings WHERE business_id=? AND seed_item_id IS NOT NULL ORDER BY id", (biz,)):
        latest[p["seed_item_id"]] = p
    joins = {}
    for p in db.all(
        """SELECT * FROM plantings WHERE business_id=? AND seed_item_id IS NOT NULL AND planted_on=? AND still_in_greenhouse=1 AND actual_ready_on IS NULL
           ORDER BY id""",
        (biz, day),
    ):
        joins.setdefault(p["seed_item_id"], {})[str(p["holes_per_tray"] or 0)] = {
            "label": planting_label(p), "mortality": p["mortality_pct"], "ready": p["ready_on"],
        }
    items, info = [], {}
    for i in db.all("SELECT id, item_code, name, category, quantity, seeds_per_pack, loose_seeds FROM products WHERE business_id=? ORDER BY name", (biz,)):
        if not is_seed_category(i["category"]):
            continue
        i["allowed"] = item_grant(db, i["id"], "transfer") is not None
        i["usable"] = i["allowed"] and bool(i["seeds_per_pack"]) and (i["quantity"] > 0 or (i["loose_seeds"] or 0) > 0)
        items.append(i)
        last = latest.get(i["id"])
        info[str(i["id"])] = {
            "name": i["name"], "packs": i["quantity"], "loose": i["loose_seeds"] or 0, "size": i["seeds_per_pack"] or 0,
            "mortality": last["mortality_pct"] if last else None, "grow": grow_days(last) if last else None, "batches": joins.get(i["id"], {}),
        }
    return items, info


@app.get("/planting/planter/<int:employee_id>")
@need(None)
def planter_day(employee_id):
    db, biz = get_db(), g.user["business_id"]
    e = get_employee(db, employee_id)
    day = activity_day()
    shown = datetime.date.fromisoformat(day)
    items, info = planter_seed_items(db, biz, day)
    done = db.all(
        """SELECT t.id, t.trays, t.paid_payslip_id, p.id AS planting_id, p.name, p.batch_no, p.planted_on, p.holes_per_tray, p.seeds_planted,
                  p.trays AS batch_trays, p.ready_on, p.actual_ready_on
           FROM planting_trays t JOIN plantings p ON p.id = t.planting_id AND p.business_id = t.business_id
           WHERE t.business_id=? AND t.employee_id=? AND p.planted_on=? ORDER BY t.id""",
        (biz, employee_id, day),
    )
    for r in done:
        r["label"] = planting_label(r)
    hr_set = hr_settings_for(db, biz)
    return render_template(
        "planter_day.html", e=e, day=day, today=today_str(), shown=shown, items=items, info=info, done=done,
        total=round(sum(r["trays"] for r in done), 2), usual_holes=hr_set["holes_per_tray"] or "", max_holes=MAX_TRAY_HOLES,
        rate=rate_for(e, hr_set) if is_casual(e) else None, planter=is_planter(e),
        prev_day=(shown - datetime.timedelta(days=1)).isoformat(), next_day=(shown + datetime.timedelta(days=1)).isoformat(),
    )


def read_planter_entries(db, biz, day):
    """The entries on a planter's day form: ([{item, grant, size, trays, holes, mortality, ready_on}], problems). A row left empty is skipped."""
    form = {key: request.form.getlist(key) for key in ("item_id", "trays", "holes", "mortality_pct", "ready_on")}
    usual = hr_settings_for(db, biz)["holes_per_tray"] or None
    entries, problems, earlier = [], [], set()
    for i in range(len(form["item_id"])):
        value = lambda key: (form[key][i] if i < len(form[key]) else "").strip()  # noqa: E731
        if not value("item_id") and not value("trays"):
            continue
        where = f"Entry {i + 1}"
        item = None
        if value("item_id").isascii() and value("item_id").isdigit():
            item = db.one("SELECT " + ITEM_COLUMNS + ", loose_seeds FROM products WHERE id=? AND business_id=?", (int(value("item_id")), biz))
        if item is None or not is_seed_category(item["category"]):
            problems.append(f"{where}: choose the seed item from inventory.")
            continue
        grant = item_grant(db, item["id"], "transfer")
        if grant is None:
            problems.append(f"{where}: your account cannot take {item['name']} out of inventory. Ask an admin, or request it on the item's page.")
            continue
        if not item["seeds_per_pack"]:
            problems.append(f"{where}: {item['name']} has no pack size recorded. Record its seeds per pack on the full planting form first.")
            continue
        try:
            trays = tray_count(value("trays"))
        except ValueError:
            problems.append(f"{where}: enter the trays planted of {item['name']} (a number above zero).")
            continue
        try:
            holes = int(value("holes")) if value("holes") else usual
            if holes is None or not 1 <= holes <= MAX_TRAY_HOLES:
                raise ValueError
        except ValueError:
            problems.append(f"{where}: enter the holes in each tray (a whole number from 1 to {MAX_TRAY_HOLES:,}).")
            continue
        # Joining a batch already planted that day (or earlier in this form) keeps that batch's mortality and ready date
        joins = (item["id"], holes) in earlier or day_batch(db, biz, item["id"], day, holes) is not None
        mortality, ready_on = None, None
        if not joins:
            try:
                mortality = float(value("mortality_pct").replace(",", ""))
                if not 0 <= mortality < 100:
                    raise ValueError
            except ValueError:
                problems.append(f"{where}: enter the expected mortality for {item['name']} as a percentage from 0 up to (not including) 100.")
                continue
            ready_on = day_or_none(value("ready_on")) if value("ready_on") else None
            if ready_on is None or ready_on < day:
                problems.append(f"{where}: enter the date the {item['name']} seedlings are expected to be ready (not before the day planted).")
                continue
            if seedlings_from(int(math.floor(trays * holes + 0.5 + 1e-9)), mortality) < 1:
                problems.append(f"{where}: at that mortality these seeds would give less than one seedling.")
                continue
        earlier.add((item["id"], holes))
        entries.append({"item_id": item["id"], "grant": grant, "trays": trays, "holes": holes, "mortality": mortality, "ready_on": ready_on})
    return entries, problems


@app.post("/planting/planter/<int:employee_id>")
@need(None)
def planter_save(employee_id):
    """Saves every entry on a planter's day form at once, or none of them if anything is wrong."""
    db, biz = get_db(), g.user["business_id"]
    e = get_employee(db, employee_id)
    day = day_or_none(text("date")) if text("date") else None
    back = redirect(url_for("planter_day", employee_id=employee_id, date=day or today_str()))
    if day is None or day > today_str():
        flash("Choose the day the trays were planted (today or earlier).", "error")
        return back
    if not is_planter(e):
        flash(f"{e['full_name']} is not set to plant trays. An admin can tick \"Plants trays\" for them in HR.", "error")
        return back
    entries, problems = read_planter_entries(db, biz, day)
    if problems:
        flash(" ".join(problems) + " Nothing was saved.", "error")
        return back
    if not entries:
        flash("Add at least one entry: the seed item and the trays planted.", "error")
        return back

    done, joined, grants = [], set(), []
    try:
        for entry in entries:
            item = db.one("SELECT " + ITEM_COLUMNS + ", loose_seeds FROM products WHERE id=? AND business_id=?", (entry["item_id"], biz))
            size, trays, holes = item["seeds_per_pack"], entry["trays"], entry["holes"]
            seeds = int(math.floor(trays * holes + 0.5 + 1e-9))   # trays x holes in each tray; a part-planted tray rounds to whole seeds
            seed_cost = item["unit_cost"] / size
            packs, loose_delta, _ = take_seeds(db, biz, item, size, seeds)
            batch = day_batch(db, biz, item["id"], day, holes)
            if batch:
                total = batch["seeds_planted"] + seeds
                unit_cost = (batch["seed_unit_cost"] * batch["seeds_planted"] + seed_cost * seeds) / total
                db.execute(
                    """UPDATE plantings SET seeds_planted=?, packs_used = packs_used + ?, loose_delta = loose_delta + ?, seedlings=?, seed_unit_cost=?,
                              cost_per_seedling=?, trays = COALESCE(trays, 0) + ?, holes_per_tray=? WHERE id=? AND business_id=?""",
                    (total, packs, loose_delta, seedlings_from(total, batch["mortality_pct"]), unit_cost, unit_cost / (1 - batch["mortality_pct"] / 100),
                     trays, holes, batch["id"], biz),
                )
                planting_id = batch["id"]
                joined.add(planting_id)
            else:
                mortality = entry["mortality"]
                cost_per_seedling = seed_cost / (1 - mortality / 100)
                last = db.one("SELECT price FROM plantings WHERE business_id=? AND seed_item_id=? ORDER BY id DESC LIMIT 1", (biz, item["id"]))
                price = last["price"] if last and last["price"] else round_up_to_cent(cost_per_seedling)
                planting_id = insert_planting(db, biz, {
                    "seed_item_id": item["id"], "seed_code": item["item_code"], "seed_name": item["name"], "name": seedling_name_for(item["name"]),
                    "department": item["department"], "batch_no": "", "source": "inventory", "seeds": seeds, "packs": packs, "loose_delta": loose_delta,
                    "seeds_per_pack": size, "mortality": mortality, "seedlings": seedlings_from(seeds, mortality), "seed_cost": seed_cost,
                    "cost_per_seedling": cost_per_seedling, "price": round(price, 2), "planted_on": day, "ready_on": entry["ready_on"],
                    "trays": trays, "holes_per_tray": holes,
                })
            save_tray_rows(db, biz, planting_id, [(e, trays)])
            grants.append(entry["grant"])
            done.append(f"{tray_figure(trays)} tray{'s' if trays != 1 else ''} of {item['name']} ({seeds:,} seeds" + (f", {packs} pack{'s' if packs != 1 else ''}" if packs else "")
                        + (", added to that day's batch" if batch else "") + ")")
        for grant in grants:
            use_grant(db, grant)
        db.commit()
    except NotEnoughSeeds as short:
        db.rollback()
        flash(f"{short} Nothing was saved.", "error")
        return back
    except StockError:
        db.rollback()
        flash("The stock changed while you were working. Nothing was saved; please try again.", "error")
        return back
    except DB_ERRORS:
        db.rollback()
        flash("The entries could not be saved. Please try again.", "error")
        return back

    release_plantings(db, biz)
    for planting_id in joined:
        sync_planting(db, biz, planting_id)   # a batch already on the POS lists the extra seedlings
    db.commit()
    flash(f"Saved for {e['full_name']} on {day}: " + "; ".join(done) + ".", "ok")
    return back


@app.get("/planting/new")
@need(None)
def planting_new():
    db, biz = get_db(), g.user["business_id"]
    seeds, info = [], {}
    for i in db.all(
        "SELECT id, item_code, name, category, department, quantity, unit_cost, seeds_per_pack, loose_seeds FROM products WHERE business_id=? ORDER BY name", (biz,)
    ):
        if not is_seed_category(i["category"]):
            continue
        i["allowed"] = item_grant(db, i["id"], "transfer") is not None
        seeds.append(i)
        info[str(i["id"])] = {
            "name": i["name"], "packs": i["quantity"], "loose": i["loose_seeds"] or 0, "size": i["seeds_per_pack"] or 0, "cost": i["unit_cost"],
            "seedling": seedling_name_for(i["name"]), "department": i["department"],
        }
    return render_template(
        "planting_new.html", seed_items=seeds, info=info, chosen=request.args.get("item", ""), departments=department_names(db, biz), today=today_str(),
        release_days=RELEASE_DAYS, staff=planting_staff(db, biz), usual_holes=hr_settings_for(db, biz)["holes_per_tray"] or "", max_holes=MAX_TRAY_HOLES,
    )


@app.post("/planting")
@need(None)
def planting_create():
    db, biz = get_db(), g.user["business_id"]
    raw_item = text("item_id")
    back = redirect(url_for("planting_new", item=raw_item))
    today = today_str()

    item, grant = None, None
    if raw_item:
        try:
            item_id = int(raw_item)
        except ValueError:
            abort(400)
        item = db.one("SELECT " + ITEM_COLUMNS + ", loose_seeds FROM products WHERE id=? AND business_id=?", (item_id, biz))
        if item is None or not is_seed_category(item["category"]):
            flash("Choose one of your seed items, or leave the item empty to record a planting that is not from inventory.", "error")
            return back
        grant = item_grant(db, item_id, "transfer")
        if grant is None:
            return refuse_item_action(item_id)
    elif not can_plant():
        flash("Your account cannot record a planting that is not taken from inventory. Ask an admin.", "error")
        return back

    trays_form, problem = read_trays_form(db, biz)
    if problem:
        flash(problem, "error")
        return back
    trays, holes = trays_form["trays"], trays_form["holes"]
    try:
        if text("seeds_planted"):
            seeds = number("seeds_planted", int)
        elif trays and holes:
            seeds = int(math.floor(trays * holes + 0.5 + 1e-9))   # trays x holes in each tray; a part-planted tray rounds to whole seeds
        else:
            raise ValueError
        if seeds < 1:
            raise ValueError
    except ValueError:
        flash("Enter how many seeds are being planted (a whole number above zero), or the trays planted and the holes in each tray: trays x holes gives the seeds.", "error")
        return back
    try:
        mortality = number("mortality_pct")
        if not 0 <= mortality < 100:
            raise ValueError
    except ValueError:
        flash("Enter the expected mortality rate as a percentage from 0 up to (not including) 100.", "error")
        return back
    planted_on = day_or_none(text("planted_on")) if text("planted_on") else today
    ready_on = day_or_none(text("ready_on"))
    if planted_on is None or ready_on is None:
        flash("Enter the planting date and the date the seedlings are expected to be ready.", "error")
        return back
    if ready_on < planted_on:
        flash("The expected ready date cannot be before the planting date.", "error")
        return back

    size = item["seeds_per_pack"] if item else None
    if item and not size:
        try:
            size = number("seeds_per_pack", int)
            if size < 1:
                raise ValueError
        except ValueError:
            flash("Say how many seeds are in one pack (a whole number above zero).", "error")
            return back
    try:
        if text("seed_cost"):
            seed_cost = number("seed_cost")
        else:
            seed_cost = item["unit_cost"] / size if item else 0.0
        if seed_cost < 0:
            raise ValueError
    except ValueError:
        flash("The cost of one seed must be a number, 0 or more.", "error")
        return back
    seedlings = seedlings_from(seeds, mortality)
    if seedlings < 1:
        flash("At that mortality rate these seeds would give less than one seedling.", "error")
        return back
    cost_per_seedling = seed_cost / (1 - mortality / 100)
    try:
        price = number("price") if text("price") else round_up_to_cent(cost_per_seedling)
        if price < 0:
            raise ValueError
    except ValueError:
        flash("The selling price per seedling must be a number, 0 or more.", "error")
        return back

    name = clean_name(text("name"), 100) or (seedling_name_for(item["name"]) if item else "")
    if not name:
        flash("Give the planting a name, for example Tomato seedlings.", "error")
        return back
    if item:
        seed_code, seed_name, dept = item["item_code"], item["name"], item["department"]
    else:
        dept = text("department") if text("department") in department_names(db, biz) else "General"
        seed_name = name
        seed_code = seed_code_for(name)

    packs, loose_delta, loose_left = 0, 0, 0
    try:
        if item:
            packs, loose_delta, loose_left = take_seeds(db, biz, item, size, seeds)

        planting_id = insert_planting(db, biz, {
            "seed_item_id": item["id"] if item else None, "seed_code": seed_code, "seed_name": seed_name, "name": name, "department": dept,
            "batch_no": text("batch_no")[:40], "source": "inventory" if item else "manual", "seeds": seeds, "packs": packs, "loose_delta": loose_delta,
            "seeds_per_pack": size, "mortality": mortality, "seedlings": seedlings, "seed_cost": seed_cost, "cost_per_seedling": cost_per_seedling,
            "price": round(price, 2), "planted_on": planted_on, "ready_on": ready_on, "notes": text("notes")[:300],
            "still_in_greenhouse": checked("still_in_greenhouse"), "trays": trays, "holes_per_tray": holes if trays else None,
        })
        save_tray_rows(db, biz, planting_id, trays_form["rows"])
        use_grant(db, grant)
        db.commit()
    except NotEnoughSeeds as short:
        db.rollback()
        flash(f"{short} Nothing was planted.", "error")
        return back
    except StockError:
        db.rollback()
        flash("The stock changed while you were working. Nothing was planted; please try again.", "error")
        return back
    except DB_ERRORS:
        db.rollback()
        flash("The planting could not be saved. Please try again.", "error")
        return back

    release_plantings(db, biz)
    made = db.one("SELECT released, still_in_greenhouse, ready_on FROM plantings WHERE id=?", (planting_id,))
    if not made["still_in_greenhouse"]:
        where = "It is kept as a historical record only: not listed on the POS and not available to pre-order."
    elif made["released"]:
        where = "It is on the POS now."
    else:
        where = f"It will appear on the POS on {add_days(ready_on, -RELEASE_DAYS)}, a week before it is ready."
    used = f", using {packs} pack(s) from inventory" if item else ""
    spare = f" {loose_left:,} loose seeds are kept for the next planting." if item and loose_left else ""
    in_trays = f" ({tray_figure(trays)} trays)" if trays else ""
    flash(f"Planting recorded: {seeds:,} seeds{in_trays} of {seed_name}{used}. About {seedlings:,} seedlings are expected by {ready_on}.{spare} {where}", "ok")
    return redirect(url_for("planting_detail", planting_id=planting_id))


def get_planting(db, planting_id):
    p = db.one("SELECT * FROM plantings WHERE id=? AND business_id=?", (planting_id, g.user["business_id"]))
    if p is None:
        abort(404)
    return p


@app.get("/planting/<int:planting_id>")
@need(None)
def planting_detail(planting_id):
    db, biz = get_db(), g.user["business_id"]
    release_plantings(db, biz)
    p = get_planting(db, planting_id)
    f = with_figures(db, biz, [p])[0]
    orders = db.all("SELECT * FROM preorders WHERE planting_id=? AND business_id=? ORDER BY id DESC", (planting_id, biz))
    return render_template(
        "planting_detail.html", p=f, orders=orders, editable=can_plant(), release_days=RELEASE_DAYS, today=today_str(), planted_by=planting_tray_rows(db, biz, planting_id),
        staff=planting_staff(db, biz), max_holes=MAX_TRAY_HOLES,
    )


def planting_tray_rows(db, biz, planting_id):
    """Who planted how many trays of a planting, and whether each is paid: on a payslip, still to be paid (a casual worker),
    or not paid by the tray (permanent staff are on a salary; a person since removed from HR is only a name)."""
    rows = db.all(
        """SELECT t.id, t.employee_id, t.employee_name, t.trays, t.paid_payslip_id, e.employment_type, e.tray_rate, pr.tray_rate AS paid_rate
           FROM planting_trays t LEFT JOIN employees e ON e.id = t.employee_id AND e.business_id = t.business_id
           LEFT JOIN payroll pr ON pr.id = t.paid_payslip_id AND pr.business_id = t.business_id
           WHERE t.business_id=? AND t.planting_id=? ORDER BY t.id""",
        (biz, planting_id),
    )
    hr_set = hr_settings_for(db, biz)
    for r in rows:
        r["state"] = "paid" if r["paid_payslip_id"] else ("due" if r["employment_type"] == "Casual Worker" else "salaried" if r["employment_type"] else "gone")
        # What the trays earn: at the rate they were paid at, or at the worker's rate now (unpaid trays are paid at the rate in force)
        r["rate"] = r["paid_rate"] if r["state"] == "paid" else (rate_for(r, hr_set) if r["state"] == "due" else None)
        r["earns"] = round(r["trays"] * r["rate"], 2) if r["rate"] else None
    return rows


@app.post("/planting/<int:planting_id>/trays")
@need(None)
def planting_trays_update(planting_id):
    """Changes who planted how many trays. Trays already paid on a payslip are locked; the rest are replaced by what the form sends."""
    db, biz = get_db(), g.user["business_id"]
    get_planting(db, planting_id)
    back = redirect(url_for("planting_detail", planting_id=planting_id))
    if not can_plant():
        flash("Your account cannot edit planting records. Ask an admin.", "error")
        return back
    paid = db.one(
        "SELECT COALESCE(SUM(trays), 0) AS n FROM planting_trays WHERE business_id=? AND planting_id=? AND paid_payslip_id IS NOT NULL", (biz, planting_id)
    )["n"]
    info, problem = read_trays_form(db, biz, paid=round(paid, 2))
    if problem:
        flash(problem, "error")
        return back
    db.execute("DELETE FROM planting_trays WHERE business_id=? AND planting_id=? AND paid_payslip_id IS NULL", (biz, planting_id))
    save_tray_rows(db, biz, planting_id, info["rows"])
    db.execute(
        "UPDATE plantings SET trays=?, holes_per_tray=? WHERE id=? AND business_id=?",
        (info["trays"], info["holes"] if info["trays"] else None, planting_id, biz),
    )
    db.commit()
    flash("Trays saved." if info["trays"] else "Trays cleared.", "ok")
    return back


@app.post("/planting/<int:planting_id>/update")
@need(None)
def planting_update(planting_id):
    db, biz = get_db(), g.user["business_id"]
    p = get_planting(db, planting_id)
    back = redirect(url_for("planting_detail", planting_id=planting_id))
    if not can_plant():
        flash("Your account cannot edit planting records. Ask an admin.", "error")
        return back
    f = with_figures(db, biz, [p])[0]
    name = clean_name(text("name"), 100)
    ready_on = day_or_none(text("ready_on"))
    try:
        mortality = number("mortality_pct")
        if not 0 <= mortality < 100:
            raise ValueError
        price = number("price")
        if price < 0:
            raise ValueError
    except ValueError:
        flash("Mortality must be from 0 up to (not including) 100, and the price must be 0 or more.", "error")
        return back
    if not name or ready_on is None or ready_on < (p["planted_on"] or ""):
        flash("Give the batch a name and an expected ready date that is not before it was planted.", "error")
        return back
    still = checked("still_in_greenhouse")
    if not still and (f["sold"] + f["pending"]) > 0:
        flash(f"{f['sold'] + f['pending']:,} seedlings from this batch are already sold or pre-ordered, so it cannot be marked as a historical record. Nothing was changed.", "error")
        return back
    # While the batch is only expected, the mortality rate decides how many seedlings to expect. Once it is marked ready the real count stands.
    seedlings = p["seedlings"] if p["actual_ready_on"] else seedlings_from(p["seeds_planted"], mortality)
    if seedlings < f["sold"] + f["pending"]:
        flash(f"That would leave {seedlings:,} seedlings, but {f['sold'] + f['pending']:,} are already sold or pre-ordered. Nothing was changed.", "error")
        return back
    cost_per_seedling = p["seed_unit_cost"] / (1 - mortality / 100)
    db.execute(
        """UPDATE plantings SET name=?, batch_no=?, ready_on=?, mortality_pct=?, seedlings=?, cost_per_seedling=?, price=?, still_in_greenhouse=?, notes=?
           WHERE id=? AND business_id=?""",
        (name, text("batch_no")[:40], ready_on, mortality, seedlings, cost_per_seedling, round(price, 2), 1 if still else 0, text("notes")[:300], planting_id, biz),
    )
    if not still and p["released"]:
        # It was listed for sale; now it is only a record, so it comes off the POS (the listing itself is kept, at zero, for its sales history)
        db.execute("UPDATE pos_products SET quantity=0 WHERE business_id=? AND item_code=?", (biz, p["pos_code"]))
    release_plantings(db, biz)
    sync_planting(db, biz, planting_id)
    db.commit()
    flash("Planting record saved." if still else "Planting record saved, marked as a historical record. It is off the POS and cannot be pre-ordered.", "ok")
    return back


@app.post("/planting/<int:planting_id>/ready")
@need(None)
def planting_ready(planting_id):
    db, biz = get_db(), g.user["business_id"]
    p = get_planting(db, planting_id)
    back = redirect(url_for("planting_detail", planting_id=planting_id))
    if not can_plant():
        flash("Your account cannot edit planting records. Ask an admin.", "error")
        return back
    if not p["still_in_greenhouse"]:
        flash("This is a historical record, not currently in the greenhouse, so it cannot be marked ready for sale.", "error")
        return back
    f = with_figures(db, biz, [p])[0]
    ready_day = day_or_none(text("actual_ready_on")) if text("actual_ready_on") else today_str()
    if ready_day is None:
        flash("Enter the date the seedlings were ready.", "error")
        return back
    count = p["seedlings"]
    if text("ready_count"):
        try:
            count = number("ready_count", int)
            if count < 0:
                raise ValueError
        except ValueError:
            flash("Enter the number of seedlings that are really ready as a whole number.", "error")
            return back
    if count < f["sold"] + f["pending"]:
        flash(f"{f['sold'] + f['pending']:,} seedlings are already sold or pre-ordered, so the count cannot be below that. Nothing was changed.", "error")
        return back
    db.execute("UPDATE plantings SET actual_ready_on=?, seedlings=? WHERE id=? AND business_id=?", (ready_day, count, planting_id, biz))
    release_plantings(db, biz)
    sync_planting(db, biz, planting_id)
    db.commit()
    flash(f"Marked ready on {ready_day} with {count:,} seedlings. It is on the POS.", "ok")
    return back


@app.post("/planting/<int:planting_id>/toggle-record")
@need(None)
def planting_toggle_record(planting_id):
    """Quick action from the Planting Records list: mark a batch as a historical record, or growing again - without
    opening its own edit form. Marking it a record takes it off the POS at once, the same as the edit form does.
    """
    db, biz = get_db(), g.user["business_id"]
    p = get_planting(db, planting_id)
    back = redirect(url_for("planting_detail", planting_id=planting_id) if text("back") == "detail" else url_for("planting_list"))
    if not can_plant():
        flash("Your account cannot edit planting records. Ask an admin.", "error")
        return back

    still = text("to") != "record"
    if still == bool(p["still_in_greenhouse"]):
        return back
    if not still:
        f = with_figures(db, biz, [p])[0]
        if (f["sold"] + f["pending"]) > 0:
            flash(f"{f['sold'] + f['pending']:,} seedlings from this batch are already sold or pre-ordered, so it cannot be marked as a historical record.", "error")
            return back

    db.execute("UPDATE plantings SET still_in_greenhouse=? WHERE id=? AND business_id=?", (1 if still else 0, planting_id, biz))
    if not still and p["released"]:
        db.execute("UPDATE pos_products SET quantity=0 WHERE business_id=? AND item_code=?", (biz, p["pos_code"]))
    release_plantings(db, biz)
    sync_planting(db, biz, planting_id)
    db.commit()
    flash(f"'{p['name']}' marked as still growing." if still else f"'{p['name']}' marked as a historical record. It is off the POS and cannot be pre-ordered.", "ok")
    return back


@app.post("/planting/<int:planting_id>/delete")
@need("admin")
def planting_delete(planting_id):
    db, biz = get_db(), g.user["business_id"]
    p = get_planting(db, planting_id)
    f = with_figures(db, biz, [p])[0]
    back = redirect(url_for("planting_detail", planting_id=planting_id))
    if f["sold"] or f["pending"] or f["collected"]:
        flash("Seedlings from this batch have been sold or pre-ordered, so the record is kept. Cancel the pre-orders first if it was a mistake.", "error")
        return back
    unpaid = sum(r["trays"] for r in planting_tray_rows(db, biz, planting_id) if r["state"] == "due")
    try:
        if p["seed_item_id"] and p["packs_used"] is not None:
            db.execute(
                "UPDATE products SET quantity = quantity + ?, loose_seeds = COALESCE(loose_seeds, 0) - ? WHERE id=? AND business_id=?",
                (p["packs_used"], p["loose_delta"] or 0, p["seed_item_id"], biz),
            )
        db.execute("DELETE FROM pos_products WHERE business_id=? AND item_code=?", (biz, p["pos_code"]))
        db.execute("DELETE FROM preorders WHERE planting_id=? AND business_id=?", (planting_id, biz))
        db.execute("DELETE FROM planting_trays WHERE planting_id=? AND business_id=?", (planting_id, biz))
        db.execute("DELETE FROM plantings WHERE id=? AND business_id=?", (planting_id, biz))
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The record could not be deleted. Please try again.", "error")
        return back
    flash("The planting record was deleted" + (f" and {p['packs_used']} pack(s) went back into inventory." if p["packs_used"] else ".")
          + (f" {tray_figure(unpaid)} trays planted by casual workers were not paid yet, and will not be paid now." if unpaid else ""), "ok")
    return redirect(url_for("planting_list"))


def up_next_url():
    """Back to Up Next after a pre-order action, keeping the search that was on screen."""
    q = text("q")
    return (url_for("up_next", q=q) if q else url_for("up_next")) + "#preorders"


def planting_matches(p, words):
    """True when every searched word appears in the batch's name, label, seed, batch number or POS code."""
    hay = norm(" ".join(str(p.get(key) or "") for key in ("label", "name", "seed_name", "seed_code", "batch_no", "pos_code")))
    return all(word in hay for word in words)


@app.get("/planting/up-next")
@need(None)
def up_next():
    db, biz = get_db(), g.user["business_id"]
    q = " ".join(request.args.get("q", "").split())[:100]
    words = norm(q).split()
    release_plantings(db, biz)
    rows = with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? ORDER BY ready_on, id", (biz,)))
    batches = [f for f in rows if (f["available"] > 0 or f["pending"] > 0) and planting_matches(f, words)]

    # Pre-orders match on the product (batch) as above, or on the customer who placed them
    where, params = "po.business_id=?", [biz]
    for word in words:
        like = f"%{word}%"
        where += """ AND (LOWER(p.name) LIKE ? OR LOWER(p.seed_name) LIKE ? OR LOWER(p.seed_code) LIKE ? OR LOWER(COALESCE(p.batch_no, '')) LIKE ?
                          OR LOWER(COALESCE(p.pos_code, '')) LIKE ? OR LOWER(po.customer_name) LIKE ? OR LOWER(COALESCE(po.customer_phone, '')) LIKE ?)"""
        params += [like] * 7
    orders = db.all(
        f"""SELECT po.*, p.name AS planting_name, p.batch_no AS planting_batch, p.planted_on AS planting_planted
            FROM preorders po JOIN plantings p ON p.id = po.planting_id WHERE {where}
            ORDER BY CASE WHEN po.status='pending' THEN 0 ELSE 1 END, po.id DESC LIMIT 200""",
        params,
    )
    by_planting = {r["id"]: r for r in rows}
    for o in orders:
        o["label"] = planting_label({"name": o["planting_name"], "batch_no": o["planting_batch"], "planted_on": o["planting_planted"]})
        o["total"] = round(o["qty"] * o["unit_price"], 2)
        p = by_planting.get(o["planting_id"])
        o["ready_on"] = (p["actual_ready_on"] or p["ready_on"]) if p else None
        o["is_ready"] = bool(p and p["stage"] in ("ready", "due"))
        o["days"] = p["days"] if p else None
        o["collect_when"] = when_words(o["collect_on"], today_str(), before="due ") if o["collect_on"] and o["status"] == "pending" else ""
    return render_template(
        "up_next.html", batches=batches, orders=orders, groups=preorder_groups(db, biz, [o for o in orders if o["status"] == "pending"]),
        history=[o for o in orders if o["status"] != "pending"], statuses=PAYMENT_STATUSES, release_days=RELEASE_DAYS, q=q, methods=PAYMENT_METHODS,
        remind_days=preorder_remind_days(), reminders_on=alert_enabled("preorders"), today=today_str(), tomorrow=add_days(today_str(), 1),
    )


# --- Pre-orders grouped by customer ---
# A customer's pre-orders (same name and phone number, spelled any way) are listed together. "Generate invoice" opens the POS with
# the ticked ones already in the cart, at the quantity and price agreed when they were placed; completing that sale collects them.
def customer_group_key(name, phone):
    return name_key(name or ""), phone_key(phone)


def preorder_groups(db, biz, pending):
    """The pending pre-orders shown, gathered by customer: newest-placed customer first, each customer's items soonest-ready first."""
    groups = {}
    for o in sorted(pending, key=lambda o: o["id"]):
        key = customer_group_key(o["customer_name"], o["customer_phone"])
        group = groups.setdefault(key, {"name": o["customer_name"], "phone": o["customer_phone"], "orders": [], "anchor": o["id"]})
        group["orders"].append(o)
    out = []
    for group in groups.values():
        group["orders"].sort(key=lambda o: (o["ready_on"] or "9999", o["id"]))
        group["total"] = round(sum(o["total"] for o in group["orders"]), 2)
        group["ready"] = [o for o in group["orders"] if o["is_ready"]]
        group["ready_total"] = round(sum(o["total"] for o in group["ready"]), 2)
        group["latest"] = max(o["id"] for o in group["orders"])
        out.append(group)
    return sorted(out, key=lambda grp: -grp["latest"])


# --- Reminders: pre-orders about to be collected ---
# A pre-order with a collection day is reminded the day before it. One without is reminded when its batch is ready within the
# days set on Up Next (or already is). Either way once, to everyone.
PREORDER_REMIND_DEFAULT = 3


def preorder_remind_days():
    days = g.user["preorder_remind_days"]
    return PREORDER_REMIND_DEFAULT if days is None else days


def when_words(day, today, before=""):
    """'tomorrow, Tue 27 Oct', 'today', 'in 3 days, on Fri 30 Oct' or 'since Mon 26 Oct' for a day relative to today."""
    left = (datetime.date.fromisoformat(day) - datetime.date.fromisoformat(today)).days
    label = datetime.date.fromisoformat(day).strftime("%a %d %b")
    if left == 0:
        return "today"
    if left == 1:
        return f"tomorrow, {label}"
    return f"in {left} days, on {label}" if left > 0 else f"{before}since {label}"


def tell_everyone(db, biz, kind, title, body, link):
    """An alert for the admins (with the WhatsApp copy, if a number is set) and for every other user of the business."""
    notify(db, kind, title, body, link, whatsapp=True)
    for u in db.all("SELECT id FROM users WHERE business_id=? AND role <> 'admin'", (biz,)):
        notify(db, kind, title, body, link, for_user_id=u["id"])


def remind_preorders_due(db, biz):
    """Raises the pre-order reminders that are due. Runs from the alerts poll, so it needs nothing scheduled; a pre-order is claimed
    before its alert is raised, so two pages polling at once cannot both raise it."""
    if not alert_enabled("preorders"):
        return
    today = today_str()
    due = db.all(
        """SELECT po.id, po.customer_name, po.customer_phone, po.qty, po.collect_on, p.id AS planting_id, p.name, p.batch_no, p.planted_on,
                  COALESCE(p.actual_ready_on, p.ready_on) AS ready
           FROM preorders po JOIN plantings p ON p.id = po.planting_id
           WHERE po.business_id=? AND po.status='pending' AND po.reminded_at IS NULL
             AND ((po.collect_on IS NOT NULL AND po.collect_on <= ?) OR (po.collect_on IS NULL AND COALESCE(p.actual_ready_on, p.ready_on) <= ?))
           ORDER BY po.id""",
        (biz, add_days(today, 1), add_days(today, preorder_remind_days())),
    )
    if not due:
        return
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    claimed = [d for d in due if db.execute("UPDATE preorders SET reminded_at=? WHERE id=? AND reminded_at IS NULL", (stamp, d["id"])).rowcount == 1]
    link = url_for("up_next") + "#preorders"

    # With a collection day: one alert per customer and day
    visits = {}
    for d in claimed:
        if d["collect_on"]:
            visits.setdefault((customer_group_key(d["customer_name"], d["customer_phone"]), d["collect_on"]), []).append(d)
    for (_, day), orders in visits.items():
        who = orders[0]["customer_name"]
        items = ", ".join(f"{d['qty']:,} x {planting_label(d)}" for d in orders)
        when = when_words(day, today, before="due ")
        tell_everyone(db, biz, "preorder", f"Pre-order collection {'today' if when == 'today' else when.split(',')[0]}: {who}",
                      f"{who} collects {when}: {items}. Generate the invoice from Up Next when they come.", link)

    # Without one: one alert per batch, when it is nearly ready
    batches = {}
    for d in claimed:
        if not d["collect_on"]:
            batches.setdefault(d["planting_id"], []).append(d)
    for orders in batches.values():
        first = orders[0]
        label = planting_label(first)
        left = (datetime.date.fromisoformat(first["ready"]) - datetime.date.fromisoformat(today)).days
        ready_date = datetime.date.fromisoformat(first["ready"]).strftime("%d %b")
        when = "ready today" if left == 0 else f"ready in {left} day{'s' if left != 1 else ''}, on {ready_date}" if left > 0 else f"ready since {ready_date}"
        people = {}
        for d in orders:
            people[d["customer_name"]] = people.get(d["customer_name"], 0) + d["qty"]
        body = (f"{label} is {when}. {len(orders)} pre-order{'s' if len(orders) != 1 else ''} waiting to be collected: "
                + ", ".join(f"{name} ({qty:,})" for name, qty in people.items()) + ".")
        tell_everyone(db, biz, "preorder", f"Pre-orders due: {label}", body, link)
    db.commit()


@app.post("/planting/up-next/reminder")
@need("admin")
def preorder_reminder_settings():
    db = get_db()
    days = request.form.get("remind_days", type=int)
    if days is None or not 0 <= days <= 60:
        flash("Choose how many days before a batch is ready to be reminded (0 to 60).", "error")
    else:
        db.execute("UPDATE businesses SET preorder_remind_days=? WHERE id=?", (days, g.user["business_id"]))
        db.commit()
        flash("Everyone will be reminded " + ("on the day a pre-ordered batch is ready." if days == 0 else f"{days} day{'s' if days != 1 else ''} before a pre-ordered batch is ready.")
              + " Pre-orders with a collection day are reminded the day before it.", "ok")
    return redirect(url_for("up_next") + "#preorders")


def read_collect_day(p):
    """The collection day typed on a pre-order form for batch p: (day or None, problem). Blank is allowed. It cannot be in the past,
    or before the batch is expected to be ready."""
    raw = text("collect_on")
    if not raw:
        return None, None
    day = day_or_none(raw)
    if day is None:
        return None, "Enter the collection day as a date, or leave it blank."
    ready = p["actual_ready_on"] or p["ready_on"] or ""
    if day < today_str():
        return None, "The collection day cannot be in the past."
    if ready and day < ready:
        return None, f"This batch is expected to be ready on {ready}. Choose that day or a later one for the collection."
    return day, None


@app.post("/planting/<int:planting_id>/preorder")
@need("sell")
def preorder_create(planting_id):
    db, biz = get_db(), g.user["business_id"]
    p = get_planting(db, planting_id)
    back = redirect(url_for("planting_detail", planting_id=planting_id) if text("back") == "detail" else up_next_url())
    if not p["still_in_greenhouse"]:
        flash("This is a historical record, not currently in the greenhouse, so it cannot be pre-ordered.", "error")
        return back
    customer = clean_name(text("customer_name"), 100)
    if not customer:
        flash("Enter the customer's name for the pre-order.", "error")
        return back
    try:
        qty = number("qty", int)
        if qty < 1:
            raise ValueError
    except ValueError:
        flash("Enter how many seedlings the customer wants (a whole number above zero).", "error")
        return back
    collect_on, problem = read_collect_day(p)
    if problem:
        flash(problem + " Nothing was recorded.", "error")
        return back
    f = with_figures(db, biz, [p])[0]
    if qty > f["available"]:
        flash(f"Only {f['available']:,} seedlings of this batch are still free (not sold or pre-ordered). Nothing was recorded.", "error")
        return back
    db.insert(
        """INSERT INTO preorders (business_id, planting_id, customer_name, customer_phone, qty, unit_price, status, note, created_by, created_at, collect_on)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
        (biz, planting_id, customer, text("customer_phone")[:40], qty, p["price"], text("note")[:200], g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S"),
         collect_on),
    )
    sync_planting(db, biz, planting_id)
    db.commit()
    flash(f"Pre-order recorded for {customer}: {qty:,} x {f['label']}"
          + (f", to be collected on {collect_on}" if collect_on else "")
          + ". The seedlings are held; use Generate invoice when the customer collects them.", "ok")
    return back


def get_preorder(db, po_id):
    po = db.one("SELECT * FROM preorders WHERE id=? AND business_id=?", (po_id, g.user["business_id"]))
    if po is None:
        abort(404)
    return po


@app.post("/preorders/<int:po_id>/collect-day")
@need("sell")
def preorder_collect_day(po_id):
    """Changes (or clears) the day a customer means to collect a pre-order. Its reminder is raised again for the new day."""
    db, biz = get_db(), g.user["business_id"]
    po = get_preorder(db, po_id)
    back = redirect(up_next_url())
    if po["status"] != "pending":
        flash("That pre-order has already been collected or cancelled.", "error")
        return back
    collect_on, problem = read_collect_day(get_planting(db, po["planting_id"]))
    if problem:
        flash(problem + " Nothing was changed.", "error")
        return back
    db.execute("UPDATE preorders SET collect_on=?, reminded_at=NULL WHERE id=? AND business_id=?", (collect_on, po_id, biz))
    db.commit()
    flash(f"{po['customer_name']} now collects on {collect_on}." if collect_on else f"The collection day for {po['customer_name']} was cleared.", "ok")
    return back


@app.post("/preorders/<int:po_id>/cancel")
@need("sell")
def preorder_cancel(po_id):
    db, biz = get_db(), g.user["business_id"]
    po = get_preorder(db, po_id)
    back = redirect(up_next_url())
    if po["status"] != "pending":
        flash("That pre-order has already been collected or cancelled.", "error")
        return back
    db.execute("UPDATE preorders SET status='cancelled', cancelled_at=? WHERE id=? AND business_id=?", (now().strftime("%Y-%m-%d %H:%M:%S"), po_id, biz))
    sync_planting(db, biz, po["planting_id"])
    db.commit()
    flash(f"Pre-order for {po['customer_name']} cancelled. The {po['qty']:,} seedlings are free again.", "ok")
    return back


# --- Customer requests: things customers asked for that we do not have ---
# Logged with the day to follow it up; that day the admins are alerted. When the item is bought or planted, the admins are told who
# asked for it. A request is fulfilled or dropped, never deleted, and everything that happened to it is kept in its log.
REQUEST_STATUSES = {"open": "Open", "fulfilled": "Fulfilled", "dropped": "Dropped"}
REQUEST_LOG_WORDS = {"logged": "Logged", "follow_up": "Follow-up moved", "note": "Note", "arrived": "Arrived", "fulfilled": "Fulfilled",
                     "dropped": "Dropped", "reopened": "Reopened", "reminded": "Reminder sent"}
# Words that say what form something comes in rather than what it is, left out when a request is matched to an item by name
REQUEST_FILLER = {"seedling", "seedlings", "seed", "seeds", "plant", "plants", "pack", "packs", "packet", "packets", "of", "the", "a", "an", "some",
                  "kg", "bag", "bags", "tray", "trays", "variety", "type"}


def request_words(name):
    words = [w for w in re.findall(r"[a-z0-9]+", norm(name)) if w not in REQUEST_FILLER]
    return words or re.findall(r"[a-z0-9]+", norm(name))


def request_fits(item_name, other_name):
    """True when every word that names what was asked for is in the other name: 'cabbage seedlings' fits 'Cabbage Gloria F1'."""
    wanted, have = request_words(item_name), set(re.findall(r"[a-z0-9]+", norm(other_name)))
    return bool(wanted) and all(w in have for w in wanted)


def known_item(db, biz, typed):
    """(name, code) for a request: the inventory or POS item it names (by code or exact name), else the name as typed and no code."""
    name = clean_name(typed, 100)
    for table in ("products", "pos_products"):
        row = db.one(f"SELECT item_code, name FROM {table} WHERE business_id=? AND (UPPER(item_code)=? OR LOWER(name)=?) ORDER BY id LIMIT 1",
                     (biz, name.upper(), norm(name)))
        if row:
            return row["name"], row["item_code"]
    return name, ""


def request_item_choices(db, biz):
    """What the item box suggests: stock, the POS and what has been planted, by name."""
    names = [r["name"] for r in db.all("SELECT name FROM products WHERE business_id=? UNION SELECT name FROM pos_products WHERE business_id=?", (biz, biz))]
    names += [r["name"] for r in db.all("SELECT DISTINCT name FROM plantings WHERE business_id=?", (biz,))]
    return sorted({n for n in names if n}, key=str.lower)[:600]


def requests_with_stock(db, biz, rows):
    """Adds to each request what we have of it now: in stock (inventory and POS, when it is a known item) and seedlings free in the greenhouse."""
    if not rows:
        return rows
    stock = {}
    for table in ("products", "pos_products"):
        for r in db.all(f"SELECT item_code, quantity FROM {table} WHERE business_id=?", (biz,)):
            stock[r["item_code"]] = stock.get(r["item_code"], 0) + (r["quantity"] or 0)
    growing = with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? AND still_in_greenhouse=1", (biz,)))
    for r in rows:
        r["in_stock"] = stock.get(r["item_code"]) if r["item_code"] else None
        fits = [f for f in growing if f["available"] > 0 and request_fits(r["item_name"], f["name"])]
        r["growing"] = sum(f["available"] for f in fits)
        r["growing_ready"] = min((f["actual_ready_on"] or f["ready_on"] or "") for f in fits) if fits else ""
        r["have_now"] = bool(r["in_stock"]) or r["growing"] > 0
    return rows


def request_log(db, biz, cr_id, kind, detail=""):
    db.insert("INSERT INTO customer_request_log (business_id, request_id, kind, detail, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (biz, cr_id, kind, detail[:300], g.user["username"] if g.get("user") else "", now().strftime("%Y-%m-%d %H:%M:%S")))


def request_label(r):
    return (f"{r['quantity']:,} x " if r["quantity"] else "") + r["item_name"]


def most_requested(rows):
    """The open requests by what was asked for: how many customers, how many in all. Most customers first."""
    groups = {}
    for r in rows:
        key = " ".join(request_words(r["item_name"]))
        grp = groups.setdefault(key, {"item": r["item_name"], "customers": set(), "quantity": 0, "requests": 0, "have_now": False})
        grp["customers"].add(customer_group_key(r["customer_name"], r["customer_phone"]))
        grp["quantity"] += r["quantity"] or 0
        grp["requests"] += 1
        grp["have_now"] = grp["have_now"] or r.get("have_now", False)
    out = [dict(g_, customers=len(g_["customers"])) for g_ in groups.values()]
    return sorted(out, key=lambda x: (-x["customers"], -x["quantity"], x["item"].lower()))


def read_follow_up_day(field="follow_up_on"):
    """The follow-up day from the form: (day, None), or (None, what is wrong). Today or later, within a year."""
    day = day_or_none(text(field)) if text(field) else None
    if day is None:
        return None, "Choose the day to follow it up."
    if day < today_str():
        return None, "The follow-up day cannot be in the past."
    if day > add_days(today_str(), 366):
        return None, "Choose a follow-up day within the next year."
    return day, None


def get_customer_request(db, cr_id):
    r = db.one("SELECT * FROM customer_requests WHERE id=? AND business_id=?", (cr_id, g.user["business_id"]))
    if r is None:
        abort(404)
    return r


@app.get("/customer-requests")
@need(None)
def customer_requests():
    db, biz, today = get_db(), g.user["business_id"], today_str()
    show = request.args.get("show", "due")
    show = show if show in ("due", "open", "closed", "all") else "due"
    q = " ".join(request.args.get("q", "").split())[:100]
    where, params = "business_id=?", [biz]
    for word in norm(q).split():
        where += " AND (LOWER(customer_name) LIKE ? OR LOWER(COALESCE(customer_phone, '')) LIKE ? OR LOWER(item_name) LIKE ? OR LOWER(COALESCE(note, '')) LIKE ?)"
        params += [f"%{word}%"] * 4
    rows = db.all(f"SELECT * FROM customer_requests WHERE {where} ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, follow_up_on, id DESC LIMIT 500", params)
    open_rows = requests_with_stock(db, biz, [r for r in rows if r["status"] == "open"])
    counts = {"due": sum(1 for r in open_rows if r["follow_up_on"] <= today), "open": len(open_rows), "closed": len(rows) - len(open_rows), "all": len(rows)}
    if show == "due":
        listed = [r for r in open_rows if r["follow_up_on"] <= today]
    elif show == "open":
        listed = open_rows
    elif show == "closed":
        listed = sorted((r for r in rows if r["status"] != "open"), key=lambda r: r["closed_at"] or "", reverse=True)
    else:
        listed = open_rows + [r for r in rows if r["status"] != "open"]
    for r in listed:
        r["label"] = request_label(r)
        r["when"] = when_words(r["follow_up_on"], today, before="due ") if r["status"] == "open" else ""
    return render_template(
        "customer_requests.html", rows=listed, show=show, counts=counts, q=q, today=today, top=most_requested(open_rows)[:8],
        items=request_item_choices(db, biz) if can("sell") else [], statuses=REQUEST_STATUSES, reminders_on=alert_enabled("requests"),
    )


@app.post("/customer-requests")
@need("sell")
def customer_request_create():
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("customer_requests", show="open"))
    customer = clean_name(text("customer_name"), 100)
    item_name, code = known_item(db, biz, text("item_name"))
    if not customer or not item_name:
        flash("Enter the customer's name and what they asked for. Nothing was logged.", "error")
        return back
    quantity = None
    if text("quantity"):
        try:
            quantity = number("quantity", int)
            if not 1 <= quantity <= 10_000_000:
                raise ValueError
        except ValueError:
            flash("The quantity must be a whole number above zero, or left empty. Nothing was logged.", "error")
            return back
    day, problem = read_follow_up_day()
    if problem:
        flash(problem + " Nothing was logged.", "error")
        return back
    source = "assistant" if text("source") == "assistant" else "page"
    cr_id = db.insert(
        """INSERT INTO customer_requests (business_id, customer_name, customer_phone, item_name, item_code, quantity, note, follow_up_on, status, source, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
        (biz, customer, text("customer_phone")[:40], item_name, code, quantity, text("note")[:300], day, source, g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    request_log(db, biz, cr_id, "logged", f"Follow up {day}" + (" (with the assistant)" if source == "assistant" else ""))
    db.commit()
    label = (f"{quantity:,} x " if quantity else "") + item_name
    flash(f"Request logged for {customer}: {label}. The admins are reminded to follow it up on {day}.", "ok")
    return redirect(url_for("customer_request", cr_id=cr_id))


@app.get("/customer-requests/<int:cr_id>")
@need(None)
def customer_request(cr_id):
    db, biz, today = get_db(), g.user["business_id"], today_str()
    r = requests_with_stock(db, biz, [get_customer_request(db, cr_id)])[0]
    r["label"], r["when"] = request_label(r), (when_words(r["follow_up_on"], today, before="due ") if r["status"] == "open" else "")
    log_rows = db.all("SELECT * FROM customer_request_log WHERE business_id=? AND request_id=? ORDER BY id", (biz, cr_id))
    others = db.all(
        "SELECT id, item_name, quantity, status, follow_up_on, created_at FROM customer_requests WHERE business_id=? AND id<>? AND LOWER(customer_name)=? ORDER BY id DESC LIMIT 10",
        (biz, cr_id, norm(r["customer_name"])),
    )
    return render_template("customer_request.html", r=r, log=log_rows, words=REQUEST_LOG_WORDS, statuses=REQUEST_STATUSES, today=today, others=others)


def open_request_or_back(db, cr_id):
    r = get_customer_request(db, cr_id)
    back = redirect(url_for("customer_request", cr_id=cr_id))
    if r["status"] != "open":
        flash("That request is already closed. Reopen it first.", "error")
        return r, back, True
    return r, back, False


@app.post("/customer-requests/<int:cr_id>/follow-up")
@need("sell")
def customer_request_follow_up(cr_id):
    db, biz = get_db(), g.user["business_id"]
    r, back, closed = open_request_or_back(db, cr_id)
    if closed:
        return back
    day, problem = read_follow_up_day()
    if problem:
        flash(problem + " Nothing was changed.", "error")
        return back
    db.execute("UPDATE customer_requests SET follow_up_on=?, reminded_at=NULL WHERE id=? AND business_id=?", (day, cr_id, biz))
    request_log(db, biz, cr_id, "follow_up", f"From {r['follow_up_on']} to {day}" + (f": {text('note')[:200]}" if text("note") else ""))
    db.commit()
    flash(f"{r['customer_name']}'s request is followed up again on {day}.", "ok")
    return back


@app.post("/customer-requests/<int:cr_id>/note")
@need("sell")
def customer_request_note(cr_id):
    db, biz = get_db(), g.user["business_id"]
    get_customer_request(db, cr_id)
    back = redirect(url_for("customer_request", cr_id=cr_id))
    if not text("note"):
        flash("Write the note first.", "error")
        return back
    request_log(db, biz, cr_id, "note", text("note")[:300])
    db.commit()
    flash("Note added.", "ok")
    return back


@app.post("/customer-requests/<int:cr_id>/close")
@need("sell")
def customer_request_close(cr_id):
    db, biz = get_db(), g.user["business_id"]
    r, back, closed = open_request_or_back(db, cr_id)
    if closed:
        return back
    outcome = text("outcome")
    if outcome not in ("fulfilled", "dropped"):
        flash("Say whether the customer got it or the request was dropped.", "error")
        return back
    note = text("note")[:300]
    db.execute("UPDATE customer_requests SET status=?, outcome=?, closed_by=?, closed_at=? WHERE id=? AND business_id=? AND status='open'",
               (outcome, note, g.user["username"], now().strftime("%Y-%m-%d %H:%M:%S"), cr_id, biz))
    request_log(db, biz, cr_id, outcome, note)
    db.commit()
    flash(f"{r['customer_name']}'s request is marked {REQUEST_STATUSES[outcome].lower()}.", "ok")
    return back


@app.post("/customer-requests/<int:cr_id>/reopen")
@need("sell")
def customer_request_reopen(cr_id):
    db, biz = get_db(), g.user["business_id"]
    r = get_customer_request(db, cr_id)
    back = redirect(url_for("customer_request", cr_id=cr_id))
    if r["status"] == "open":
        return back
    day, problem = read_follow_up_day()
    if problem:
        flash(problem + " Nothing was changed.", "error")
        return back
    db.execute("UPDATE customer_requests SET status='open', outcome='', closed_by=NULL, closed_at=NULL, follow_up_on=?, reminded_at=NULL WHERE id=? AND business_id=?",
               (day, cr_id, biz))
    request_log(db, biz, cr_id, "reopened", f"Follow up {day}")
    db.commit()
    flash(f"{r['customer_name']}'s request is open again, to follow up on {day}.", "ok")
    return back


def remind_requests_due(db, biz):
    """On a request's follow-up day the admins are alerted, once. Runs from the alerts poll; each request is claimed before its alert
    is raised, so two pages polling at once cannot both raise it. Several due together make one alert."""
    if not alert_enabled("requests"):
        return
    due = db.all("SELECT * FROM customer_requests WHERE business_id=? AND status='open' AND reminded_at IS NULL AND follow_up_on <= ? ORDER BY follow_up_on, id",
                 (biz, today_str()))
    if not due:
        return
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    claimed = [r for r in due if db.execute("UPDATE customer_requests SET reminded_at=? WHERE id=? AND reminded_at IS NULL", (stamp, r["id"])).rowcount == 1]
    if not claimed:
        return
    for r in claimed:
        request_log(db, biz, r["id"], "reminded", f"Follow-up day {r['follow_up_on']}")
    if len(claimed) == 1:
        r = claimed[0]
        title = f"Follow up: {r['customer_name']} asked for {request_label(r)}"
        body = f"{r['customer_name']}{' (' + r['customer_phone'] + ')' if r['customer_phone'] else ''} asked for {request_label(r)} on {r['created_at'][:10]}. Today is the day to follow it up."
        link = url_for("customer_request", cr_id=r["id"])
    else:
        title = f"Follow up {len(claimed)} customer requests"
        body = "Due today: " + "; ".join(f"{r['customer_name']}: {request_label(r)}" for r in claimed[:10]) + (" and more." if len(claimed) > 10 else ".")
        link = url_for("customer_requests", show="due")
    notify(db, "request", title, body, link, whatsapp=True)
    db.commit()


def remind_pay_due(db, biz):
    """On an employee's pay date the admins are alerted, once for that date, with what is owed and a link to their payslip, filled in and
    waiting to be checked and saved. Runs from the alerts poll; each employee is claimed before the alert is raised."""
    if not alert_enabled("payroll"):
        return
    today = today_str()
    due = db.all(f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE business_id=? AND next_pay_date IS NOT NULL AND next_pay_date <= ? "
                 "AND COALESCE(pay_reminded_for, '') <> next_pay_date ORDER BY next_pay_date, full_name", (biz, today))
    claimed = [e for e in due if db.execute("UPDATE employees SET pay_reminded_for=? WHERE id=? AND COALESCE(pay_reminded_for, '') <> ?",
                                            (e["next_pay_date"], e["id"], e["next_pay_date"])).rowcount == 1]
    if not claimed:
        return
    hr_set = hr_settings_for(db, biz)
    lines = []
    for e in claimed:
        p = pay_preview(db, biz, e, hr_set, e["next_pay_date"])
        how = f"{p['trays']:g} trays at {money(p['rate'])}" if p["basis"] == "trays" else f"{p['worked']:g} of {p['expected']:g} days"
        lines.append((e, p, how))
    if len(lines) == 1:
        e, p, how = lines[0]
        title = f"Pay due: {e['full_name']}, {money(p['amount'])}"
        body = (f"{e['full_name']} is paid {'today' if e['next_pay_date'] == today else 'since ' + e['next_pay_date']}: {money(p['amount'])} before NAPSA and NHIMA "
                f"({how}, {p['start']} to {p['end']}). The payslip is filled in, ready to check and save.")
        link = url_for("new_payslip", employee=e["id"], pay_date=e["next_pay_date"])
    else:
        title = f"Pay due for {len(lines)} employees"
        body = "Due: " + "; ".join(f"{e['full_name']} {money(p['amount'])}" for e, p, _ in lines[:10]) + ". Each payslip is filled in on HR & Payroll."
        link = url_for("hr") + "#pay"
    notify(db, "payroll", title, body, link, whatsapp=True)
    db.commit()


# --- THE REMINDER REPORT ---
# Every few days (Settings > Alerts; every 3 days unless changed) the admins get one alert gathering what is easy to forget: customers who
# still owe, seedling batches that are ready with seedlings left to sell, stock in the store with none on the POS, batches ready for longer
# than they should be, and pre-orders due soon (so the customers can be reminded). It opens the Reminders page, and the assistant tells each
# admin about it the next time they open it. Raised from the alerts poll, and each morning by the reminders cron (with the app closed).
REMINDER_EVERY = ((0, "Off"), (1, "Every day"), (2, "Every 2 days"), (3, "Every 3 days"), (7, "Once a week"))
REMINDER_EVERY_DEFAULT = 3
REMINDER_STALE_DEFAULT = 30
REMINDER_ORDER_DAYS = 7     # pre-orders due within this many days (and any overdue) are listed
REMINDER_SHOW = 12          # rows shown in each part of the Reminders page; the counts cover everything


def reminder_every(user):
    every = user.get("reminder_every")
    return REMINDER_EVERY_DEFAULT if every is None else every


def reminder_stale_days(user):
    return user.get("reminder_stale_days") or REMINDER_STALE_DEFAULT


def reminder_report(db, biz, stale_days=REMINDER_STALE_DEFAULT, admin=True):
    """What the reminder report holds, worked out now. Each part is a list (largest or soonest first); who owes only for admins."""
    today = today_str()
    out = {"today": today, "stale_days": stale_days}
    if admin:
        rows = db.all(
            """SELECT MAX(customer_id) AS customer_id, customer_name, MAX(customer_phone) AS customer_phone, COUNT(*) AS invoices,
                      SUM(balance_due) AS owed, MIN(created_at) AS since
               FROM invoices WHERE business_id=? AND kind='SALE' AND payment_status <> 'Cancelled' AND balance_due > 0.005
               GROUP BY customer_name ORDER BY owed DESC""",
            (biz,),
        )
        out["owing"] = [dict(r, since=(r["since"] or today)[:10], days=(datetime.date.fromisoformat(today) - datetime.date.fromisoformat((r["since"] or today)[:10])).days)
                        for r in rows]
        out["owing_total"] = sum(r["owed"] for r in rows)
    batches = with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? AND still_in_greenhouse=1", (biz,)))
    ready, stale = [], []
    for f in batches:
        if f["stage"] not in ("ready", "due") or f["available"] <= 0:
            continue
        since = f["actual_ready_on"] or f["ready_on"] or today
        f = dict(f, ready_since=since, ready_days=(datetime.date.fromisoformat(today) - datetime.date.fromisoformat(since)).days)
        (stale if f["ready_days"] > stale_days else ready).append(f)
    out["ready"] = sorted(ready, key=lambda f: -f["available"])
    out["stale"] = sorted(stale, key=lambda f: -f["ready_days"])
    store = db.all(
        """SELECT p.id, p.item_code, p.name, p.category, p.quantity, p.price, COALESCE(s.quantity, 0) AS on_pos
           FROM products p LEFT JOIN pos_products s ON s.business_id = p.business_id AND s.item_code = p.item_code
           WHERE p.business_id=? AND p.quantity > 0 AND p.price > 0 AND COALESCE(s.quantity, 0) <= 0 ORDER BY p.quantity DESC, p.name""",
        (biz,),
    )
    out["not_on_pos"] = [r for r in store if not is_seed_category(r["category"])]
    orders = db.all(
        """SELECT po.id, po.customer_name, po.customer_phone, po.qty, po.collect_on, p.name, p.batch_no, p.planted_on, p.id AS planting_id,
                  COALESCE(po.collect_on, p.actual_ready_on, p.ready_on) AS due
           FROM preorders po JOIN plantings p ON p.id = po.planting_id
           WHERE po.business_id=? AND po.status='pending' AND COALESCE(po.collect_on, p.actual_ready_on, p.ready_on) <= ?
           ORDER BY COALESCE(po.collect_on, p.actual_ready_on, p.ready_on), po.id""",
        (biz, add_days(today, REMINDER_ORDER_DAYS)),
    )
    out["orders"] = [dict(o, label=planting_label(o), overdue=(o["due"] or today) < today, by_collection=bool(o["collect_on"])) for o in orders]
    return out


REMINDER_PARTS = ("owing", "ready", "not_on_pos", "stale", "orders")
REMINDER_WORDS = {
    "owing": ("customer still owes", "customers still owe"), "ready": ("batch ready to sell", "batches ready to sell"),
    "not_on_pos": ("item in the store, none on the POS", "items in the store, none on the POS"),
    "stale": ("batch ready too long", "batches ready too long"), "orders": ("pre-order due soon", "pre-orders due soon"),
}


def reminder_counts(report):
    return [(key, len(report[key])) for key in REMINDER_PARTS if report.get(key)]


def reminder_lines(report, limit=3):
    """The report in short lines (the alert's text, the WhatsApp copy, the assistant's mention)."""
    lines = []
    if report.get("owing"):
        n = len(report["owing"])
        lines.append(f"{n} {REMINDER_WORDS['owing'][n != 1]}, {money(report['owing_total'])} in all: "
                     + ", ".join(f"{r['customer_name']} {money(r['owed'])}" for r in report["owing"][:limit]) + (" and more" if n > limit else ""))
    if report.get("ready"):
        lines.append("Ready to sell: " + ", ".join(f"{f['label']} ({f['available']:,} left)" for f in report["ready"][:limit]))
    if report.get("not_on_pos"):
        lines.append("In the store but not on the POS: " + ", ".join(f"{r['name']} ({r['quantity']:,})" for r in report["not_on_pos"][:limit]))
    if report.get("stale"):
        lines.append(f"Ready over {report['stale_days']} days: " + ", ".join(f"{f['label']} ({f['ready_days']} days, {f['available']:,} left)" for f in report["stale"][:limit]))
    if report.get("orders"):
        lines.append("Pre-orders to remind: " + ", ".join(
            f"{o['customer_name']} {o['qty']:,} x {o['name']} ({'overdue since ' + o['due'] if o['overdue'] else 'due ' + o['due']})" for o in report["orders"][:limit]))
    return lines


def remind_report_due(db, biz):
    """Raises the reminder report for the admins when it is due (every so many days), once. The day is claimed first, so two pages polling
    at once cannot both raise it; a report with nothing in it raises no alert but still counts as given."""
    every = reminder_every(g.user)
    if every <= 0 or g.user["demo_expires_at"]:
        return
    today = today_str()
    if (g.user.get("reminder_sent_on") or "") > add_days(today, -every):
        return
    if db.execute("UPDATE businesses SET reminder_sent_on=? WHERE id=? AND (reminder_sent_on IS NULL OR reminder_sent_on <= ?)",
                  (today, biz, add_days(today, -every))).rowcount != 1:
        db.commit()
        return
    db.commit()
    g.user = dict(g.user, reminder_sent_on=today)
    report = reminder_report(db, biz, reminder_stale_days(g.user))
    counts = reminder_counts(report)
    if not counts:
        return
    title = "Reminders: " + ", ".join(f"{n} {REMINDER_WORDS[key][n != 1]}" for key, n in counts)
    notify(db, "reminders", title, ". ".join(reminder_lines(report)) + ".", url_for("reminders"), whatsapp=True)
    db.commit()


@app.get("/reminders")
@need("admin")
def reminders():
    db, biz = get_db(), g.user["business_id"]
    report = reminder_report(db, biz, reminder_stale_days(g.user))
    every = reminder_every(g.user)
    return render_template(
        "reminders.html", r=report, show=REMINDER_SHOW, every=every, every_words=dict(REMINDER_EVERY).get(every, f"Every {every} days"),
        next_on=add_days(g.user["reminder_sent_on"], every) if every and g.user.get("reminder_sent_on") else None, order_days=REMINDER_ORDER_DAYS,
        counts=dict(reminder_counts(report)),
    )


def requests_arrived(db, biz, name, code, how):
    """Something was bought or planted: the admins are told which customers with an open request asked for it, and each request's log says so.
    The caller commits, so the alert goes only if what arrived is saved."""
    user = g.get("user")
    # Only for the signed-in person's own business (demo data is filled in for a business before anyone uses it)
    if not user or user.get("business_id") != biz or user.get("alert_requests") == 0:
        return
    waiting = [r for r in db.all("SELECT * FROM customer_requests WHERE business_id=? AND status='open'", (biz,))
               if (code and r["item_code"] == code) or request_fits(r["item_name"], name)]
    if not waiting:
        return
    for r in waiting:
        request_log(db, biz, r["id"], "arrived", f"{name}: {how}")
    people = [f"{r['customer_name']}" + (f" ({r['quantity']:,})" if r["quantity"] else "") for r in waiting]
    title = f"{len(waiting)} customer request{'s' if len(waiting) != 1 else ''} for {name}"
    body = f"{name}: {how}. Asked for by " + ", ".join(people[:10]) + (" and more" if len(people) > 10 else "") + ". Let them know."
    link = url_for("customer_request", cr_id=waiting[0]["id"]) if len(waiting) == 1 else url_for("customer_requests", show="open", q=" ".join(request_words(waiting[0]["item_name"])))
    notify(db, "request", title, body, link, whatsapp=True)


def pos_preorders(db, biz, ids):
    """The pending pre-orders asked for, ready to go into a POS cart: ([line], problem). They must all be one customer's."""
    ids = [i for i in dict.fromkeys(ids) if i][:50]
    if not ids:
        return [], None
    rows = db.all(
        f"""SELECT po.id, po.customer_name, po.customer_phone, po.qty, po.unit_price, po.collect_on, po.status, p.id AS planting_id, p.name,
                   p.batch_no, p.planted_on, p.pos_code, p.department
            FROM preorders po JOIN plantings p ON p.id = po.planting_id
            WHERE po.business_id=? AND po.id IN ({', '.join('?' for _ in ids)}) ORDER BY po.id""",
        [biz] + ids,
    )
    pending = [r for r in rows if r["status"] == "pending"]
    if not pending:
        return [], "Those pre-orders have already been collected or cancelled."
    key = customer_group_key(pending[0]["customer_name"], pending[0]["customer_phone"])
    if any(customer_group_key(r["customer_name"], r["customer_phone"]) != key for r in pending):
        return [], "Only one customer's pre-orders can go on one invoice."
    lines = [{
        "preorder": r["id"], "name": planting_label(r), "qty": r["qty"], "price": r["unit_price"], "total": round(r["qty"] * r["unit_price"], 2),
        "customer_name": r["customer_name"], "customer_phone": r["customer_phone"] or "", "collect_on": r["collect_on"] or "",
        "pos_code": r["pos_code"], "department": r["department"], "batch_no": r["batch_no"],
    } for r in pending]
    skipped = len(ids) - len(pending)
    return lines, (f"{skipped} of the pre-orders asked for had already been collected or cancelled, so {'it is' if skipped == 1 else 'they are'} not in the cart."
                   if skipped else None)


PLANTING_COLUMNS = {
    "item_code": ("item code", "code", "seed code", "sku"),
    "name": ("name", "seedling name", "seed name", "item name", "item", "seed", "plant"),
    "seeds": ("seeds planted", "seeds", "number of seeds", "quantity", "qty"),
    "planted_on": ("planted on", "date planted", "planting date", "date"),
    "ready_on": ("ready on", "expected ready", "expected ready date", "ready date", "ready", "date ready"),
    "actual_ready_on": ("actual ready date", "actual ready", "ready on actual", "marked ready"),
    "mortality": ("mortality", "expected mortality", "mortality rate", "expected mortality rate", "mortality %"),
    "price": ("selling price", "price", "seedling price"),
    "seed_cost": ("seed cost", "cost per seed", "seed unit cost", "unit cost"),
    "department": ("department", "dept"),
    "batch_no": ("batch no", "batch number", "batch"),
    "still_in_greenhouse": ("still in greenhouse", "in greenhouse", "still present", "currently in greenhouse", "in the greenhouse"),
    "notes": ("notes", "note", "comments"),
    "trays": ("trays planted", "trays", "number of trays", "no of trays", "tray count"),
    "holes": ("holes per tray", "holes", "holes in each tray", "cells per tray", "cells"),
    "planted_by": ("planted by", "planter", "planters", "worker", "worker name", "employee", "employee name", "staff"),
}
IMPORT_TEMPLATES["planting"] = (
    ["Item Code", "Name", "Seeds Planted", "Trays Planted", "Holes Per Tray", "Planted By", "Planted On", "Ready On", "Mortality", "Seed Cost", "Selling Price", "Department",
     "Batch No", "Actual Ready Date", "Still In Greenhouse", "Notes"],
    [
        # One batch planted by two people: a row for each, the same name, planting date and batch number. Seeds are trays x holes.
        ["TOM-001", "Tomato seedlings", "", 5, 242, "Mary Banda", "2026-10-01", "2026-11-05", 15, "", "", "Greenhouse", "B-22", "", "Yes", "Tray block A"],
        ["TOM-001", "Tomato seedlings", "", 3, 242, "John Phiri", "2026-10-01", "2026-11-05", 15, "", "", "Greenhouse", "B-22", "", "Yes", "Tray block A"],
        # A batch with seeds only, as before
        ["LET-001", "Lettuce seedlings", 3000, "", "", "", "2026-10-02", "2026-11-10", 10, "", "", "Greenhouse", "B-23", "", "Yes", ""],
    ],
)


def import_trays(raw):
    """A number of trays in an import file: above zero, part trays allowed."""
    value = round(parse_amount(raw), 2)
    if not 0 < value <= MAX_TRAYS:
        raise ValueError(raw)
    return value


PLANTING_BATCH_FIELDS = (
    ("item_code", "Item Code"), ("name", "Name"), ("seeds", "Seeds Planted"), ("holes", "Holes Per Tray"), ("planted_on", "Planted On"), ("ready_on", "Ready On"),
    ("actual_ready_on", "Actual Ready Date"), ("mortality", "Mortality"), ("price", "Selling Price"), ("seed_cost", "Seed Cost"), ("department", "Department"),
    ("batch_no", "Batch No"), ("still_in_greenhouse", "Still In Greenhouse"), ("notes", "Notes"),
)


def same_cell(field, a, b):
    """Two cells of one batch that say the same thing: 15 and 15.0, or the same date."""
    if field in ("planted_on", "ready_on", "actual_ready_on"):
        return (day_or_none(a) or a.lower()) == (day_or_none(b) or b.lower())
    if field in ("seeds", "holes", "mortality", "price", "seed_cost"):
        try:
            return parse_amount(a) == parse_amount(b)
        except ValueError:
            pass
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def join_batch_rows(rows, usual_holes):
    """One batch listed on several rows (a row for each person who planted part of it) made into the one record the rest of the import
    works on. Returns (record, problems). The trays are added up; the seeds are what was given, or trays x holes."""
    problems = []
    rec = dict(rows[0])
    for field, label in PLANTING_BATCH_FIELDS:
        filled = [r[field].strip() for r in rows if r[field].strip()]
        if not filled:
            continue
        rec[field] = filled[0]
        if any(not same_cell(field, filled[0], value) for value in filled[1:]):
            problems.append(f"{label} is not the same on every row of this batch")
    named = any(r["planted_by"].strip() for r in rows)
    shares, total = {}, 0.0
    for r in rows:
        who, raw = r["planted_by"].strip(), r["trays"].strip()
        if not raw:
            if who:
                problems.append(f"row {r['_row']}: give the Trays Planted by {who}")
            continue
        try:
            trays = import_trays(raw)
        except ValueError:
            problems.append(f"row {r['_row']}: trays planted '{raw}' is not a number above zero")
            continue
        total += trays
        if who:
            share = shares.setdefault(name_key(who), [clean_name(who, 100), 0.0, []])
            share[1] = round(share[1] + trays, 2)
            share[2].append(r["_row"])
        elif named:
            problems.append(f"row {r['_row']}: has trays but no Planted By, while other rows of this batch say who planted")
    total = round(total, 2)
    holes = None
    if rec["holes"]:
        try:
            holes = parse_whole(rec["holes"])
            if not 1 <= holes <= MAX_TRAY_HOLES:
                raise ValueError
        except ValueError:
            problems.append(f"holes per tray '{rec['holes']}' is not a whole number from 1 to {MAX_TRAY_HOLES:,}")
    elif usual_holes:
        holes = usual_holes
    if total and not rec["seeds"] and not problems:
        if holes:
            rec["seeds"] = str(int(math.floor(total * holes + 0.5 + 1e-9)))
        else:
            problems.append("give Seeds Planted, or Holes Per Tray (the usual number can be set under HR & Payroll > Settings)")
    rec["_trays"] = total or None
    rec["_holes"] = holes if total else None
    rec["_planters"] = [(key, name, n, found_rows) for key, (name, n, found_rows) in shares.items()]
    return rec, problems


def group_tray_rows(records, usual_holes):
    """Rows that name who planted, or give trays, are one person's share of a batch: rows with the same name (or code), planting date and
    batch number are joined into one batch. Every other row is a batch of its own, as it always was. Returns (records, errors)."""
    order, groups, errors, result = [], {}, [], []
    for rec in records:
        if not (rec["planted_by"].strip() or rec["trays"].strip()):
            order.append(rec)
            continue
        label = clean_name(rec["name"], 100) or clean_name(rec["item_code"], 30)
        planted = rec["planted_on"].strip()
        key = (norm(label) if label else rec["_row"], (day_or_none(planted) or planted.lower()) if planted else "", rec["batch_no"].strip().lower())
        if key not in groups:
            groups[key] = []
            order.append(groups[key])
        groups[key].append(rec)
    for item in order:
        if isinstance(item, dict):
            result.append(item)
            continue
        rec, problems = join_batch_rows(item, usual_holes)
        if problems:
            where = "Rows " + ", ".join(str(r["_row"]) for r in item) if len(item) > 1 else f"Row {item[0]['_row']}"
            errors.append(f"{where}: " + "; ".join(problems))
        else:
            result.append(rec)
    return result, errors


def in_record_only_window(day, start, end):
    """True when day falls inside an admin-set 'treat as a record only' date range (either end may be blank)."""
    if not start and not end:
        return False
    if start and day < start:
        return False
    if end and day > end:
        return False
    return True


@app.post("/import/planting")
@need("admin")
def import_planting():
    db, biz = get_db(), g.user["business_id"]
    try:
        records = read_csv_upload(PLANTING_COLUMNS, ("ready_on", "mortality"), one_of=("seeds", "trays"))
    except ImportProblem as e:
        flash(str(e), "error")
        return redirect(url_for("import_data"))

    # A row whose Still In Greenhouse column is blank falls back to this date filter: inside it, it is saved as a
    # historical record only; outside it (or with no filter set), it is treated as still growing, as before.
    record_from = record_to = ""
    if text("record_only_from"):
        record_from = day_or_none(text("record_only_from"))
        if record_from is None:
            flash("The 'treat as a record only from' date is not a date.", "error")
            return redirect(url_for("import_data"))
    if text("record_only_to"):
        record_to = day_or_none(text("record_only_to"))
        if record_to is None:
            flash("The 'treat as a record only to' date is not a date.", "error")
            return redirect(url_for("import_data"))
    if record_from and record_to and record_from > record_to:
        record_from, record_to = record_to, record_from

    departments = set(department_names(db, biz))
    fallback = "Greenhouse" if "Greenhouse" in departments else "General"
    seen = {(norm(r["name"]), r["planted_on"], r["batch_no"] or "", r["seeds_planted"]) for r in db.all("SELECT name, planted_on, batch_no, seeds_planted FROM plantings WHERE business_id=?", (biz,))}
    today = today_str()
    check = request.form.get("check") == "1"   # the Import page asks first which names need confirming; nothing is saved
    staff = db.all("SELECT id, full_name, nrc_number, employment_type FROM employees WHERE business_id=?", (biz,))
    records, errors = group_tray_rows(records, hr_settings_for(db, biz)["holes_per_tray"] or None)
    plans, new_departments = [], []

    for rec in records:
        problems = []
        name = clean_name(rec["name"], 100)
        code = clean_name(rec["item_code"], 30)
        if not name and not code:
            problems.append("give a Name or an Item Code")
        seeds = 0
        try:
            seeds = parse_whole(rec["seeds"])
            if seeds < 1:
                raise ValueError
        except ValueError:
            problems.append(f"seeds planted '{rec['seeds']}' is not a whole number above zero")
        mortality = 0.0
        try:
            mortality = parse_amount(rec["mortality"])
            if not 0 <= mortality < 100:
                raise ValueError
        except ValueError:
            problems.append(f"mortality '{rec['mortality']}' must be a percentage from 0 up to (not including) 100")
        planted_on = day_or_none(rec["planted_on"]) if rec["planted_on"] else today
        ready_on = day_or_none(rec["ready_on"])
        actual = day_or_none(rec["actual_ready_on"]) if rec["actual_ready_on"] else None
        if planted_on is None:
            problems.append(f"planted on '{rec['planted_on']}' is not a date (use YYYY-MM-DD or DD/MM/YYYY)")
        if ready_on is None:
            problems.append(f"ready on '{rec['ready_on']}' is not a date (use YYYY-MM-DD or DD/MM/YYYY)")
        elif planted_on and ready_on < planted_on:
            problems.append("ready on is before planted on")
        if rec["actual_ready_on"] and actual is None:
            problems.append(f"actual ready date '{rec['actual_ready_on']}' is not a date")
        seed_cost = price = None
        for field, label in (("seed_cost", "seed cost"), ("price", "selling price")):
            if rec[field]:
                try:
                    value = parse_amount(rec[field])
                    if value < 0:
                        raise ValueError
                except ValueError:
                    problems.append(f"{label} '{rec[field]}' is not a valid amount")
                    continue
                if field == "seed_cost":
                    seed_cost = value
                else:
                    price = value

        # Whether this batch is really still in the greenhouse, or just a historical record. An explicit Yes/No
        # in the file always wins; a blank cell falls back to the record-only date filter set on the Import page.
        still = None
        word = rec["still_in_greenhouse"].strip().lower()
        if word:
            if word in YES_WORDS:
                still = True
            elif word in NO_WORDS:
                still = False
            else:
                problems.append(f"'Still In Greenhouse' value '{rec['still_in_greenhouse']}' must be Yes or No")

        if problems:
            errors.append(f"Row {rec['_row']}: " + "; ".join(problems))
            continue
        if still is None:
            still = not in_record_only_window(planted_on, record_from, record_to)
        label = name or code
        key = (norm(label), planted_on, rec["batch_no"] or "", seeds)
        if key in seen:
            errors.append(f"Row {rec['_row']}: '{label}' with {seeds:,} seeds planted on {planted_on} is already recorded (was this file imported before?)")
            continue
        seen.add(key)
        dept = rec["department"] or fallback
        if dept not in departments:
            departments.add(dept)
            new_departments.append(dept)
        seedlings = seedlings_from(seeds, mortality)
        if seedlings < 1:
            errors.append(f"Row {rec['_row']}: at that mortality rate these seeds would give less than one seedling")
            continue
        seed_cost = seed_cost if seed_cost is not None else 0.0
        cost_per = seed_cost / (1 - mortality / 100)
        plans.append({
            "seed_code": code or seed_code_for(label), "seed_name": label, "name": label, "department": dept, "batch_no": rec["batch_no"][:40], "source": "import",
            "seeds": seeds, "mortality": mortality, "seedlings": seedlings, "seed_cost": seed_cost, "cost_per_seedling": cost_per,
            "price": round(price if price is not None else round_up_to_cent(cost_per), 2), "planted_on": planted_on, "ready_on": ready_on,
            "actual_ready_on": actual, "still_in_greenhouse": still, "notes": rec["notes"][:300],
            "trays": rec.get("_trays"), "holes_per_tray": rec.get("_holes"), "planters": rec.get("_planters", []),
        })

    if errors:
        return jsonify(questions=[]) if check else import_errors("planting records", errors)

    # Who planted: a name that is exactly an employee's is theirs; one that is only close, or nobody's, is asked about in the dialog box
    names = {}
    for plan in plans:
        for key, display, _, found_rows in plan["planters"]:
            names.setdefault(key, [display, []])[1].extend(found_rows)
    choices = read_name_choices(staff, allow_skip=True)
    resolved, asked = {}, {}
    for key, (display, found_rows) in names.items():
        how, found = match_person(staff, display)
        if how == "same":
            resolved[key] = ("use", found[0])
        elif key in choices:
            resolved[key] = ("new", display) if choices[key][0] == "new" else choices[key]
        else:
            asked[key] = person_question(key, display, how, found, found_rows)
    if check:
        return name_check_reply("planting", asked, staff)
    if asked:
        return import_errors("planting records", unconfirmed_names(asked))

    registered, attached = [], set()
    try:
        for dept in new_departments:
            db.execute("INSERT INTO departments (business_id, name) VALUES (?, ?)", (biz, dept))
        for key, answer in list(resolved.items()):
            if answer[0] == "new":
                worker = {"id": db.insert(
                    """INSERT INTO employees (business_id, full_name, nrc_number, basic_salary, employment_type, address, age, start_date, expected_days_week)
                       VALUES (?, ?, '', 0, 'Casual Worker', '', NULL, ?, 5)""",
                    (biz, answer[1], today),
                ), "full_name": answer[1]}
                resolved[key] = ("use", worker)
                registered.append(answer[1])
        for d in plans:
            planting_id = insert_planting(db, biz, d)
            given = {}
            for key, _, trays, _ in d["planters"]:
                if resolved[key][0] == "use":
                    worker = resolved[key][1]
                    given[worker["id"]] = [worker, round(given.get(worker["id"], [None, 0.0])[1] + trays, 2)]
            save_tray_rows(db, biz, planting_id, [(worker, trays) for worker, trays in given.values()])
            attached.update(given)
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The import failed while saving, so nothing was imported. Please try again.", "error")
        return redirect(url_for("import_data"))
    release_plantings(db, biz)
    record_only = sum(1 for d in plans if not d["still_in_greenhouse"])
    still_growing = len(plans) - record_only
    note = f": {still_growing} still in the greenhouse, {record_only} as a historical record only" if record_only else ""
    who = f" Trays were attached to {len(attached)} employee(s)." if attached else ""
    new = f" Registered as casual workers: {', '.join(registered)}." if registered else ""
    flash(f"Imported {len(plans)} planting record(s){note}.{who}{new} Inventory was not changed. Batches within a week of being ready are already on the POS.", "ok")
    return redirect(url_for("planting_list"))


# --- STOCK ADJUSTMENTS (write-offs from the POS, inventory quantity corrections) ---
@app.get("/stock-adjustments")
@need("admin")
def stock_adjustments():
    db, biz = get_db(), g.user["business_id"]
    kind = request.args.get("kind", "") if request.args.get("kind") in ("pos_removal", "inventory_edit") else ""
    sql = """SELECT id, kind, item_code, item_name, department, quantity_before, quantity_after, delta, reason, note, created_by, created_at
             FROM stock_adjustments WHERE business_id=?"""
    params = [biz]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    rows = db.all(sql + " ORDER BY id DESC LIMIT 500", params)
    return render_template("stock_adjustments.html", rows=rows, kind=kind)


# --- ACCOUNTS PAGE (ADMIN) ---
# Money in and money out over a chosen period: how much came in and went out, through which channel (cash, bank, mobile money),
# where it came from and what it was spent on, how it moved day by day or month by month, and every entry behind those totals.
ACCOUNT_PARAMS = ("period", "from", "to", "direction", "channel", "category", "q")
ACCOUNT_LIST_LIMIT = 500


def account_filters(db=None, biz=None, counts=None):
    """What the Accounts page shows: a date range for everything, and the narrowing of its list of entries. "Not in any balance
    yet" lists only what came after the balances were counted (what came before is part of the counts)."""
    period, start, end = invoice_period(default="this_month")
    pick = lambda key, allowed: request.args.get(key, "") if request.args.get(key, "") in allowed else ""  # noqa: E731
    since = None
    if db is not None and request.args.get("channel") == "Unassigned":
        counts = balance_counts(db, biz) if counts is None else counts
        since = min(boundary_of(c) for c in counts.values()) if counts else None
    return {
        "since": since,
        "period": period, "start": start, "end": end,
        "direction": pick("direction", ("in", "out")), "channel": pick("channel", list(dict(CHANNELS)) + ["Unassigned"]),
        "category": pick("category", CATEGORY_LABELS), "q": " ".join(request.args.get("q", "").split())[:100],
    }


def days_where(start, end, column="occurred_at"):
    """SQL limiting a timestamp column to whole days; either day may be None (no limit on that side)."""
    where, params = "", []
    if start:
        where += f" AND {column} >= ?"
        params.append(start.isoformat())
    if end:
        where += f" AND {column} < ?"
        params.append((end + datetime.timedelta(days=1)).isoformat())
    return where, params


def entries_where(biz, f):
    """The WHERE clause shared by the list of entries, its totals and its CSV export."""
    where, params = days_where(f["start"], f["end"], "c.occurred_at")
    if f["direction"]:
        where += " AND c.direction=?"
        params.append(f["direction"])
    if f.get("since"):
        clause, values = after(f["since"], "c.")
        where, params = where + " AND " + clause, params + values
    if f["channel"]:
        methods = UNASSIGNED_METHODS if f["channel"] == "Unassigned" else dict(CHANNELS)[f["channel"]]
        where += f" AND c.method IN ({', '.join('?' for _ in methods)})"
        params += list(methods)
    if f["category"]:
        where += " AND c.category=?"
        params.append(f["category"])
    for word in f["q"].lower().split():
        like = f"%{word}%"
        where += """ AND (LOWER(COALESCE(c.party, '')) LIKE ? OR LOWER(COALESCE(c.description, '')) LIKE ?
                          OR LOWER(COALESCE(c.reference, '')) LIKE ? OR LOWER(COALESCE(c.recorded_by, '')) LIKE ?)"""
        params += [like] * 4
    return "c.business_id=?" + where, [biz] + params


def entry_link(row):
    """Where an entry came from, for a link back to it."""
    source, ref = row["source"], row["source_id"]
    if source == "invoice" and ref:
        return url_for("invoice", invoice_id=ref)
    if source == "purchase" and ref:
        return url_for("procurement", purchase_id=ref)
    if source == "supplier_payment" and row.get("supplier_id"):
        return url_for("supplier_statement", supplier_id=row["supplier_id"])
    if source == "customer" and ref:
        return url_for("customer_statement", customer_id=ref)
    if source == "payslip" and ref:
        return url_for("payslips", id=ref)
    return None


def account_entries(db, biz, f, limit):
    where, params = entries_where(biz, f)
    rows = db.all(
        f"""SELECT c.id, c.direction, c.amount, c.method, c.reference, c.category, c.party, c.description, c.source, c.source_id,
                   c.occurred_at, c.recorded_by, sp.supplier_id
            FROM cashbook c LEFT JOIN supplier_payments sp ON c.source = 'supplier_payment' AND sp.id = c.source_id
            WHERE {where} ORDER BY c.occurred_at DESC, c.id DESC LIMIT ?""",
        params + [limit],
    )
    for r in rows:
        r["label"] = CATEGORY_LABELS.get(r["category"], r["category"])
        r["channel"] = CHANNEL_OF.get(r["method"], "Other")
        r["link"] = entry_link(r)
    return rows


def account_summary(db, biz, start, end):
    """Totals for the period: in, out, by channel and method, and by where money came from and what it went on."""
    where, params = days_where(start, end)
    groups = db.all(
        f"""SELECT direction, method, category, COALESCE(SUM(amount), 0) AS total FROM cashbook
            WHERE business_id=?{where} GROUP BY direction, method, category""",
        [biz] + params,
    )
    flows = {}
    for r in groups:
        flows.setdefault(r["method"], {"in": 0.0, "out": 0.0})[r["direction"]] += r["total"]

    channels, known = [], set(CHANNEL_OF)
    for name, methods in CHANNELS:
        # A method the app no longer knows (from an old record) is shown with Other
        members = list(methods) + ([m for m in flows if m not in known] if name == "Other" else [])
        rows = []
        for m in members:
            fin, fout = flows.get(m, {}).get("in", 0.0), flows.get(m, {}).get("out", 0.0)
            if abs(fin) > 0.005 or abs(fout) > 0.005:
                rows.append({"name": m, "in": fin, "out": fout, "net": fin - fout})
        if rows:
            channels.append({
                "name": name, "methods": rows if len(rows) > 1 else [],
                "in": sum(r["in"] for r in rows), "out": sum(r["out"] for r in rows), "net": sum(r["net"] for r in rows),
            })

    def ranked(direction, categories):
        totals = {}
        for r in groups:
            if r["direction"] == direction:
                totals[r["category"]] = totals.get(r["category"], 0.0) + r["total"]
        whole = sum(v for v in totals.values() if v > 0) or 1.0
        known_keys = [key for key, _ in categories]
        rows = [
            {"key": key, "label": CATEGORY_LABELS.get(key, key), "total": value, "pct": max(0.0, round(value / whole * 100, 1))}
            for key, value in totals.items() if abs(value) > 0.005
        ]
        return sorted(rows, key=lambda r: (-r["total"], known_keys.index(r["key"]) if r["key"] in known_keys else 99))

    money_in = sum(r["total"] for r in groups if r["direction"] == "in")
    money_out = sum(r["total"] for r in groups if r["direction"] == "out")
    return {
        "in": money_in, "out": money_out, "net": money_in - money_out, "channels": channels,
        "sources": ranked("in", MONEY_IN), "purposes": ranked("out", MONEY_OUT),
    }


def account_flow(db, biz, start, end, counts):
    """Money in and out per day (a period up to two months) or per month, with a running balance. Once the balances are counted,
    that is what is held in them (worked out from each count, so it is right before the count as well as after); until then,
    everything ever recorded in less everything out. counts is balance_counts(db, biz)."""
    by_day = bool(start and end and (end - start).days <= 62)
    width = 10 if by_day else 7
    where, params = days_where(start, end)
    rows = db.all(
        f"""SELECT SUBSTR(occurred_at, 1, {width}) AS bucket, direction, method, COALESCE(SUM(amount), 0) AS total FROM cashbook
            WHERE business_id=?{where} GROUP BY bucket, direction, method ORDER BY bucket""",
        [biz] + params,
    )
    if counts:
        tracked = {m for key in counts for m in dict(CHANNELS)[key]}
        first = start.isoformat() if start else (min((r["bucket"] for r in rows), default=today_str()) + "-01")[:10]
        opening, lines = flow_lines(rows, by_day, sum(balances_at(db, biz, counts, first)[0].values()), tracked)
        return {"by_day": by_day, "opening": opening, "lines": lines, "has_start": bool(start), "counted": True}
    opening = 0.0
    if start:
        before = db.one(
            """SELECT COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE 0 END), 0) AS cin,
                      COALESCE(SUM(CASE WHEN direction='out' THEN amount ELSE 0 END), 0) AS cout
               FROM cashbook WHERE business_id=? AND occurred_at < ?""",
            (biz, start.isoformat()),
        )
        opening = before["cin"] - before["cout"]
    opening, lines = flow_lines(rows, by_day, opening, None)
    return {"by_day": by_day, "opening": opening, "lines": lines, "has_start": bool(start), "counted": False}


def flow_lines(rows, by_day, opening, tracked):
    """The cash flow table's lines from (bucket, direction, method, total) rows. The balance moves with the entries through the
    tracked methods only (every method when tracked is None)."""
    buckets = {}
    for r in rows:
        b = buckets.setdefault(r["bucket"], {"in": 0.0, "out": 0.0, "moved": 0.0})
        b[r["direction"]] += r["total"]
        if tracked is None or r["method"] in tracked:
            b["moved"] += r["total"] if r["direction"] == "in" else -r["total"]
    lines, balance = [], opening
    for bucket in sorted(buckets):
        b = buckets[bucket]
        balance += b["moved"]
        if by_day:
            label = datetime.date.fromisoformat(bucket).strftime("%a %d %b %Y")
        else:
            label = datetime.date(int(bucket[:4]), int(bucket[5:7]), 1).strftime("%B %Y")
        lines.append({"label": label, "in": b["in"], "out": b["out"], "net": b["in"] - b["out"], "balance": balance})
    return opening, lines


# --- Balances: what is really held in cash, at the bank and in each mobile money wallet ---
# The owner counts each balance at a moment; from then on every entry paid through it moves it (cash for Cash, the bank for bank
# transfers, cheques and cards, each wallet for its own mobile money). Entries dated before the moment are already part of the count,
# so earlier records never change a balance. Counting again later starts afresh from the new count, and keeps what the app expected
# at that moment, so a shortfall or surplus shows.
# Times are kept to the second, so a count also keeps the last entry that existed when it was saved: an entry dated in that same
# second is after the count only if it was recorded after it. Every entry is then on exactly one side of every count.
def balance_counts(db, biz):
    """The latest count of each balance: {channel: row}."""
    rows = db.all(
        """SELECT account, amount, as_of, last_id, expected, set_by, set_at FROM money_balances
           WHERE id IN (SELECT MAX(id) FROM money_balances WHERE business_id=? GROUP BY account)""",
        (biz,),
    )
    return {r["account"]: r for r in rows if r["account"] in dict(BALANCE_ACCOUNTS)}


def boundary_of(count):
    return (count["as_of"], count["last_id"] or 0)


def after(point, prefix=""):
    """SQL for the entries after a point in time: (timestamp, last entry already there). A plain moment is (timestamp, 0)."""
    return f"({prefix}occurred_at > ? OR ({prefix}occurred_at = ? AND {prefix}id > ?))", [point[0], point[0], point[1]]


def balances_at(db, biz, counts, point=None, loose_since=None):
    """Every counted balance at a point in time (now, when None), each worked forwards or backwards from its own count, in one query:
    ({channel: amount}, loose). A point is (timestamp, last entry) or just a timestamp (the start of that moment). With loose_since,
    loose also counts the entries after that point that belong to no balance: {"n", "cin", "cout"}."""
    if point is not None and not isinstance(point, tuple):
        point = (point, 0)
    signed = "CASE WHEN direction='in' THEN amount ELSE -amount END"
    columns, params = [], []
    for i, (key, count) in enumerate(counts.items()):
        methods = dict(CHANNELS)[key]
        base = boundary_of(count)
        # Forwards from the count to the point (or to now), or backwards from the count to a point before it
        low, high = (base, point) if point is None or point >= base else (point, base)
        where, values = f"method IN ({', '.join('?' for _ in methods)})", list(methods)
        clause, more = after(low)
        where, values = where + " AND " + clause, values + more
        if high:
            clause, more = after(high)
            where, values = where + " AND NOT " + clause, values + more
        columns.append(f"COALESCE(SUM(CASE WHEN {where} THEN {signed} ELSE 0 END), 0) AS b{i}")
        params += values
    if loose_since:
        clause, more = after(loose_since)
        where = f"method IN ({', '.join('?' for _ in UNASSIGNED_METHODS)}) AND {clause}"
        columns += [
            f"COALESCE(SUM(CASE WHEN {where} THEN 1 ELSE 0 END), 0) AS loose_n",
            f"COALESCE(SUM(CASE WHEN {where} AND direction='in' THEN amount ELSE 0 END), 0) AS loose_in",
            f"COALESCE(SUM(CASE WHEN {where} AND direction='out' THEN amount ELSE 0 END), 0) AS loose_out",
        ]
        params += (list(UNASSIGNED_METHODS) + more) * 3
    if not columns:
        return {}, None
    row = db.one(f"SELECT {', '.join(columns)} FROM cashbook WHERE business_id=?", params + [biz])
    amounts = {}
    for i, (key, count) in enumerate(counts.items()):
        base = boundary_of(count)
        moved = row[f"b{i}"]
        amounts[key] = count["amount"] + moved if point is None or point >= base else count["amount"] - moved
    loose = {"n": int(row["loose_n"]), "cin": row["loose_in"], "cout": row["loose_out"]} if loose_since else None
    return amounts, loose


def balance_at(db, biz, channel, count, point=None):
    """One balance at a point in time (now, when None), worked forwards or backwards from its count."""
    return balances_at(db, biz, {channel: count}, point)[0][channel]


def balances_now(db, biz, counts=None):
    """Each balance as it stands now, their total, and the entries since the counts that belong to none of them."""
    counts = balance_counts(db, biz) if counts is None else counts
    if not counts:
        return None
    earliest = min(boundary_of(c) for c in counts.values())
    amounts, loose = balances_at(db, biz, counts, loose_since=earliest)
    accounts = [
        {"key": key, "label": label, "amount": round(amounts[key], 2), "count": counts[key]}
        for key, label in BALANCE_ACCOUNTS if key in counts
    ]
    return {
        "accounts": accounts, "total": round(sum(a["amount"] for a in accounts), 2), "since": earliest[0],
        "counted": max(c["as_of"] for c in counts.values()), "loose": loose,
    }


def balance_history(db, biz):
    """Every time the balances were counted, newest first: [{"as_of", "set_by", "rows": [(label, counted, expected)]}]."""
    labels = dict(BALANCE_ACCOUNTS)
    history = []
    for r in db.all("SELECT account, amount, expected, as_of, set_by, set_at FROM money_balances WHERE business_id=? ORDER BY id DESC LIMIT 80", (biz,)):
        label = labels.get(r["account"], r["account"])
        # One count saves every balance together; a balance seen again is the count before it
        if not history or history[-1]["set_at"] != r["set_at"] or history[-1]["as_of"] != r["as_of"] or label in [x[0] for x in history[-1]["rows"]]:
            history.append({"as_of": r["as_of"], "set_at": r["set_at"], "set_by": r["set_by"], "rows": []})
        history[-1]["rows"].append((label, r["amount"], r["expected"]))
    return history[:12]


# --- Financial standing: everything the records say about where the business stands, on one page ---
# What it is worth today: the money it holds, what customers owe it and the stock it has (at what it cost), less what it owes
# suppliers and lenders. And whether it made a profit over the chosen period: sales made (paid or not) and other income, less
# purchases made (paid or not) and running costs. Stock bought and not yet sold counts as spent in the profit; it is in the stock
# figure instead. Owner's money put in or taken out, loans and refunds are not income or costs.
RUNNING_COSTS = ("salaries", "statutory", "rent", "utilities", "transport", "repairs", "charges", "tax", "other_expense")
STANDING_LIST = 8   # the biggest few customers owing and suppliers owed, on the page (the downloads have them all)


def stock_value(db, biz):
    """Stock at what it cost: inventory items, what is listed in the POS, and seedlings in the greenhouse (growing, ready or
    pre-ordered and not yet collected). POS items whose cost is not known are counted apart. Returns totals and every line."""
    lines = []
    products = db.all(
        "SELECT item_code, name, department, quantity, unit_cost, seeds_per_pack, loose_seeds FROM products WHERE business_id=? ORDER BY name", (biz,)
    )
    for p in products:
        cost, units = p["unit_cost"] or 0.0, max(p["quantity"] or 0, 0)
        value = units * cost
        # Seeds left over from an opened pack are worth their share of a pack
        if p["seeds_per_pack"] and (p["loose_seeds"] or 0) > 0:
            value += p["loose_seeds"] * cost / p["seeds_per_pack"]
        if value > 0.005:
            lines.append({"where": "Inventory", "code": p["item_code"], "name": p["name"], "department": p["department"], "units": units, "cost": cost, "value": round(value, 2)})

    every_batch = db.all("SELECT * FROM plantings WHERE business_id=? ORDER BY planted_on, id", (biz,))
    batch_codes = {b["pos_code"] for b in every_batch if b["pos_code"]}
    for p in with_figures(db, biz, [b for b in every_batch if b["still_in_greenhouse"]]):
        units = p["available"] + p["pending"]
        if units > 0:
            lines.append({
                "where": "Greenhouse", "code": p["pos_code"], "name": p["label"], "department": p["department"], "units": units,
                "cost": p["cost_per_seedling"] or 0.0, "value": round(units * (p["cost_per_seedling"] or 0.0), 2),
            })

    # In the POS: seedlings from a sowing cost what the sowing worked out; anything moved from inventory costs what the item cost
    sown = {
        r["pos_code"]: r["cost_per_seedling"]
        for r in db.all("SELECT pos_code, cost_per_seedling FROM seedling_batches WHERE business_id=? ORDER BY id", (biz,))
    }
    bought = {r["item_code"]: r["unit_cost"] for r in products}
    uncosted = {"units": 0, "count": 0, "at_price": 0.0}
    for p in db.all("SELECT item_code, name, department, quantity, price FROM pos_products WHERE business_id=? AND quantity > 0 ORDER BY name", (biz,)):
        if p["item_code"] in batch_codes:
            continue   # a greenhouse batch, counted above
        cost = sown.get(p["item_code"], bought.get(p["item_code"]))
        if cost is None:
            uncosted["units"] += p["quantity"]
            uncosted["count"] += 1
            uncosted["at_price"] += p["quantity"] * (p["price"] or 0.0)
            lines.append({"where": "POS (cost not known)", "code": p["item_code"], "name": p["name"], "department": p["department"], "units": p["quantity"], "cost": None, "value": 0.0})
            continue
        lines.append({"where": "POS", "code": p["item_code"], "name": p["name"], "department": p["department"], "units": p["quantity"], "cost": cost, "value": round(p["quantity"] * cost, 2)})

    total = lambda where: round(sum(line["value"] for line in lines if line["where"] == where), 2)  # noqa: E731
    return {
        "inventory": total("Inventory"), "pos": total("POS"), "greenhouse": total("Greenhouse"),
        "total": round(total("Inventory") + total("POS") + total("Greenhouse"), 2), "uncosted": uncosted, "lines": lines,
    }


def customer_names(db, biz):
    return {c["id"]: c["name"] for c in db.all("SELECT id, name FROM customers WHERE business_id=?", (biz,))}


def supplier_names(db, biz):
    return {s["id"]: s["name"] for s in db.all("SELECT id, name FROM suppliers WHERE business_id=?", (biz,))}


def customers_owing(db, biz, names=None):
    """Who owes the business money on sales, most first: [{"name", "owed", "invoices", "link"}]. names is customer_names(db, biz)."""
    rows = db.all(
        """SELECT customer_id, customer_name, customer_phone, COUNT(*) AS invoices, SUM(balance_due) AS owed, MIN(created_at) AS since FROM invoices
           WHERE business_id=? AND kind='SALE' AND payment_status <> 'Cancelled' AND balance_due > 0.005
           GROUP BY customer_id, customer_name, customer_phone""",
        (biz,),
    )
    names = customer_names(db, biz) if names is None else names
    owing = {}
    for r in rows:
        # One line per customer account; a sale with no account stands on its own
        key = ("c", r["customer_id"]) if r["customer_id"] else ("n", r["customer_name"], r["customer_phone"])
        o = owing.setdefault(key, {
            "name": names.get(r["customer_id"]) or r["customer_name"] or WALK_IN, "phone": r["customer_phone"] or "", "owed": 0.0, "invoices": 0,
            "since": r["since"], "link": url_for("customer_statement", customer_id=r["customer_id"]) if r["customer_id"] else None,
        })
        o["owed"] += r["owed"]
        o["invoices"] += r["invoices"]
        o["since"] = min(o["since"] or "9999", r["since"] or "9999")
    return sorted(owing.values(), key=lambda o: -o["owed"])


def suppliers_owed(db, biz, names=None):
    """Who the business owes for purchases, most first: [{"name", "owed", "purchases", "link"}]. names is supplier_names(db, biz)."""
    rows = db.all(
        """SELECT supplier_id, supplier_name, total_cost - COALESCE(amount_paid, total_cost) AS owed, purchase_date FROM procurements
           WHERE business_id=? AND total_cost - COALESCE(amount_paid, total_cost) > 0.005""",
        (biz,),
    )
    names = supplier_names(db, biz) if names is None else names
    owed = {}
    for r in rows:
        key = r["supplier_id"] or ("n", r["supplier_name"] or "")
        o = owed.setdefault(key, {
            "name": names.get(r["supplier_id"]) or r["supplier_name"] or "No supplier named", "owed": 0.0, "purchases": 0, "since": r["purchase_date"],
            "link": url_for("supplier_statement", supplier_id=r["supplier_id"]) if r["supplier_id"] else None,
        })
        o["owed"] += r["owed"]
        o["purchases"] += 1
        o["since"] = min(o["since"] or "9999", r["purchase_date"] or "9999")
    return sorted(owed.values(), key=lambda o: -o["owed"])


def money_held(db, biz, balances):
    """The money the business holds: the balances as they stand when they have been counted (balances_now), otherwise everything
    recorded in less everything recorded out (which is only as good as the records)."""
    if balances:
        return {"amount": balances["total"], "counted": True, "accounts": [(a["label"], a["amount"]) for a in balances["accounts"]], "loose": balances["loose"]}
    row = db.one(
        """SELECT COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE -amount END), 0) AS net FROM cashbook WHERE business_id=?""", (biz,)
    )
    return {"amount": round(row["net"], 2), "counted": False, "accounts": [], "loose": None}


def category_totals(db, biz, start=None, end=None):
    """Cashbook totals per (direction, category), for a period or for all time."""
    where, params = days_where(start, end)
    return {
        (r["direction"], r["category"]): r["total"]
        for r in db.all(
            f"SELECT direction, category, COALESCE(SUM(amount), 0) AS total FROM cashbook WHERE business_id=?{where} GROUP BY direction, category",
            [biz] + params,
        )
    }


def period_profit(db, biz, start, end):
    """Earned against spent over a period: sales made (paid or not) and other income, less purchases made (paid or not) and running costs."""
    where, params = days_where(start, end, "created_at")
    sales = db.one(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS total FROM invoices WHERE business_id=? AND kind='SALE' AND payment_status <> 'Cancelled'{where}",
        [biz] + params,
    )
    where, params = days_where(start, end, "purchase_date")
    buys = db.one(
        f"""SELECT COALESCE(SUM(CASE WHEN COALESCE(in_inventory, 1) = 1 THEN total_cost ELSE 0 END), 0) AS stock,
                   COALESCE(SUM(CASE WHEN COALESCE(in_inventory, 1) = 1 THEN 0 ELSE total_cost END), 0) AS other
            FROM procurements WHERE business_id=?{where}""",
        [biz] + params,
    )
    moved = category_totals(db, biz, start, end)
    earned = [("Sales made" + (f" ({sales['n']:,} invoice{'s' if sales['n'] != 1 else ''})" if sales["n"] else ""), sales["total"])]
    if abs(moved.get(("in", "other_income"), 0.0)) > 0.005:
        earned.append((CATEGORY_LABELS["other_income"], moved[("in", "other_income")]))
    spent = [("Stock bought", buys["stock"]), ("Other purchases (not stock)", buys["other"])]
    spent += [(CATEGORY_LABELS[key], moved.get(("out", key), 0.0)) for key in RUNNING_COSTS]
    spent = [(label, value) for label, value in spent if abs(value) > 0.005]
    total_earned, total_spent = sum(v for _, v in earned), sum(v for _, v in spent)
    return {"earned": earned, "spent": spent, "total_earned": total_earned, "total_spent": total_spent, "profit": total_earned - total_spent}


def records_check(db, biz, customers=None, suppliers=None):
    """Whether the money each record says was paid is the money Accounts holds for it: what customers paid on their invoices against
    the money in from sales and customer payments, what was paid on purchases against the money out to suppliers and for purchases,
    and every payslip against its pay and NAPSA/NHIMA. Each check names where it does not agree."""
    checks = []

    def check(label, expected, recorded, parties):
        parties = sorted((p for p in parties if abs(p["diff"]) > 0.005), key=lambda p: -abs(p["diff"]))
        checks.append({"label": label, "expected": round(expected, 2), "recorded": round(recorded, 2), "ok": abs(expected - recorded) < 0.01 and not parties, "parties": parties})

    held = db.all(
        """SELECT source, direction, category, source_id, COALESCE(SUM(amount), 0) AS total FROM cashbook
           WHERE business_id=? AND source IN ('invoice', 'customer', 'purchase', 'payslip') GROUP BY source, direction, category, source_id""",
        (biz,),
    )

    def booked(source, direction, categories=None):
        found = {}
        for r in held:
            if r["source"] == source and r["direction"] == direction and (not categories or r["category"] in categories):
                found[r["source_id"]] = found.get(r["source_id"], 0.0) + r["total"]
        return found

    # Customers: a sale's payment is in Accounts against the invoice (paid at the till, or changed on the invoice) or against the
    # customer's account (a lump sum spread over their invoices). Refunds are money out and checked against nothing here.
    on_invoice, on_account = booked("invoice", "in"), booked("customer", "in")
    names = customer_names(db, biz) if customers is None else customers
    by_customer, parties, paid = {}, [], 0.0
    for inv in db.all("SELECT id, invoice_no, customer_id, customer_name, amount_paid FROM invoices WHERE business_id=? AND kind='SALE'", (biz,)):
        paid += inv["amount_paid"]
        gap = inv["amount_paid"] - on_invoice.get(inv["id"], 0.0)
        if inv["customer_id"]:
            by_customer[inv["customer_id"]] = by_customer.get(inv["customer_id"], 0.0) + gap
        elif abs(gap) > 0.005:
            parties.append({"name": f"Invoice {inv['invoice_no']} ({inv['customer_name'] or WALK_IN})", "diff": gap, "link": url_for("invoice", invoice_id=inv["id"])})
    for customer_id in set(by_customer) | set(on_account):
        gap = by_customer.get(customer_id, 0.0) - on_account.get(customer_id, 0.0)
        exists = customer_id in names
        parties.append({
            "name": names.get(customer_id, f"Customer account {customer_id} (no longer there)"), "diff": gap,
            "link": url_for("customer_statement", customer_id=customer_id) if exists else None,
        })
    in_book = sum(on_invoice.values()) + sum(on_account.values())
    check("Payments from customers", paid, in_book, parties)

    # Suppliers: a purchase's payment is in Accounts as a payment to the supplier (spread over their purchases) or, with no
    # supplier account (or paid before supplier payments were kept), against the purchase itself
    on_purchase = booked("purchase", "out")
    to_supplier = {
        r["supplier_id"]: r["total"]
        for r in db.all(
            """SELECT sp.supplier_id, COALESCE(SUM(c.amount), 0) AS total FROM cashbook c JOIN supplier_payments sp ON sp.id = c.source_id
               WHERE c.business_id=? AND c.source='supplier_payment' AND c.direction='out' GROUP BY sp.supplier_id""",
            (biz,),
        )
    }
    stray = db.one(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM cashbook c WHERE business_id=? AND source='supplier_payment'
           AND NOT EXISTS (SELECT 1 FROM supplier_payments sp WHERE sp.id = c.source_id)""",
        (biz,),
    )["total"]
    names = supplier_names(db, biz) if suppliers is None else suppliers
    by_supplier, parties, paid = {}, [], 0.0
    for pr in db.all(
        "SELECT id, name, supplier_id, supplier_name, COALESCE(amount_paid, total_cost) AS paid FROM procurements WHERE business_id=?", (biz,)
    ):
        paid += pr["paid"] or 0.0
        gap = (pr["paid"] or 0.0) - on_purchase.get(pr["id"], 0.0)
        if pr["supplier_id"]:
            by_supplier[pr["supplier_id"]] = by_supplier.get(pr["supplier_id"], 0.0) + gap
        elif abs(gap) > 0.005:
            parties.append({"name": f"Purchase of {pr['name']} ({pr['supplier_name'] or 'no supplier'})", "diff": gap, "link": url_for("procurement", purchase_id=pr["id"])})
    for supplier_id in set(by_supplier) | set(to_supplier):
        parties.append({
            "name": names.get(supplier_id, f"Supplier account {supplier_id} (no longer there)"), "diff": by_supplier.get(supplier_id, 0.0) - to_supplier.get(supplier_id, 0.0),
            "link": url_for("supplier_statement", supplier_id=supplier_id) if supplier_id in names else None,
        })
    if abs(stray) > 0.005:
        parties.append({"name": "Supplier payments no longer on any statement", "diff": -stray, "link": None})
    check("Payments to suppliers", paid, sum(on_purchase.values()) + sum(to_supplier.values()) + stray, parties)

    # Payslips: net pay and NAPSA/NHIMA (both shares) each leave as their own entry
    pay, levies = booked("payslip", "out", ("salaries",)), booked("payslip", "out", ("statutory",))
    parties, expected = [], 0.0
    slips = db.all(
        """SELECT id, employee_name, pay_month, net_payable,
                  COALESCE(napsa_amt, 0) + COALESCE(nhima_amt, 0) + COALESCE(employer_napsa, 0) + COALESCE(employer_nhima, 0) AS levies
           FROM payroll WHERE business_id=?""",
        (biz,),
    )
    for s in slips:
        expected += s["net_payable"] + s["levies"]
        gap = s["net_payable"] + s["levies"] - pay.get(s["id"], 0.0) - levies.get(s["id"], 0.0)
        parties.append({"name": f"Payslip: {s['employee_name']}, {s['pay_month']}", "diff": gap, "link": url_for("payslips", id=s["id"])})
    gone = set(pay) | set(levies)
    gone -= {s["id"] for s in slips}
    for slip_id in gone:
        parties.append({"name": f"A deleted payslip (number {slip_id})", "diff": -(pay.get(slip_id, 0.0) + levies.get(slip_id, 0.0)), "link": None})
    check("Payslips", expected, sum(pay.values()) + sum(levies.values()), parties)
    return checks


def financial_standing(db, biz, start, end, balances=False):
    """Everything on the financial standing: what the business is worth today, the profit over the period, and the records check.
    balances is balances_now(db, biz), when the caller has it already."""
    held, stock = money_held(db, biz, balances_now(db, biz) if balances is False else balances), stock_value(db, biz)
    customers, suppliers = customer_names(db, biz), supplier_names(db, biz)
    owing, owed = customers_owing(db, biz, customers), suppliers_owed(db, biz, suppliers)
    all_time = category_totals(db, biz)
    loans = round(all_time.get(("in", "loan_in"), 0.0) - all_time.get(("out", "loan_out"), 0.0), 2)
    receivable = round(sum(o["owed"] for o in owing), 2)
    payable = round(sum(o["owed"] for o in owed), 2)
    assets = [
        ("Money held" if held["counted"] else "Money recorded", held["amount"]),
        ("Customers owe us", receivable),
        ("Stock (at cost)", stock["total"]),
    ]
    liabilities = [("We owe suppliers", payable)]
    if loans > 0.005:
        liabilities.append(("Loans still to repay", loans))
    total_assets, total_liabilities = sum(v for _, v in assets), sum(v for _, v in liabilities)
    return {
        "held": held, "stock": stock, "owing": owing, "owed": owed, "loans": loans, "receivable": receivable, "payable": payable,
        "assets": assets, "liabilities": liabilities, "total_assets": round(total_assets, 2), "total_liabilities": round(total_liabilities, 2),
        "worth": round(total_assets - total_liabilities, 2),
        "owner_in": all_time.get(("in", "capital"), 0.0), "owner_out": all_time.get(("out", "drawings"), 0.0),
        "profit": period_profit(db, biz, start, end), "checks": records_check(db, biz, customers, suppliers), "as_of": now().strftime("%d %b %Y %H:%M"),
    }


def period_words(f):
    if f["start"] and f["end"]:
        return f["start"].strftime("%d %b %Y") + ("" if f["end"] == f["start"] else " to " + f["end"].strftime("%d %b %Y"))
    if f["start"]:
        return "From " + f["start"].strftime("%d %b %Y")
    if f["end"]:
        return "Up to " + f["end"].strftime("%d %b %Y")
    return "All dates"


@app.get("/accounts")
@need("admin")
def accounts():
    db, biz = get_db(), g.user["business_id"]
    counts = balance_counts(db, biz)
    f = account_filters(db, biz, counts)
    where, params = entries_where(biz, f)
    listed = db.one(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN c.direction='in' THEN c.amount ELSE 0 END), 0) AS cin,
                   COALESCE(SUM(CASE WHEN c.direction='out' THEN c.amount ELSE 0 END), 0) AS cout
            FROM cashbook c WHERE {where}""",
        params,
    )
    balances = balances_now(db, biz, counts)
    standing = financial_standing(db, biz, f["start"], f["end"], balances)
    args = {key: request.args.get(key) for key in ACCOUNT_PARAMS if request.args.get(key)}
    period_args = {key: value for key, value in args.items() if key in ("period", "from", "to")}
    return render_template(
        "accounts.html", f=f, periods=INVOICE_PERIODS, summary=account_summary(db, biz, f["start"], f["end"]),
        flow=account_flow(db, biz, f["start"], f["end"], counts), entries=account_entries(db, biz, f, ACCOUNT_LIST_LIMIT), listed=listed,
        standing=standing, standing_list=STANDING_LIST, period_label=period_words(f),
        standing_xlsx=url_for("standing_xlsx", **period_args), standing_pdf=url_for("standing_pdf", **period_args),
        owed_to_us=standing["receivable"], we_owe=standing["payable"], channels=[name for name, _ in CHANNELS], money_in=MONEY_IN, money_out=MONEY_OUT,
        manual_in=[(k, CATEGORY_LABELS[k]) for k in MANUAL_IN], manual_out=[(k, CATEGORY_LABELS[k]) for k in MANUAL_OUT],
        methods=PAYMENT_METHODS, csv_url=url_for("accounts_csv", **args), return_to=url_for("accounts", **args), today=today_str(),
        list_filtered=bool(f["direction"] or f["channel"] or f["category"] or f["q"]),
        balances=balances, balance_accounts=BALANCE_ACCOUNTS, balance_fields=BALANCE_FIELDS, history=balance_history(db, biz),
        assign_url=url_for("accounts", period="all", channel="Unassigned") + "#transactions" if balances else None,
        moment=now().strftime("%Y-%m-%dT%H:%M"), unassigned=UNASSIGNED_METHODS, method_names=METHOD_NAMES,
        # The list of entries showing just one channel or category of the period (from the totals above it), or all of it again
        narrow=lambda key, value: url_for("accounts", **{**period_args, key: value}) + "#transactions",
        clear_url=url_for("accounts", **period_args) + "#transactions",
    )


@app.get("/accounts.csv")
@need("admin")
def accounts_csv():
    db, biz = get_db(), g.user["business_id"]
    rows = account_entries(db, biz, account_filters(db, biz), 100000)
    return csv_response(
        "accounts.csv",
        ["Date", "In / Out", "Category", "From / To", "Details", "Method", "Channel", "Reference", "Amount", "Recorded by"],
        [
            (r["occurred_at"], "In" if r["direction"] == "in" else "Out", r["label"], r["party"], r["description"], r["method"], r["channel"],
             r["reference"], r["amount"], r["recorded_by"])
            for r in rows
        ],
    )


def standing_rows(s, period):
    """The financial standing as (section, item, amount) lines, for the downloads."""
    rows = [("What we have", label, value) for label, value in s["assets"]]
    rows += [("What we have", f"  of which {label}", value) for label, value in s["held"]["accounts"]]
    rows += [
        ("What we have", "  of which stock in inventory", s["stock"]["inventory"]), ("What we have", "  of which stock in the POS", s["stock"]["pos"]),
        ("What we have", "  of which seedlings in the greenhouse", s["stock"]["greenhouse"]), ("What we have", "Total", s["total_assets"]),
    ]
    rows += [("What we owe", label, value) for label, value in s["liabilities"]] + [("What we owe", "Total", s["total_liabilities"])]
    rows.append(("Worth", "What the business is worth (what we have less what we owe)", s["worth"]))
    p = s["profit"]
    rows += [(f"Profit, {period}", f"Earned: {label}", value) for label, value in p["earned"]]
    rows += [(f"Profit, {period}", f"Spent: {label}", value) for label, value in p["spent"]]
    rows.append((f"Profit, {period}", "Profit" if p["profit"] >= 0 else "Loss", p["profit"]))
    return rows


@app.get("/accounts/standing.xlsx")
@need("admin")
def standing_xlsx():
    db, biz = get_db(), g.user["business_id"]
    f = account_filters()
    s, period = financial_standing(db, biz, f["start"], f["end"]), period_words(f)
    stock_where = ("Inventory", "POS", "Greenhouse", "POS (cost not known)")
    sheets = [
        ("Financial standing", [("Section", TEXT), ("Item", TEXT), ("Amount", MONEY)], standing_rows(s, period)),
        ("Customers owing", [("Customer", TEXT), ("Phone", TEXT), ("Unpaid invoices", WHOLE), ("Owing since", TEXT), ("Owed", MONEY)],
         [(o["name"], o["phone"], o["invoices"], (o["since"] or "")[:10], o["owed"]) for o in s["owing"]]),
        ("Suppliers owed", [("Supplier", TEXT), ("Unpaid purchases", WHOLE), ("Owed since", TEXT), ("Owed", MONEY)],
         [(o["name"], o["purchases"], (o["since"] or "")[:10], o["owed"]) for o in s["owed"]]),
        ("Stock at cost", [("Where", TEXT), ("Code", TEXT), ("Item", TEXT), ("Department", TEXT), ("Units", WHOLE), ("Cost each", MONEY), ("Value", MONEY)],
         [(x["where"], x["code"], x["name"], x["department"], x["units"], x["cost"], x["value"])
          for x in sorted(s["stock"]["lines"], key=lambda x: (stock_where.index(x["where"]), x["name"]))]),
        ("Records check", [("Check", TEXT), ("Where", TEXT), ("Records say paid", MONEY), ("In Accounts", MONEY), ("Difference", MONEY)],
         [row for c in s["checks"] for row in [(c["label"], "All", c["expected"], c["recorded"], round(c["expected"] - c["recorded"], 2))]
          + [(c["label"], p["name"], None, None, round(p["diff"], 2)) for p in c["parties"]]]),
    ]
    return xlsx_response(f"financial-standing-{today_str()}.xlsx", workbook_bytes(sheets))


@app.get("/accounts/standing.pdf")
@need("admin")
def standing_pdf():
    db, biz = get_db(), g.user["business_id"]
    f = account_filters()
    s, period = financial_standing(db, biz, f["start"], f["end"]), period_words(f)
    return Response(
        standing_pdf_bytes(s, period), mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="financial-standing-{today_str()}.pdf"', "Cache-Control": "no-store"},
    )


def standing_pdf_bytes(s, period):
    """The financial standing as a printable A4 statement."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    base = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=base["Normal"], fontSize=8, leading=10)
    centre = ParagraphStyle("centre", parent=base["Normal"], alignment=1, fontSize=9, leading=12)
    title = ParagraphStyle("title", parent=base["Title"], fontSize=16, spaceAfter=2)
    heading = ParagraphStyle("heading", parent=base["Heading3"], fontSize=11, spaceBefore=10, spaceAfter=4)
    grey, line = colors.HexColor("#eeeeee"), colors.HexColor("#bbbbbb")

    def table(rows, widths, bold_rows=()):
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 9), ("LEADING", (0, 0), (-1, -1), 11), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("BACKGROUND", (0, 0), (-1, 0), grey),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, line), ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
        ]
        for r in bold_rows:
            style += [("FONTNAME", (0, r), (-1, r), "Helvetica-Bold"), ("LINEABOVE", (0, r), (-1, r), 0.8, colors.black)]
        return Table(rows, colWidths=[w * mm for w in widths], style=TableStyle(style))

    story = [Paragraph(escape(g.user["business_name"]), title)]
    for extra in (g.user["subtitle"], g.user["contact"]):
        if extra:
            story.append(Paragraph(escape(extra), centre))
    story += [Spacer(1, 4), Paragraph("<b>FINANCIAL STANDING</b>", centre), Paragraph(escape(f"As at {s['as_of']}"), centre), Spacer(1, 6)]

    story.append(Paragraph("What we have", heading))
    rows = [["Item", "Amount"]]
    for label, value in s["assets"]:
        rows.append([label, money(value)])
        if label.startswith("Money") and s["held"]["accounts"]:
            rows += [[f"      {name}", money(amount)] for name, amount in s["held"]["accounts"]]
        if label.startswith("Stock"):
            rows += [["      In inventory", money(s["stock"]["inventory"])], ["      In the POS", money(s["stock"]["pos"])],
                     ["      Seedlings in the greenhouse", money(s["stock"]["greenhouse"])]]
    rows.append(["Total", money(s["total_assets"])])
    story.append(table(rows, (130, 50), (len(rows) - 1,)))

    story.append(Paragraph("What we owe", heading))
    rows = [["Item", "Amount"]] + [[label, money(value)] for label, value in s["liabilities"]] + [["Total", money(s["total_liabilities"])]]
    story.append(table(rows, (130, 50), (len(rows) - 1,)))
    story.append(Spacer(1, 6))
    story.append(Table(
        [["What the business is worth", money(s["worth"])]], colWidths=[130 * mm, 50 * mm],
        style=TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 10), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LINEABOVE", (0, 0), (-1, 0), 0.8, colors.black), ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
        ]),
    ))

    p = s["profit"]
    story.append(Paragraph(escape(f"Profit: {period}"), heading))
    rows = [["Item", "Amount"]] + [[label, money(value)] for label, value in p["earned"]] + [["Total earned", money(p["total_earned"])]]
    earned_total = len(rows) - 1
    rows += [[label, money(-value)] for label, value in p["spent"]] + [["Total spent", money(-p["total_spent"])]]
    rows.append(["Profit" if p["profit"] >= 0 else "Loss", money(p["profit"])])
    story.append(table(rows, (130, 50), (earned_total, len(rows) - 2, len(rows) - 1)))
    story.append(Paragraph(
        "Sales and purchases count when they are made, paid or not. Stock bought and not yet sold counts as spent here; it is in the stock figure above.", small,
    ))

    for title_text, people, column in (("Customers who owe us", s["owing"], "Invoices"), ("Suppliers we owe", s["owed"], "Purchases")):
        if people:
            story.append(Paragraph(title_text, heading))
            rows = [["Name", column, "Since", "Owed"]]
            rows += [[Paragraph(escape(o["name"]), small), str(o.get("invoices", o.get("purchases"))), (o["since"] or "")[:10], money(o["owed"])] for o in people]
            story.append(table(rows, (95, 25, 30, 30)))

    problems = [c for c in s["checks"] if not c["ok"]]
    story.append(Paragraph("Records check", heading))
    if not problems:
        story.append(Paragraph("Every payment on sales, purchases and payslips agrees with Accounts.", small))
    for c in problems:
        story.append(Paragraph(escape(f"{c['label']}: the records say {money(c['expected'])}, Accounts holds {money(c['recorded'])}."), small))

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 9 * mm, f"Financial standing printed {s['as_of']}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        title="Financial standing", author=g.user["business_name"],
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def accounts_back():
    """Back to the Accounts page as it was, from the address the form was sent with (never anywhere else)."""
    back = text("back")
    return redirect(back if back.startswith("/accounts") and not back.startswith("//") else url_for("accounts"))


@app.post("/accounts/record")
@need("admin")
def record_account_entry():
    """Money in or out that no other page records: rent, fuel, owner's money put in, a loan, bank charges..."""
    db, biz = get_db(), g.user["business_id"]
    back = accounts_back()
    direction = text("direction")
    allowed = MANUAL_IN if direction == "in" else MANUAL_OUT if direction == "out" else ()
    category = text("category")
    try:
        amount = round(number("amount"), 2)
    except ValueError:
        amount = 0.0
    method = method_from_text(text("payment_method"))
    day = day_or_none(text("occurred_on")) if text("occurred_on") else None
    if category not in allowed:
        flash("Choose what the money was for from the list.", "error")
    elif amount <= 0:
        flash("Enter the amount, above zero.", "error")
    elif method is None:
        flash("Choose how the money was paid: " + ", ".join(METHOD_NAMES) + ".", "error")
    elif day is None or day > today_str():
        flash("Enter the date the money moved (today or earlier).", "error")
    else:
        record_money(
            db, biz, direction, amount, method, category, " ".join(text("payment_reference").split())[:60], clean_name(text("party"), 100),
            " ".join(text("description").split())[:200], "manual", None, stamp_for_day(day),
        )
        db.commit()
        flash(f"Recorded {money(amount)} {'in' if direction == 'in' else 'out'}: {CATEGORY_LABELS[category]}, by {method.lower()}.", "ok")
    return back


@app.post("/accounts/balances")
@need("admin")
def set_balances():
    """What is held in cash, at the bank and in each mobile money wallet at a moment (now, unless another time is given).
    From then on every payment in or out through each of them moves it."""
    db, biz = get_db(), g.user["business_id"]
    back = accounts_back()
    raw = text("as_of")
    current = now().replace(tzinfo=None)
    try:
        moment = datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M") if raw else current
    except ValueError:
        flash("Enter the date and time the balances were counted.", "error")
        return back
    if moment > current + datetime.timedelta(minutes=1):
        flash("The balances cannot be counted at a time that has not come yet.", "error")
        return back
    # The form gives minutes only: counted "now" means this very second, so nothing recorded earlier this minute is counted twice
    if moment.replace(second=0, microsecond=0) == current.replace(second=0, microsecond=0):
        moment = current
    amounts = {}
    for key, label in BALANCE_ACCOUNTS:
        try:
            amounts[key] = round(number(BALANCE_FIELDS[key]), 2)
        except ValueError:
            flash(f"Enter what is held in {label} (0 if there is nothing). Nothing was changed.", "error")
            return back
    as_of = moment.strftime("%Y-%m-%d %H:%M:%S")
    stamp = current.strftime("%Y-%m-%d %H:%M:%S")
    counts = balance_counts(db, biz)
    # Everything already recorded and dated up to the count is part of it
    last_id = db.one("SELECT COALESCE(MAX(id), 0) AS n FROM cashbook WHERE business_id=?", (biz,))["n"]
    expected_at = balances_at(db, biz, counts, (as_of, last_id))[0]
    differences = []
    try:
        for key, label in BALANCE_ACCOUNTS:
            expected = round(expected_at[key], 2) if key in counts else None
            db.execute(
                "INSERT INTO money_balances (business_id, account, amount, as_of, last_id, expected, set_by, set_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (biz, key, amounts[key], as_of, last_id, expected, g.user["username"], stamp),
            )
            if expected is not None and abs(amounts[key] - expected) > 0.005:
                gap = amounts[key] - expected
                differences.append(f"{label} is {money(abs(gap))} {'more' if gap > 0 else 'less'} than the app expected ({money(expected)})")
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The balances could not be saved. Please try again.", "error")
        return back
    flash(
        f"Balances counted at {moment.strftime('%d %b %Y %H:%M')}: {money(sum(amounts.values()))} in all. From now on every payment in or out "
        "through cash, the bank, Airtel Money or MTN Money moves its balance." + (" " + "; ".join(differences) + "." if differences else ""),
        "ok",
    )
    return back


@app.post("/accounts/<int:entry_id>/assign")
@need("admin")
def assign_account_entry(entry_id):
    """Says how an entry was paid when it did not (an older record, or a correction from an older page), so its balance includes it."""
    db, biz = get_db(), g.user["business_id"]
    back = accounts_back()
    method = method_from_text(text("payment_method"))
    entry = db.one("SELECT id, method, source, source_id FROM cashbook WHERE id=? AND business_id=?", (entry_id, biz))
    if entry is None:
        abort(404)
    if method not in METHOD_NAMES:
        flash("Choose how the money was paid: " + ", ".join(METHOD_NAMES) + ".", "error")
        return back
    if entry["method"] not in UNASSIGNED_METHODS:
        flash("That entry already says how it was paid. Change it where it came from.", "error")
        return back
    db.execute("UPDATE cashbook SET method=? WHERE id=?", (method, entry_id))
    # A supplier payment keeps its method on the supplier's statement too
    if entry["source"] == "supplier_payment" and entry["source_id"]:
        db.execute("UPDATE supplier_payments SET method=? WHERE id=? AND business_id=? AND method=?", (method, entry["source_id"], biz, entry["method"]))
    db.commit()
    flash(f"Entry assigned to {method}" + (f"; the {CHANNEL_OF[method]} balance now includes it." if CHANNEL_OF.get(method) in dict(BALANCE_ACCOUNTS) else "."), "ok")
    return back


@app.post("/accounts/<int:entry_id>/delete")
@need("admin")
def delete_account_entry(entry_id):
    """Only entries made on the Accounts page can be removed here; the rest follow the sale, payment or payslip they belong to."""
    db, biz = get_db(), g.user["business_id"]
    removed = db.execute("DELETE FROM cashbook WHERE id=? AND business_id=? AND source='manual'", (entry_id, biz)).rowcount
    db.commit()
    flash("Entry removed from Accounts." if removed else "That entry belongs to a sale, payment, purchase or payslip; change it there.", "ok" if removed else "error")
    return accounts_back()


def history_stamp(value):
    """A stored date or date-and-time as a full timestamp, or None when there is none."""
    value = (value or "").strip()
    if len(value) >= 19:
        return value[:19]
    return f"{value[:10]} 00:00:00" if len(value) >= 10 else None


def cashbook_history(db, biz):
    """The money recorded before Accounts existed, as cashbook entries. How it was paid was not kept for sales or for purchases
    without a supplier account, so those say 'Not recorded'; supplier payments kept theirs."""
    entries = []
    fallback = now().strftime("%Y-%m-%d %H:%M:%S")

    def add(direction, amount, method, category, when, party, description, source, source_id, reference=""):
        if abs(round(amount or 0.0, 2)) >= 0.005:
            entries.append((direction, round(amount, 2), method, reference or "", category, party or "", (description or "")[:200], source, source_id, when or fallback))

    def changes(kind, extra=""):
        found = {}
        for row in db.all(f"SELECT ref_id, old_paid, new_paid, note, changed_at FROM payment_log WHERE business_id=? AND kind=?{extra} ORDER BY id", (biz, kind)):
            found.setdefault(row["ref_id"], []).append(row)
        return found

    # Sales: what was paid at the time of sale, then every later change to it
    sale_changes = changes("sale")
    for inv in db.all("SELECT id, invoice_no, customer_name, amount_paid, created_at FROM invoices WHERE business_id=? AND kind='SALE'", (biz,)):
        logged = sale_changes.get(inv["id"], [])
        when = history_stamp(inv["created_at"])
        add("in", logged[0]["old_paid"] if logged else inv["amount_paid"], NOT_RECORDED, "sale", when, inv["customer_name"], f"Invoice {inv['invoice_no']}", "invoice", inv["id"])
        for c in logged:
            delta = c["new_paid"] - c["old_paid"]
            add(
                "in", delta, NOT_RECORDED if delta > 0 else CORRECTION, "customer_payment", history_stamp(c["changed_at"]) or when, inv["customer_name"],
                f"Invoice {inv['invoice_no']}" + (f": {c['note']}" if c["note"] else ""), "invoice", inv["id"],
            )

    # Payments to supplier accounts were kept with their method and reference
    for sp in db.all(
        """SELECT sp.id, sp.amount, sp.method, sp.reference, sp.paid_on, sp.note, sp.recorded_at, s.name
           FROM supplier_payments sp LEFT JOIN suppliers s ON s.id = sp.supplier_id WHERE sp.business_id=?""",
        (biz,),
    ):
        add("out", sp["amount"], sp["method"] or NOT_RECORDED, "supplier", history_stamp(sp["paid_on"]) or history_stamp(sp["recorded_at"]),
            sp["name"], sp["note"] or "Payment to supplier", "supplier_payment", sp["id"], sp["reference"])

    # Purchases: for a supplier account, whatever was paid outside the supplier payments above (from before they were kept);
    # with no supplier account, what was paid when it was logged and every later change
    allocated = {
        r["procurement_id"]: r["total"]
        for r in db.all("SELECT procurement_id, COALESCE(SUM(amount), 0) AS total FROM supplier_payment_allocations WHERE business_id=? GROUP BY procurement_id", (biz,))
    }
    purchase_changes = changes("purchase", " AND payment_id IS NULL")
    for pr in db.all(
        "SELECT id, name, quantity, supplier_name, supplier_id, purchase_date, COALESCE(amount_paid, total_cost) AS paid FROM procurements WHERE business_id=?",
        (biz,),
    ):
        when, what = history_stamp(pr["purchase_date"]), f"{pr['name']} x {pr['quantity'] or 0:,}"
        if pr["supplier_id"]:
            loose = (pr["paid"] or 0.0) - allocated.get(pr["id"], 0.0)
            if loose > 0.005:
                add("out", loose, NOT_RECORDED, "supplier", when, pr["supplier_name"], what, "purchase", pr["id"])
            continue
        logged = purchase_changes.get(pr["id"], [])
        add("out", logged[0]["old_paid"] if logged else pr["paid"], NOT_RECORDED, "purchase", when, pr["supplier_name"], what, "purchase", pr["id"])
        for c in logged:
            delta = c["new_paid"] - c["old_paid"]
            add("out", delta, NOT_RECORDED if delta > 0 else CORRECTION, "purchase", history_stamp(c["changed_at"]) or when, pr["supplier_name"], what, "purchase", pr["id"])

    # Payslips: the net pay, and NAPSA/NHIMA (both shares)
    for s in db.all(
        "SELECT id, employee_name, pay_month, net_payable, napsa_amt, nhima_amt, employer_napsa, employer_nhima, created_at FROM payroll WHERE business_id=?",
        (biz,),
    ):
        when = history_stamp(s["created_at"])
        add("out", s["net_payable"], NOT_RECORDED, "salaries", when, s["employee_name"], f"Pay for {s['pay_month']}", "payslip", s["id"])
        add("out", (s["napsa_amt"] or 0) + (s["nhima_amt"] or 0) + (s["employer_napsa"] or 0) + (s["employer_nhima"] or 0), NOT_RECORDED, "statutory", when,
            s["employee_name"], f"NAPSA & NHIMA for {s['pay_month']}", "payslip", s["id"])
    return entries


def backfill_cashbook():
    """Copies each business's earlier money records into the cashbook, once. Safe to repeat and to run on two servers at once:
    a business is claimed before its records are copied, and if anything fails nothing is kept, so it is tried again next start."""
    db = Database()
    try:
        for row in db.all("SELECT id FROM businesses WHERE cashbook_ready IS NULL"):
            biz = row["id"]
            if db.execute("UPDATE businesses SET cashbook_ready=1 WHERE id=? AND cashbook_ready IS NULL", (biz,)).rowcount != 1:
                db.rollback()
                continue
            stamp = now().strftime("%Y-%m-%d %H:%M:%S")
            entries = cashbook_history(db, biz)
            # Many rows per statement, so a long history is copied in a few round trips
            for i in range(0, len(entries), 60):
                chunk = entries[i:i + 60]
                db.execute(
                    """INSERT INTO cashbook (business_id, direction, amount, method, reference, category, party, description, source, source_id,
                                             occurred_at, recorded_by, recorded_at) VALUES """
                    + ", ".join("(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" for _ in chunk),
                    [value for e in chunk for value in (biz, *e, "Copied from earlier records", stamp)],
                )
            db.commit()
    except DB_ERRORS:
        db.rollback()
    finally:
        db.close()



# --- DAILY ACTIVITIES ---
# Work stations (plots) belong to a department and have workers from the payroll assigned to them (admins set these up). For a plot
# and a day, whoever is logging writes what its workers did: for each activity the worker (or everyone on the plot), morning or
# afternoon, what was done, the tools used, any product used with its quantity, and how much work was done. Products are recorded,
# not taken out of stock (stock used on the farm goes out through Transfer / Use). The day's report gathers it all by worker, and
# downloads as Excel.
ACTIVITY_PERIODS = (("morning", "Morning"), ("afternoon", "Afternoon"))


def activity_day():
    """The day being looked at (?date=), today by default and never later than today."""
    raw = request.args.get("date", "")
    day = day_or_none(raw) if raw else None
    return day if day and day <= today_str() else today_str()


def plots_with_workers(db, biz, include_retired=False):
    plots = db.all("SELECT id, department, name, notes, active FROM plots WHERE business_id=? ORDER BY department, name", (biz,))
    assigned = {}
    for w in db.all(
        """SELECT pw.plot_id, e.id, e.full_name FROM plot_workers pw JOIN employees e ON e.id = pw.employee_id
           WHERE pw.business_id=? ORDER BY e.full_name""",
        (biz,),
    ):
        assigned.setdefault(w["plot_id"], []).append({"id": w["id"], "name": w["full_name"]})
    for p in plots:
        p["workers"] = assigned.get(p["id"], [])
    return [p for p in plots if include_retired or p["active"]]


def get_plot(db, biz, plot_id):
    plot = next((p for p in plots_with_workers(db, biz, include_retired=True) if p["id"] == plot_id), None)
    if plot is None:
        abort(404)
    return plot


def day_activities(db, biz, day, plot_id=None):
    sql = """SELECT id, plot_id, plot_name, department, employee_id, worker_name, period, description, tools, product, product_qty, product_unit,
                    work_done, recorded_by, recorded_at
             FROM activities WHERE business_id=? AND work_date=?"""
    params = [biz, day]
    if plot_id:
        sql += " AND plot_id=?"
        params.append(plot_id)
    return db.all(sql + " ORDER BY worker_name, CASE period WHEN 'morning' THEN 0 ELSE 1 END, plot_name, id", params)


def by_worker(rows):
    """A day's activities grouped by worker, each with how many were in the morning and the afternoon, and where."""
    groups = []
    for r in rows:
        r["period_label"] = dict(ACTIVITY_PERIODS).get(r["period"], r["period"])
        if not groups or groups[-1]["worker"] != r["worker_name"]:
            groups.append({"worker": r["worker_name"], "entries": [], "morning": 0, "afternoon": 0, "plots": []})
        group = groups[-1]
        group["entries"].append(r)
        group[r["period"] if r["period"] in ("morning", "afternoon") else "morning"] += 1
        if r["plot_name"] not in group["plots"]:
            group["plots"].append(r["plot_name"])
    return groups


def activity_back(default):
    """Back to the activities page the form came from (never anywhere else)."""
    back = text("back")
    return redirect(back if back.startswith("/activities") and not back.startswith("//") else default)


def read_plot_form(db, biz):
    """The department, name, notes and workers of a plot from its form: (values, problem)."""
    department = text("department")
    name = clean_name(text("name"), 80)
    notes = " ".join(text("notes").split())[:200]
    staff = {e["id"] for e in db.all("SELECT id FROM employees WHERE business_id=?", (biz,))}
    workers = sorted({i for i in (request.form.getlist("workers", type=int)) if i in staff})
    if department not in department_names(db, biz):
        return None, "Choose the plot's department."
    if not name:
        return None, "Write the plot's name (for example Greenhouse 2, Block A)."
    return {"department": department, "name": name, "notes": notes, "workers": workers}, None


def plot_name_taken(db, biz, department, name, plot_id=None):
    return any(
        norm(r["name"]) == norm(name) and r["id"] != plot_id
        for r in db.all("SELECT id, name FROM plots WHERE business_id=? AND department=? AND active=1", (biz, department))
    )


def save_plot_workers(db, biz, plot_id, workers):
    db.execute("DELETE FROM plot_workers WHERE plot_id=? AND business_id=?", (plot_id, biz))
    for employee_id in workers:
        db.execute("INSERT INTO plot_workers (business_id, plot_id, employee_id) VALUES (?, ?, ?)", (biz, plot_id, employee_id))


@app.get("/activities")
@need(None)
def activities():
    db, biz = get_db(), g.user["business_id"]
    day = activity_day()
    rows = day_activities(db, biz, day)
    logged = {}
    for r in rows:
        logged[r["plot_id"]] = logged.get(r["plot_id"], 0) + 1
    shown = datetime.date.fromisoformat(day)
    return render_template(
        "activities.html", day=day, today=today_str(), shown=shown, plots=plots_with_workers(db, biz), logged=logged, groups=by_worker(rows),
        total=len(rows), prev_day=(shown - datetime.timedelta(days=1)).isoformat(), next_day=(shown + datetime.timedelta(days=1)).isoformat(),
        departments=department_names(db, biz), employees=db.all("SELECT id, full_name FROM employees WHERE business_id=? ORDER BY full_name", (biz,)),
        return_to=url_for("activities", date=day),
    )


@app.post("/activities/plots")
@need("admin")
def add_plot():
    db, biz = get_db(), g.user["business_id"]
    back = activity_back(url_for("activities"))
    plot, problem = read_plot_form(db, biz)
    if problem:
        flash(problem, "error")
        return back
    if plot_name_taken(db, biz, plot["department"], plot["name"]):
        flash(f"{plot['department']} already has a plot called '{plot['name']}'.", "error")
        return back
    plot_id = db.insert(
        "INSERT INTO plots (business_id, department, name, notes, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
        (biz, plot["department"], plot["name"], plot["notes"], now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    save_plot_workers(db, biz, plot_id, plot["workers"])
    db.commit()
    flash(f"Plot '{plot['name']}' added to {plot['department']} with {len(plot['workers'])} worker(s).", "ok")
    return back


@app.post("/activities/plots/<int:plot_id>/update")
@need("admin")
def update_plot(plot_id):
    db, biz = get_db(), g.user["business_id"]
    back = activity_back(url_for("activities"))
    get_plot(db, biz, plot_id)
    plot, problem = read_plot_form(db, biz)
    if problem:
        flash(problem, "error")
        return back
    if plot_name_taken(db, biz, plot["department"], plot["name"], plot_id):
        flash(f"{plot['department']} already has a plot called '{plot['name']}'.", "error")
        return back
    db.execute(
        "UPDATE plots SET department=?, name=?, notes=? WHERE id=? AND business_id=?",
        (plot["department"], plot["name"], plot["notes"], plot_id, biz),
    )
    save_plot_workers(db, biz, plot_id, plot["workers"])
    db.commit()
    flash(f"Plot '{plot['name']}' saved, with {len(plot['workers'])} worker(s). Activities already logged keep the names they were logged under.", "ok")
    return back


@app.post("/activities/plots/<int:plot_id>/delete")
@need("admin")
def remove_plot(plot_id):
    """A plot with activities on record is taken out of use (its records stay in the reports); one without any is deleted."""
    db, biz = get_db(), g.user["business_id"]
    plot = get_plot(db, biz, plot_id)
    used = db.one("SELECT COUNT(*) AS n FROM activities WHERE plot_id=? AND business_id=?", (plot_id, biz))["n"]
    db.execute("DELETE FROM plot_workers WHERE plot_id=? AND business_id=?", (plot_id, biz))
    if used:
        db.execute("UPDATE plots SET active=0 WHERE id=? AND business_id=?", (plot_id, biz))
        flash(f"Plot '{plot['name']}' is no longer in use. Its {used} logged activities stay in the reports.", "ok")
    else:
        db.execute("DELETE FROM plots WHERE id=? AND business_id=?", (plot_id, biz))
        flash(f"Plot '{plot['name']}' removed.", "ok")
    db.commit()
    return activity_back(url_for("activities"))


@app.get("/activities/plots/<int:plot_id>")
@need(None)
def plot_day(plot_id):
    db, biz = get_db(), g.user["business_id"]
    plot = get_plot(db, biz, plot_id)
    day = activity_day()
    rows = day_activities(db, biz, day, plot_id)
    shown = datetime.date.fromisoformat(day)
    return render_template(
        "plot_day.html", plot=plot, day=day, today=today_str(), shown=shown, groups=by_worker(rows), total=len(rows),
        prev_day=(shown - datetime.timedelta(days=1)).isoformat(), next_day=(shown + datetime.timedelta(days=1)).isoformat(),
        periods=ACTIVITY_PERIODS, default_period="morning" if now().hour < 12 else "afternoon",
        items=db.all("SELECT name, category, unit FROM products WHERE business_id=? ORDER BY name", (biz,)),
        return_to=url_for("plot_day", plot_id=plot_id, date=day),
    )


@app.post("/activities/plots/<int:plot_id>/log")
@need(None)
def log_activities(plot_id):
    """Saves every activity written in the plot's form at once. "Everyone on this plot" is one entry for each of its workers."""
    db, biz = get_db(), g.user["business_id"]
    plot = get_plot(db, biz, plot_id)
    day = day_or_none(text("date")) if text("date") else None
    back = redirect(url_for("plot_day", plot_id=plot_id, date=day or today_str()))
    if day is None or day > today_str():
        flash("Choose the day the work was done (today or earlier).", "error")
        return back
    if not plot["active"]:
        flash("This plot is no longer in use, so nothing more can be logged on it.", "error")
        return back
    if not plot["workers"]:
        flash("No workers are assigned to this plot yet. An admin can assign them from Daily Activities.", "error")
        return back
    names = {w["id"]: w["name"] for w in plot["workers"]}
    form = {key: request.form.getlist(key) for key in ("worker", "period", "description", "tools", "product", "product_qty", "product_unit", "work_done")}
    rows, problems = [], []
    for i in range(len(form["description"])):
        value = lambda key: " ".join((form[key][i] if i < len(form[key]) else "").split())  # noqa: E731
        if not any(value(key) for key in ("description", "tools", "product", "product_qty", "work_done")):
            continue   # a row left empty
        number_of = f"Activity {i + 1}"
        if not value("description"):
            problems.append(f"{number_of}: say what was done")
        worker = value("worker")
        if worker != "all" and not (worker.isdigit() and int(worker) in names):
            problems.append(f"{number_of}: choose a worker on this plot")
        if value("period") not in dict(ACTIVITY_PERIODS):
            problems.append(f"{number_of}: choose morning or afternoon")
        quantity = None
        if value("product_qty"):
            try:
                quantity = float(value("product_qty").replace(",", ""))
                if quantity < 0 or not math.isfinite(quantity):
                    raise ValueError
            except ValueError:
                problems.append(f"{number_of}: the quantity of product must be a number")
            if not value("product"):
                problems.append(f"{number_of}: say which product the quantity is of")
        rows.append({
            "workers": list(names.items()) if worker == "all" else [(int(worker), names.get(int(worker), ""))] if worker.isdigit() else [],
            "period": value("period"), "description": value("description")[:300], "tools": value("tools")[:200], "product": value("product")[:120],
            "qty": quantity, "unit": value("product_unit")[:30], "work_done": value("work_done")[:120],
        })
    if problems:
        flash("Nothing was saved. " + "; ".join(problems) + ".", "error")
        return back
    if not rows:
        flash("Write at least one activity.", "error")
        return back
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    saved = 0
    try:
        for r in rows:
            for employee_id, worker_name in r["workers"]:
                db.execute(
                    """INSERT INTO activities (business_id, plot_id, plot_name, department, work_date, employee_id, worker_name, period, description, tools,
                                               product, product_qty, product_unit, work_done, recorded_by, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (biz, plot_id, plot["name"], plot["department"], day, employee_id, worker_name, r["period"], r["description"], r["tools"],
                     r["product"], r["qty"], r["unit"], r["work_done"], g.user["username"], stamp),
                )
                saved += 1
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The activities could not be saved. Please try again.", "error")
        return back
    flash(f"Saved {len(rows)} activit{'y' if len(rows) == 1 else 'ies'} on {plot['name']} for {datetime.date.fromisoformat(day).strftime('%d %b %Y')}"
          + (f" ({saved} entries, one for each worker)." if saved != len(rows) else "."), "ok")
    return back


@app.post("/activities/<int:entry_id>/delete")
@need(None)
def delete_activity(entry_id):
    """Removes one logged activity: the person who logged it, or an admin."""
    db, biz = get_db(), g.user["business_id"]
    entry = db.one("SELECT id, recorded_by FROM activities WHERE id=? AND business_id=?", (entry_id, biz))
    if entry is None:
        abort(404)
    if g.user["role"] != "admin" and entry["recorded_by"] != g.user["username"]:
        flash("Only the person who logged an activity, or an admin, can remove it.", "error")
    else:
        db.execute("DELETE FROM activities WHERE id=? AND business_id=?", (entry_id, biz))
        db.commit()
        flash("Activity removed.", "ok")
    return activity_back(url_for("activities"))


def activity_sheets(rows):
    """A day's (or a plot's day's) activities as Excel sheets: every entry, and a summary by worker."""
    return [
        ("Activities",
         [("Date", TEXT), ("Worker", TEXT), ("Time", TEXT), ("Department", TEXT), ("Plot", TEXT), ("Activity", TEXT), ("Tools used", TEXT),
          ("Product used", TEXT), ("Quantity", NUMBER), ("Unit", TEXT), ("Work done", TEXT), ("Recorded by", TEXT)],
         [(r["work_date"], r["worker_name"], dict(ACTIVITY_PERIODS).get(r["period"], r["period"]), r["department"], r["plot_name"], r["description"],
           r["tools"], r["product"], r["product_qty"], r["product_unit"], r["work_done"], r["recorded_by"]) for r in rows]),
        ("By worker",
         [("Worker", TEXT), ("Activities", WHOLE), ("Morning", WHOLE), ("Afternoon", WHOLE), ("Plots", TEXT)],
         [(grp["worker"], len(grp["entries"]), grp["morning"], grp["afternoon"], ", ".join(grp["plots"])) for grp in by_worker(list(rows))]),
    ]


@app.get("/activities/report.xlsx")
@need(None)
def activities_report():
    db, biz = get_db(), g.user["business_id"]
    day = activity_day()
    plot = get_plot(db, biz, request.args.get("plot", type=int)) if request.args.get("plot") else None
    rows = [dict(r, work_date=day) for r in day_activities(db, biz, day, plot["id"] if plot else None)]
    title = f"Daily Activities {day}" + (f" - {plot['name']}" if plot else "")
    contents = {
        "title": f"{g.user['business_name']}: daily activities, {datetime.date.fromisoformat(day).strftime('%A %d %B %Y')}" + (f", {plot['name']}" if plot else ""),
        "facts": [("Day", day), ("Plot", f"{plot['department']} / {plot['name']}" if plot else "Every plot"), ("Activities", str(len(rows))),
                  ("Downloaded", now().strftime("%d %b %Y %H:%M")), ("By", g.user["username"])],
        "sheets": [("Daily activities", "Activities", len(rows))],
    }
    safe = re.sub(r'[\\/:*?"<>|]+', "-", title)
    return xlsx_response(f"{safe}.xlsx", workbook_bytes(activity_sheets(rows), contents))


def activities_book(db, biz):
    """Every logged activity, for the backups."""
    rows = db.all(
        """SELECT work_date, worker_name, period, department, plot_name, description, tools, product, product_qty, product_unit, work_done, recorded_by
           FROM activities WHERE business_id=? ORDER BY work_date, worker_name, id""",
        (biz,),
    )
    return activity_sheets(rows)[:1]


# --- BACKUPS (ADMIN) ---
# A backup is the business's records as Excel books - a master workbook holding every book, and each book on its own - and every
# table as a CSV file. backup.js saves them into a folder the admin chose once on their computer, replacing the files from the last
# backup (the file names never change). A browser that cannot write into a folder downloads everything as one ZIP instead.
# The Sales, Purchase, Planting and Inventory books come first everywhere, so they are always saved first.
BACKUP_HOUR_DEFAULT = 17
BACKUP_HOURS = [(-1, "Off: never remind me")] + [(h, f"{h:02d}:00") for h in range(12, 24)]
# Every table that holds this business's records, for the CSV copy. Users are written separately, without their passwords.
BACKUP_TABLES = (
    "invoices", "invoice_items", "payment_log", "deleted_invoices", "customers", "customer_aliases", "customer_distinct",
    "procurements", "suppliers", "supplier_aliases", "supplier_distinct", "supplier_payments", "supplier_payment_allocations",
    "products", "pos_products", "categories", "departments", "stock_adjustments", "plantings", "preorders", "seedling_batches",
    "cashbook", "money_balances", "employees", "payroll", "hr_settings", "plots", "plot_workers", "activities", "notifications", "permission_requests",
)
TEXT, MONEY, WHOLE, NUMBER = "text", "money", "whole", "number"


def joined_amounts(raw):
    """A payslip's bonuses or deductions, stored as JSON, as one readable line."""
    try:
        items = json.loads(raw or "{}")
    except ValueError:
        return ""
    return "; ".join(f"{name} {float(value):,.2f}" for name, value in items.items())


def sales_book(db, biz):
    paid_by = {}
    for r in db.all("SELECT source_id, method FROM cashbook WHERE business_id=? AND source='invoice' AND direction='in' AND amount > 0 ORDER BY id", (biz,)):
        methods = paid_by.setdefault(r["source_id"], [])
        if r["method"] not in (NOT_RECORDED, CORRECTION) and r["method"] not in methods:
            methods.append(r["method"])
    invoices = db.all(
        """SELECT id, invoice_no, created_at, kind, customer_name, customer_phone, total, amount_paid, balance_due, payment_status, created_by,
                  cancelled_at, cancelled_by, cancel_reason
           FROM invoices WHERE business_id=? ORDER BY created_at, id""",
        (biz,),
    )
    lines = db.all(
        """SELECT i.invoice_no, i.created_at, i.customer_name, i.payment_status, ii.item_code, ii.item_name, ii.department, ii.batch_no, ii.qty,
                  ii.unit_price, COALESCE(ii.discount, 0) AS discount, ii.line_total
           FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id WHERE ii.business_id=? ORDER BY i.created_at, i.id, ii.id""",
        (biz,),
    )
    deleted = db.all(
        """SELECT invoice_no, created_at, customer_name, total, amount_paid, reason, deleted_by, approved_by, deleted_at
           FROM deleted_invoices WHERE business_id=? ORDER BY id""",
        (biz,),
    )
    return [
        ("Sales invoices",
         [("Invoice No", TEXT), ("Date / time", TEXT), ("Type", TEXT), ("Customer", TEXT), ("Phone", TEXT), ("Total", MONEY), ("Paid", MONEY),
          ("Balance", MONEY), ("Status", TEXT), ("Paid by", TEXT), ("Recorded by", TEXT), ("Cancelled", TEXT)],
         [(i["invoice_no"], i["created_at"], "Farm use" if i["kind"] == "FARM_USE" else "Sale", i["customer_name"], i["customer_phone"], i["total"],
           i["amount_paid"], i["balance_due"], i["payment_status"], ", ".join(paid_by.get(i["id"], [])), i["created_by"],
           f"{i['cancelled_at']} by {i['cancelled_by']}: {i['cancel_reason']}" if i["cancelled_at"] else "") for i in invoices]),
        ("Sales lines",
         [("Invoice No", TEXT), ("Date / time", TEXT), ("Customer", TEXT), ("Item code", TEXT), ("Item", TEXT), ("Department", TEXT), ("Batch", TEXT),
          ("Qty", WHOLE), ("Unit price", MONEY), ("Discount", MONEY), ("Line total", MONEY), ("Invoice status", TEXT)],
         [(r["invoice_no"], r["created_at"], r["customer_name"], r["item_code"], r["item_name"], r["department"], r["batch_no"], r["qty"],
           r["unit_price"], r["discount"] or None, r["line_total"], r["payment_status"]) for r in lines]),
        ("Deleted invoices",
         [("Invoice No", TEXT), ("Sold on", TEXT), ("Customer", TEXT), ("Total", MONEY), ("Paid", MONEY), ("Reason", TEXT), ("Deleted by", TEXT),
          ("Approved by", TEXT), ("Deleted on", TEXT)],
         [(r["invoice_no"], r["created_at"], r["customer_name"], r["total"], r["amount_paid"], r["reason"], r["deleted_by"], r["approved_by"],
           r["deleted_at"]) for r in deleted]),
    ]


def purchase_book(db, biz):
    purchases = db.all(
        """SELECT purchase_date, supplier_name, invoice_no, item_code, name, category, department, COALESCE(in_inventory, 1) AS in_inventory, quantity,
                  unit_cost, total_cost, COALESCE(amount_paid, total_cost) AS paid, expiry_date
           FROM procurements WHERE business_id=? ORDER BY purchase_date, id""",
        (biz,),
    )
    payments = db.all(
        """SELECT sp.paid_on, s.name AS supplier, sp.method, sp.reference, sp.amount, sp.note, sp.recorded_by, sp.recorded_at
           FROM supplier_payments sp LEFT JOIN suppliers s ON s.id = sp.supplier_id WHERE sp.business_id=? ORDER BY sp.paid_on, sp.id""",
        (biz,),
    )
    return [
        ("Purchases",
         [("Date", TEXT), ("Supplier", TEXT), ("Supplier invoice", TEXT), ("Item code", TEXT), ("Item", TEXT), ("Category", TEXT), ("Department", TEXT),
          ("In inventory", TEXT), ("Qty", WHOLE), ("Unit cost", MONEY), ("Total cost", MONEY), ("Paid", MONEY), ("Balance", MONEY), ("Status", TEXT),
          ("Expiry", TEXT)],
         [(r["purchase_date"], r["supplier_name"], r["invoice_no"], r["item_code"], r["name"], r["category"], r["department"],
           "Yes" if r["in_inventory"] else "No", r["quantity"], r["unit_cost"], r["total_cost"], r["paid"],
           round((r["total_cost"] or 0) - (r["paid"] or 0), 2), payment_status_for(r["total_cost"] or 0, r["paid"] or 0), r["expiry_date"])
          for r in purchases]),
        ("Supplier payments",
         [("Date paid", TEXT), ("Supplier", TEXT), ("Method", TEXT), ("Reference", TEXT), ("Amount", MONEY), ("Note", TEXT), ("Recorded by", TEXT),
          ("Recorded at", TEXT)],
         [(r["paid_on"], r["supplier"], r["method"], r["reference"], r["amount"], r["note"], r["recorded_by"], r["recorded_at"]) for r in payments]),
    ]


def planting_book(db, biz):
    plantings = with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? ORDER BY planted_on, id", (biz,)))
    orders = db.all(
        """SELECT po.created_at, po.customer_name, po.customer_phone, p.name AS planting, po.qty, po.unit_price, po.status, po.collected_at,
                  po.approved_by, i.invoice_no
           FROM preorders po JOIN plantings p ON p.id = po.planting_id LEFT JOIN invoices i ON i.id = po.invoice_id
           WHERE po.business_id=? ORDER BY po.id""",
        (biz,),
    )
    sowings = db.all(
        """SELECT created_at, seed_code, seed_name, seedling_name, packs, seeds_sown, germination_pct, seedlings, cost_per_seedling, selling_price, created_by
           FROM seedling_batches WHERE business_id=? ORDER BY id""",
        (biz,),
    )
    trays = db.all(
        """SELECT p.planted_on, p.name AS planting, p.batch_no, t.employee_name, t.trays, e.employment_type, pr.pay_month
           FROM planting_trays t JOIN plantings p ON p.id = t.planting_id AND p.business_id = t.business_id
                LEFT JOIN employees e ON e.id = t.employee_id AND e.business_id = t.business_id
                LEFT JOIN payroll pr ON pr.id = t.paid_payslip_id AND pr.business_id = t.business_id
           WHERE t.business_id=? ORDER BY p.planted_on, t.id""",
        (biz,),
    )
    return [
        ("Planting records",
         [("Batch", TEXT), ("Batch No", TEXT), ("Seed", TEXT), ("Seed code", TEXT), ("Department", TEXT), ("Planted on", TEXT), ("Ready on", TEXT),
          ("Actually ready", TEXT), ("Seeds planted", WHOLE), ("Packs used", WHOLE), ("Mortality %", NUMBER), ("Seedlings", WHOLE),
          ("Cost per seedling", NUMBER), ("Price", MONEY), ("Sold", WHOLE), ("Pre-ordered", WHOLE), ("Available", WHOLE), ("In greenhouse", TEXT),
          ("POS code", TEXT), ("Notes", TEXT), ("Recorded by", TEXT), ("Trays planted", NUMBER), ("Holes per tray", WHOLE)],
         [(p["name"], p["batch_no"], p["seed_name"], p["seed_code"], p["department"], p["planted_on"], p["ready_on"], p["actual_ready_on"],
           p["seeds_planted"], p["packs_used"], p["mortality_pct"], p["seedlings"], p["cost_per_seedling"], p["price"], p["sold"], p["pending"],
           p["available"], "Yes" if p["still_in_greenhouse"] else "No (record)", p["pos_code"], p["notes"], p["created_by"], p["trays"],
           p["holes_per_tray"]) for p in plantings]),
        ("Trays planted by",
         [("Planted on", TEXT), ("Planting", TEXT), ("Batch No", TEXT), ("Employee", TEXT), ("Trays", NUMBER), ("Pay", TEXT)],
         [(r["planted_on"], r["planting"], r["batch_no"], r["employee_name"], r["trays"],
           f"Paid on the {r['pay_month']} payslip" if r["pay_month"] else ("Not paid yet" if r["employment_type"] == "Casual Worker" else "Salaried" if r["employment_type"] else "No longer an employee"))
          for r in trays]),
        ("Pre-orders",
         [("Placed", TEXT), ("Customer", TEXT), ("Phone", TEXT), ("Batch", TEXT), ("Qty", WHOLE), ("Unit price", MONEY), ("Total", MONEY),
          ("Status", TEXT), ("Collected", TEXT), ("Approved by", TEXT), ("Invoice No", TEXT)],
         [(r["created_at"], r["customer_name"], r["customer_phone"], r["planting"], r["qty"], r["unit_price"], round(r["qty"] * r["unit_price"], 2),
           r["status"], r["collected_at"], r["approved_by"], r["invoice_no"]) for r in orders]),
        ("Seed sowings",
         [("Sown", TEXT), ("Seed code", TEXT), ("Seed", TEXT), ("Seedlings listed as", TEXT), ("Packs", WHOLE), ("Seeds sown", WHOLE),
          ("Germination %", NUMBER), ("Seedlings", WHOLE), ("Cost per seedling", NUMBER), ("Selling price", MONEY), ("By", TEXT)],
         [(r["created_at"], r["seed_code"], r["seed_name"], r["seedling_name"], r["packs"], r["seeds_sown"], r["germination_pct"], r["seedlings"],
           r["cost_per_seedling"], r["selling_price"], r["created_by"]) for r in sowings]),
    ]


def inventory_book(db, biz):
    items = db.all(
        """SELECT item_code, name, category, department, unit, quantity, unit_cost, price, seeds_per_pack, supplier_name, invoice_no, purchase_date,
                  batch_no, date_planted, expiry_date
           FROM products WHERE business_id=? ORDER BY category, name""",
        (biz,),
    )
    listed = db.all(
        "SELECT item_code, name, category, department, date_planted, batch_no, quantity, price FROM pos_products WHERE business_id=? ORDER BY name",
        (biz,),
    )
    changes = db.all(
        """SELECT created_at, kind, item_code, item_name, department, quantity_before, quantity_after, delta, reason, note, created_by
           FROM stock_adjustments WHERE business_id=? ORDER BY id""",
        (biz,),
    )
    return [
        ("Inventory",
         [("Item code", TEXT), ("Item", TEXT), ("Category", TEXT), ("Department", TEXT), ("Unit", TEXT), ("In stock", WHOLE), ("Unit cost", MONEY),
          ("Stock value", MONEY), ("Selling price", MONEY), ("Seeds per pack", WHOLE), ("Supplier", TEXT), ("Supplier invoice", TEXT),
          ("Purchase date", TEXT), ("Batch", TEXT), ("Date planted", TEXT), ("Expiry", TEXT)],
         [(r["item_code"], r["name"], r["category"], r["department"], r["unit"], r["quantity"], r["unit_cost"],
           round((r["quantity"] or 0) * (r["unit_cost"] or 0), 2), r["price"], r["seeds_per_pack"], r["supplier_name"], r["invoice_no"],
           r["purchase_date"], r["batch_no"], r["date_planted"], r["expiry_date"]) for r in items]),
        ("POS listings",
         [("Code", TEXT), ("Name", TEXT), ("Category", TEXT), ("Department", TEXT), ("Planted", TEXT), ("Batch", TEXT), ("In stock", WHOLE),
          ("Price", MONEY), ("Value at price", MONEY)],
         [(r["item_code"], r["name"], r["category"], r["department"], r["date_planted"], r["batch_no"], r["quantity"], r["price"],
           round((r["quantity"] or 0) * (r["price"] or 0), 2)) for r in listed]),
        ("Stock adjustments",
         [("When", TEXT), ("Where", TEXT), ("Item code", TEXT), ("Item", TEXT), ("Department", TEXT), ("Before", WHOLE), ("After", WHOLE),
          ("Change", WHOLE), ("Reason", TEXT), ("Note", TEXT), ("By", TEXT)],
         [(r["created_at"], "POS" if r["kind"] == "pos_removal" else "Inventory", r["item_code"], r["item_name"], r["department"],
           r["quantity_before"], r["quantity_after"], r["delta"], r["reason"], r["note"], r["created_by"]) for r in changes]),
    ]


def accounts_book(db, biz):
    rows = db.all(
        """SELECT occurred_at, direction, category, party, description, method, reference, amount, recorded_by
           FROM cashbook WHERE business_id=? ORDER BY occurred_at, id""",
        (biz,),
    )
    return [(
        "Accounts entries",
        [("Date / time", TEXT), ("In / Out", TEXT), ("Category", TEXT), ("From / to", TEXT), ("Details", TEXT), ("Method", TEXT), ("Channel", TEXT),
         ("Reference", TEXT), ("Money in", MONEY), ("Money out", MONEY), ("Recorded by", TEXT)],
        [(r["occurred_at"], "In" if r["direction"] == "in" else "Out", CATEGORY_LABELS.get(r["category"], r["category"]), r["party"], r["description"],
          r["method"], CHANNEL_OF.get(r["method"], "Other"), r["reference"], r["amount"] if r["direction"] == "in" else None,
          r["amount"] if r["direction"] == "out" else None, r["recorded_by"]) for r in rows],
    ), (
        "Balance counts",
        [("Counted at", TEXT), ("Balance", TEXT), ("Counted", MONEY), ("The app expected", MONEY), ("Difference", MONEY), ("By", TEXT)],
        [(c["as_of"], dict(BALANCE_ACCOUNTS).get(c["account"], c["account"]), c["amount"], c["expected"],
          None if c["expected"] is None else round(c["amount"] - c["expected"], 2), c["set_by"])
         for c in db.all("SELECT account, amount, expected, as_of, set_by FROM money_balances WHERE business_id=? ORDER BY id", (biz,))],
    )]


def customers_book(db, biz):
    rows = db.all(
        """SELECT c.name, c.phone, c.created_at, COUNT(i.id) AS invoices, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.amount_paid), 0) AS paid,
                  COALESCE(SUM(i.balance_due), 0) AS owed
           FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id AND i.kind = 'SALE' AND i.payment_status <> 'Cancelled'
           WHERE c.business_id=? GROUP BY c.id, c.name, c.phone, c.created_at ORDER BY c.name""",
        (biz,),
    )
    return [(
        "Customers",
        [("Customer", TEXT), ("Phone", TEXT), ("Customer since", TEXT), ("Invoices", WHOLE), ("Total bought", MONEY), ("Paid", MONEY), ("Owes", MONEY)],
        [(r["name"], r["phone"], (r["created_at"] or "")[:10], r["invoices"], r["total"], r["paid"], r["owed"]) for r in rows],
    )]


def suppliers_book(db, biz):
    rows = db.all(
        """SELECT s.name, s.phone, s.created_at, COUNT(pr.id) AS purchases, COALESCE(SUM(pr.total_cost), 0) AS total,
                  COALESCE(SUM(COALESCE(pr.amount_paid, pr.total_cost)), 0) AS paid
           FROM suppliers s LEFT JOIN procurements pr ON pr.supplier_id = s.id
           WHERE s.business_id=? GROUP BY s.id, s.name, s.phone, s.created_at ORDER BY s.name""",
        (biz,),
    )
    return [(
        "Suppliers",
        [("Supplier", TEXT), ("Phone", TEXT), ("Supplier since", TEXT), ("Purchases", WHOLE), ("Total bought", MONEY), ("Paid", MONEY), ("We owe", MONEY)],
        [(r["name"], r["phone"], (r["created_at"] or "")[:10], r["purchases"], r["total"], r["paid"], round(r["total"] - r["paid"], 2)) for r in rows],
    )]


def payroll_book(db, biz):
    slips = db.all(
        """SELECT pay_month, employee_name, basic_salary, expected_days, worked_days, trays, tray_rate, calculated_basic, bonuses_json, deductions_json, napsa_amt, nhima_amt,
                  employer_napsa, employer_nhima, net_payable, created_at
           FROM payroll WHERE business_id=? ORDER BY pay_month, employee_name""",
        (biz,),
    )
    staff = db.all(
        "SELECT full_name, nrc_number, employment_type, basic_salary, start_date, expected_days_week, address FROM employees WHERE business_id=? ORDER BY full_name",
        (biz,),
    )
    return [
        ("Payslips",
         [("Pay month", TEXT), ("Employee", TEXT), ("Basic salary", MONEY), ("Expected days", NUMBER), ("Days worked", NUMBER), ("Trays planted", NUMBER),
          ("Pay per tray", MONEY), ("Basic for days or trays", MONEY),
          ("Bonuses", TEXT), ("Deductions", TEXT), ("NAPSA (employee)", MONEY), ("NHIMA (employee)", MONEY), ("NAPSA (employer)", MONEY),
          ("NHIMA (employer)", MONEY), ("Net pay", MONEY), ("Made", TEXT)],
         [(r["pay_month"], r["employee_name"], r["basic_salary"], r["expected_days"], r["worked_days"], r["trays"], r["tray_rate"], r["calculated_basic"],
           joined_amounts(r["bonuses_json"]), joined_amounts(r["deductions_json"]), r["napsa_amt"], r["nhima_amt"], r["employer_napsa"],
           r["employer_nhima"], r["net_payable"], r["created_at"]) for r in slips]),
        ("Employees",
         [("Employee", TEXT), ("NRC", TEXT), ("Type", TEXT), ("Basic salary", MONEY), ("Started", TEXT), ("Days a week", WHOLE), ("Address", TEXT)],
         [(r["full_name"], r["nrc_number"], r["employment_type"], r["basic_salary"], r["start_date"], r["expected_days_week"], r["address"]) for r in staff]),
    ]


# (key, file name, what builds it). The first four are the priority books.
BACKUP_BOOKS = (
    ("sales", "Sales Book", sales_book),
    ("purchases", "Purchase Book", purchase_book),
    ("plantings", "Planting Records", planting_book),
    ("inventory", "Inventory Records", inventory_book),
    ("accounts", "Accounts", accounts_book),
    ("customers", "Customers", customers_book),
    ("suppliers", "Suppliers", suppliers_book),
    ("payroll", "Payroll", payroll_book),
    ("activities", "Daily Activities", activities_book),
)
PRIORITY_BOOKS = 4


def workbook_bytes(sheets, contents=None):
    """An Excel workbook of sheets [(title, [(column, kind)], rows)], with a contents sheet first when given. Text is always written
    as text (never run as a formula); amounts are real numbers, so they can be added up in Excel."""
    buffer = io.BytesIO()
    book = xlsxwriter.Workbook(buffer, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False, "strings_to_numbers": False})
    head = book.add_format({"bold": True, "bg_color": "#DCE6F2", "border": 1, "text_wrap": True, "valign": "top"})
    kinds = {
        MONEY: book.add_format({"num_format": "#,##0.00"}), WHOLE: book.add_format({"num_format": "#,##0"}),
        NUMBER: book.add_format({"num_format": "#,##0.####"}),
    }
    if contents:
        sheet = book.add_worksheet("Contents")
        title = book.add_format({"bold": True, "font_size": 14})
        sheet.write_string(0, 0, contents["title"], title)
        row = 2
        for label, value in contents["facts"]:
            sheet.write_string(row, 0, label, head)
            sheet.write_string(row, 1, str(value))
            row += 1
        row += 1
        for c, name in enumerate(("Book", "Sheet", "Rows")):
            sheet.write_string(row, c, name, head)
        for book_title, sheet_title, count in contents["sheets"]:
            row += 1
            sheet.write_string(row, 0, book_title)
            sheet.write_string(row, 1, sheet_title)
            sheet.write_number(row, 2, count, kinds[WHOLE])
        sheet.set_column(0, 0, 26)
        sheet.set_column(1, 1, 34)
        sheet.set_column(2, 2, 10)
    for title, columns, rows in sheets:
        sheet = book.add_worksheet(title[:31])
        widths = [len(name) for name, _ in columns]
        for c, (name, _) in enumerate(columns):
            sheet.write_string(0, c, name, head)
        for r, row in enumerate(rows, start=1):
            for c, (value, (_, kind)) in enumerate(zip(row, columns)):
                if value is None or value == "":
                    continue
                if kind != TEXT and isinstance(value, numbers.Number) and not isinstance(value, bool):
                    sheet.write_number(r, c, float(value), kinds[kind])
                    widths[c] = max(widths[c], len(f"{float(value):,.2f}"))
                else:
                    value = str(value)
                    sheet.write_string(r, c, value)
                    widths[c] = max(widths[c], min(len(value), 60))
        for c, width in enumerate(widths):
            sheet.set_column(c, c, min(max(width + 2, 8), 62))
        sheet.freeze_panes(1, 0)
        if rows:
            sheet.autofilter(0, 0, len(rows), len(columns) - 1)
    book.close()
    return buffer.getvalue()


def backup_by():
    """Who made this backup: the admin signed in, or the nightly backup to Google Drive."""
    return g.get("backup_by") or g.user["username"]


def backup_facts():
    return [
        ("Business", g.user["business_name"]),
        ("Backed up", now().strftime("%d %b %Y %H:%M")),
        ("By", backup_by()),
    ]


def master_workbook(db, biz):
    sheets, listing = [], []
    for _, title, build in BACKUP_BOOKS:
        for sheet in build(db, biz):
            sheets.append(sheet)
            listing.append((title, sheet[0], len(sheet[2])))
    contents = {"title": f"{g.user['business_name']}: backup of all records", "facts": backup_facts(), "sheets": listing}
    return workbook_bytes(sheets, contents)


def book_workbook(db, biz, key):
    title, build = next(((t, b) for k, t, b in BACKUP_BOOKS if k == key), (None, None))
    if build is None:
        abort(404)
    sheets = build(db, biz)
    contents = {"title": f"{g.user['business_name']}: {title}", "facts": backup_facts(), "sheets": [(title, s[0], len(s[2])) for s in sheets]}
    return title, workbook_bytes(sheets, contents)


def table_csv(db, biz, table):
    """One table of this business's records as CSV. The table name comes from the list above, never from the request as such."""
    if table == "business":
        sql, params = "SELECT * FROM businesses WHERE id=?", (biz,)
    elif table == "users":
        sql, params = "SELECT id, username, role, can_sell, can_procure, can_transfer, can_delete FROM users WHERE business_id=? ORDER BY id", (biz,)
    elif table in BACKUP_TABLES:
        sql, params = f"SELECT * FROM {table} WHERE business_id=? ORDER BY 1", (biz,)
    else:
        abort(404)
    cursor = db.execute(sql + " LIMIT 0", params)
    columns = [d[0] for d in cursor.description]
    cursor.close()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in db.all(sql, params):
        writer.writerow([csv_safe(row[c]) for c in columns])
    return buffer.getvalue().encode("utf-8-sig")


def backup_tables():
    return list(BACKUP_TABLES) + ["business", "users"]


def backup_files():
    """Every file a backup holds, in the order they are saved: the priority books, the master workbook, the other books, then the data."""
    books = [{"path": f"Books/{title}.xlsx", "url": url_for("backup_book", key=key)} for key, title, _ in BACKUP_BOOKS]
    return (
        books[:PRIORITY_BOOKS] + [{"path": "Master Backup.xlsx", "url": url_for("backup_master")}] + books[PRIORITY_BOOKS:]
        + [{"path": f"Data (CSV)/{table}.csv", "url": url_for("backup_table", table=table)} for table in backup_tables()]
    )


XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx_response(name, data):
    return Response(
        data, mimetype=XLSX_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


def note_backup(db, biz):
    db.execute("UPDATE businesses SET last_backup_at=?, last_backup_by=? WHERE id=?", (now().strftime("%Y-%m-%d %H:%M:%S"), backup_by(), biz))
    db.commit()


@app.get("/backup")
@need("admin")
def backup_page():
    hour = g.user["backup_hour"]
    link = drive_link(get_db(), g.user["business_id"])
    return render_template(
        "backup.html", books=[(key, title) for key, title, _ in BACKUP_BOOKS], priority=PRIORITY_BOOKS, tables=backup_tables(),
        hours=BACKUP_HOURS, hour=BACKUP_HOUR_DEFAULT if hour is None else hour, file_count=len(backup_files()),
        drive_ready=gdrive.configured(), drive=link if link and link["refresh_token"] else None,
        drive_folder=gdrive.folder_link(link["folder_id"]) if link and link["folder_id"] else "", cron_ready=bool(os.environ.get("CRON_SECRET")),
        drive_redirect=drive_redirect_uri(),
    )


@app.get("/backup/files.json")
@need("admin")
def backup_manifest():
    return Response(json.dumps(backup_files()), mimetype="application/json", headers={"Cache-Control": "no-store"})


@app.get("/backup/master.xlsx")
@need("admin")
def backup_master():
    db, biz = get_db(), g.user["business_id"]
    return xlsx_response("Master Backup.xlsx", master_workbook(db, biz))


@app.get("/backup/books/<key>.xlsx")
@need("admin")
def backup_book(key):
    db, biz = get_db(), g.user["business_id"]
    title, data = book_workbook(db, biz, key)
    return xlsx_response(f"{title}.xlsx", data)


@app.get("/backup/data/<table>.csv")
@need("admin")
def backup_table(table):
    db, biz = get_db(), g.user["business_id"]
    return Response(
        table_csv(db, biz, table), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{table}.csv"', "Cache-Control": "no-store"},
    )


@app.get("/backup/all.zip")
@need("admin")
def backup_zip():
    """Everything in one ZIP, laid out like the backup folder, for a browser that cannot save into a folder."""
    db, biz = get_db(), g.user["business_id"]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for key, title, _ in BACKUP_BOOKS[:PRIORITY_BOOKS]:
            archive.writestr(f"Books/{title}.xlsx", book_workbook(db, biz, key)[1])
        archive.writestr("Master Backup.xlsx", master_workbook(db, biz))
        for key, title, _ in BACKUP_BOOKS[PRIORITY_BOOKS:]:
            archive.writestr(f"Books/{title}.xlsx", book_workbook(db, biz, key)[1])
        for table in backup_tables():
            archive.writestr(f"Data (CSV)/{table}.csv", table_csv(db, biz, table))
        archive.writestr("Backup info.txt", "\r\n".join(f"{label}: {value}" for label, value in backup_facts()) + "\r\n")
    note_backup(db, biz)
    stamp = now().strftime("%Y-%m-%d")
    return Response(
        buffer.getvalue(), mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="Backup {stamp}.zip"', "Cache-Control": "no-store"},
    )


@app.post("/backup/done")
@need("admin")
def backup_done():
    """backup.js reports a backup it finished saving into the folder, so the end-of-day reminder knows today is done."""
    db, biz = get_db(), g.user["business_id"]
    note_backup(db, biz)
    row = db.one("SELECT last_backup_at FROM businesses WHERE id=?", (biz,))
    return Response(json.dumps({"ok": True, "at": row["last_backup_at"]}), mimetype="application/json")


@app.post("/backup/settings")
@need("admin")
def backup_settings():
    db, biz = get_db(), g.user["business_id"]
    hour = request.form.get("backup_hour", type=int)
    if hour not in dict(BACKUP_HOURS):
        flash("Choose a time for the reminder from the list.", "error")
    else:
        db.execute("UPDATE businesses SET backup_hour=? WHERE id=?", (hour, biz))
        db.commit()
        flash("The end-of-day backup reminder is off." if hour < 0 else f"The backup reminder will appear from {hour:02d}:00 each day until that day's backup is made.", "ok")
    return redirect(url_for("backup_page"))


# --- BACKUPS TO GOOGLE DRIVE ---
# An admin connects the business's Google account once (gdrive.py has how). After that a backup saves the same files as the computer
# backup into a folder the app made in that Drive, replacing last time's (Drive keeps the earlier versions in each file's history).
# Back up now sends one file per request, so no request runs long; the nightly backup (vercel.json) does each business in one go.
DRIVE_SUBFOLDERS = ("Books", "Data (CSV)")
DRIVE_NIGHTLY_BY = "Nightly backup"
DRIVE_CRON_SECONDS = 240   # the nightly run stops starting new businesses after this; the rest go first the next night


def drive_link(db, biz):
    return db.one("SELECT * FROM drive_links WHERE business_id=?", (biz,))


def drive_redirect_uri():
    """Where Google sends the admin back to. It must be listed, exactly, on the OAuth client in Google Cloud."""
    if os.environ.get("GOOGLE_REDIRECT_URI"):
        return os.environ["GOOGLE_REDIRECT_URI"]
    if not has_request_context():
        return ""
    return url_for("drive_callback", _external=True, _scheme="https" if ON_VERCEL else request.scheme)


def backup_file(db, biz, path):
    """One file of a backup, by its path in the backup folder: its bytes and type."""
    if path == "Master Backup.xlsx":
        return master_workbook(db, biz), XLSX_TYPE
    if path.startswith("Books/") and path.endswith(".xlsx"):
        key = next((k for k, t, _ in BACKUP_BOOKS if f"Books/{t}.xlsx" == path), None)
        if key:
            return book_workbook(db, biz, key)[1], XLSX_TYPE
    if path.startswith("Data (CSV)/") and path.endswith(".csv") and path[11:-4] in backup_tables():
        return table_csv(db, biz, path[11:-4]), "text/csv"
    if path == "Backup info.txt":
        return ("\r\n".join(f"{label}: {value}" for label, value in backup_facts()) + "\r\n").encode("utf-8"), "text/plain"
    abort(404)


def drive_access(db, link):
    """An access token for the business's Drive, refreshed when the one kept has run out. A connection Google no longer accepts
    is forgotten, so the Backups page asks for it to be connected again."""
    if link["access_token"] and (link["access_until"] or 0) > gdrive.now_seconds() + 60:
        token = gdrive.unseal(app.config["SECRET_KEY"], link["access_token"])
        if token:
            return token
    refresh_token = gdrive.unseal(app.config["SECRET_KEY"], link["refresh_token"] or "")
    if not refresh_token:
        drive_forget(db, link["business_id"], "The app can no longer read its Google Drive connection. Connect Google Drive again.")
        raise gdrive.DriveError("Google Drive needs to be connected again.", gone=True)
    try:
        token, seconds = gdrive.refresh(refresh_token)
    except gdrive.DriveError as e:
        if e.gone:
            drive_forget(db, link["business_id"], "Google no longer lets the app into this Drive (access was removed or ran out). Connect Google Drive again.")
        raise
    db.execute("UPDATE drive_links SET access_token=?, access_until=? WHERE id=?",
               (gdrive.seal(app.config["SECRET_KEY"], token), gdrive.now_seconds() + seconds, link["id"]))
    db.commit()
    return token


def drive_forget(db, biz, why=""):
    """Drops the connection's tokens (the folder and the files saved stay in Drive), noting why for the Backups page."""
    db.execute("UPDATE drive_links SET refresh_token=NULL, access_token=NULL, access_until=NULL, last_error=?, last_error_at=? WHERE business_id=?",
               (why, now().strftime("%Y-%m-%d %H:%M:%S"), biz))
    db.commit()


def drive_known(db, biz, path):
    row = db.one("SELECT file_id FROM drive_files WHERE business_id=? AND path=?", (biz, path))
    return row["file_id"] if row else None


def drive_remember(db, biz, path, file_id):
    if db.execute("UPDATE drive_files SET file_id=? WHERE business_id=? AND path=?", (file_id, biz, path)).rowcount == 0:
        db.execute("INSERT INTO drive_files (business_id, path, file_id) VALUES (?, ?, ?)", (biz, path, file_id))
    db.commit()


def drive_check_folders(db, biz, link, access):
    """At the start of a backup: the backup folder and its subfolders are made again if they were deleted or binned in Drive."""
    folder = link["folder_id"]
    if not folder or not gdrive.usable(access, folder):
        folder = gdrive.make_folder(access, f"{g.user['business_name']} backups")
        db.execute("UPDATE drive_links SET folder_id=? WHERE id=?", (folder, link["id"]))
        db.execute("DELETE FROM drive_files WHERE business_id=?", (biz,))
        db.commit()
    for name in DRIVE_SUBFOLDERS:
        known = drive_known(db, biz, name + "/")
        if not known or not gdrive.usable(access, known):
            db.execute("DELETE FROM drive_files WHERE business_id=? AND path LIKE ?", (biz, name + "/%"))
            drive_remember(db, biz, name + "/", gdrive.make_folder(access, name, folder))
    return folder


def drive_save(db, biz, folder, access, path):
    data, mimetype = backup_file(db, biz, path)
    parent, name = (drive_known(db, biz, path.split("/")[0] + "/"), path.split("/", 1)[1]) if "/" in path else (folder, path)
    drive_remember(db, biz, path, gdrive.put_file(access, name, data, mimetype, parent or folder, drive_known(db, biz, path)))


def drive_finished(db, biz, files):
    note_backup(db, biz)
    db.execute("UPDATE drive_links SET last_at=?, last_by=?, last_files=?, last_error='', last_error_at=NULL WHERE business_id=?",
               (now().strftime("%Y-%m-%d %H:%M:%S"), backup_by(), files, biz))
    db.commit()


def drive_failed(db, biz, error):
    db.rollback()
    if getattr(error, "gone", False):
        return   # drive_forget has already noted why
    db.execute("UPDATE drive_links SET last_error=?, last_error_at=? WHERE business_id=?", (str(error)[:300], now().strftime("%Y-%m-%d %H:%M:%S"), biz))
    db.commit()


def drive_json(**data):
    return Response(json.dumps(data), mimetype="application/json", headers={"Cache-Control": "no-store"})


@app.post("/backup/drive/connect")
@need("admin")
def drive_connect():
    if not gdrive.configured():
        flash("Google Drive is not set up on this server yet (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are missing).", "error")
        return redirect(url_for("backup_page"))
    session["drive_state"] = secrets.token_urlsafe(24)
    return redirect(gdrive.auth_url(drive_redirect_uri(), session["drive_state"]))


@app.get("/backup/drive/callback")
@need("admin")
def drive_callback():
    expected, supplied = session.pop("drive_state", ""), request.args.get("state", "")
    if not expected or not hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
        flash("That Google sign-in did not start from this page. Click Connect Google Drive again.", "error")
        return redirect(url_for("backup_page"))
    if request.args.get("error") or not request.args.get("code"):
        flash("Google Drive was not connected: permission was not given." if request.args.get("error") == "access_denied"
              else "Google Drive was not connected. Try again.", "error")
        return redirect(url_for("backup_page"))
    db, biz = get_db(), g.user["business_id"]
    try:
        tokens, email = gdrive.exchange(request.args["code"], drive_redirect_uri())
        access = tokens["access_token"]
        old = drive_link(db, biz)
        folder = old["folder_id"] if old and old["folder_id"] and (old["email"] or "") == email else None
        if not folder or not gdrive.usable(access, folder):
            folder = gdrive.make_folder(access, f"{g.user['business_name']} backups")
            db.execute("DELETE FROM drive_files WHERE business_id=?", (biz,))
    except gdrive.DriveError as e:
        db.rollback()
        flash(f"Google Drive was not connected. {e}", "error")
        return redirect(url_for("backup_page"))
    key, stamp = app.config["SECRET_KEY"], now().strftime("%Y-%m-%d %H:%M:%S")
    values = (email, gdrive.seal(key, tokens["refresh_token"]), gdrive.seal(key, access), gdrive.now_seconds() + int(tokens.get("expires_in", 3600)),
              folder, g.user["username"], stamp)
    if old:
        db.execute("""UPDATE drive_links SET email=?, refresh_token=?, access_token=?, access_until=?, folder_id=?, connected_by=?, connected_at=?,
                      last_error='', last_error_at=NULL WHERE business_id=?""", values + (biz,))
    else:
        db.execute("""INSERT INTO drive_links (email, refresh_token, access_token, access_until, folder_id, connected_by, connected_at, business_id)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", values + (biz,))
    db.commit()
    flash(f"Google Drive is connected{' (' + email + ')' if email else ''}. Backups go to the folder “{g.user['business_name']} backups” there. "
          "Click Back up to Google Drive now to make the first one.", "ok")
    return redirect(url_for("backup_page"))


@app.post("/backup/drive/disconnect")
@need("admin")
def drive_disconnect():
    db, biz = get_db(), g.user["business_id"]
    link = drive_link(db, biz)
    if link and link["refresh_token"]:
        token = gdrive.unseal(app.config["SECRET_KEY"], link["refresh_token"])
        if token:
            gdrive.revoke(token)
    db.execute("DELETE FROM drive_links WHERE business_id=?", (biz,))
    db.execute("DELETE FROM drive_files WHERE business_id=?", (biz,))
    db.commit()
    flash("Google Drive is disconnected. The backups already saved there stay in that Drive.", "ok")
    return redirect(url_for("backup_page"))


@app.post("/backup/drive/settings")
@need("admin")
def drive_settings():
    db, biz = get_db(), g.user["business_id"]
    on = 1 if request.form.get("auto") else 0
    db.execute("UPDATE drive_links SET auto=? WHERE business_id=?", (on, biz))
    db.commit()
    flash("Each night, the day's backup is saved to Google Drive by itself." if on else "The nightly backup to Google Drive is off.", "ok")
    return redirect(url_for("backup_page"))


@app.post("/backup/drive/file")
@need("admin")
def drive_file():
    """backup.js saves a backup one file at a time: first=1 on the first, which checks the folders; done=1 after the last."""
    db, biz = get_db(), g.user["business_id"]
    link = drive_link(db, biz)
    if not link or not link["refresh_token"]:
        return drive_json(ok=False, gone=True, error="Google Drive is not connected. Connect it on the Backups page.")
    path = request.form.get("path", "")
    if not request.form.get("done") and path not in {f["path"] for f in backup_files()}:
        abort(404)
    try:
        access = drive_access(db, link)
        folder = drive_check_folders(db, biz, link, access) if request.form.get("first") else link["folder_id"]
        if request.form.get("done"):
            drive_save(db, biz, folder, access, "Backup info.txt")
            drive_finished(db, biz, request.form.get("files", type=int) or 0)
            return drive_json(ok=True, at=db.one("SELECT last_backup_at FROM businesses WHERE id=?", (biz,))["last_backup_at"],
                              folder=gdrive.folder_link(folder))
        drive_save(db, biz, folder, access, path)
    except gdrive.DriveError as e:
        drive_failed(db, biz, e)
        return drive_json(ok=False, gone=e.gone, error=str(e))
    except Exception:
        app.logger.exception("Saving %s to Google Drive failed", path)
        drive_failed(db, biz, f"{path} could not be made.")
        return drive_json(ok=False, gone=False, error=f"{path} could not be made. Try again; if it keeps failing, use Back up to this computer.")
    return drive_json(ok=True)


def drive_backup_all(db, biz):
    """Every file of a backup into the business's Drive, in one go (the nightly backup). Returns how many files."""
    link = drive_link(db, biz)
    access = drive_access(db, link)
    folder = drive_check_folders(db, biz, link, access)
    paths = [f["path"] for f in backup_files()] + ["Backup info.txt"]
    for path in paths:
        drive_save(db, biz, folder, access, path)
    drive_finished(db, biz, len(paths) - 1)
    return len(paths)


# Once a night (vercel.json) Vercel calls this with the CRON_SECRET it was given: each business with Google Drive connected and the
# nightly backup on gets that day's backup saved there, once. A failure is noted on the Backups page and raised as an alert for admins.
@app.get("/cron/drive-backups")
def cron_drive_backups():
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return jsonify({"error": "CRON_SECRET is not set on the server."}), 503
    if not hmac.compare_digest(request.headers.get("Authorization", "").encode("utf-8"), f"Bearer {secret}".encode("utf-8")):
        abort(401)
    if not gdrive.configured():
        return jsonify({"error": "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set on the server."}), 503
    db, today, started, done, failed = get_db(), today_str(), time.monotonic(), 0, 0
    rows = db.all(
        """SELECT d.business_id FROM drive_links d JOIN businesses b ON b.id = d.business_id
           WHERE d.auto=1 AND d.refresh_token IS NOT NULL AND b.demo_expires_at IS NULL ORDER BY COALESCE(d.auto_day, ''), d.business_id"""
    )
    for row in rows:
        if time.monotonic() - started > DRIVE_CRON_SECONDS:
            break
        biz = row["business_id"]
        # Taken first, so a second call the same night (Vercel can repeat one) does nothing twice
        if db.execute("UPDATE drive_links SET auto_day=? WHERE business_id=? AND (auto_day IS NULL OR auto_day <> ?)", (today, biz, today)).rowcount != 1:
            db.commit()
            continue
        db.commit()
        admin = db.one("SELECT id FROM users WHERE business_id=? AND role='admin' ORDER BY id LIMIT 1", (biz,))
        if not admin:
            continue
        g.user, g.backup_by = user_row(db, admin["id"]), DRIVE_NIGHTLY_BY
        try:
            drive_backup_all(db, biz)
            done += 1
        except Exception as e:   # one business's failure must not stop the others
            app.logger.exception("The nightly Drive backup of business %s failed", biz)
            failed += 1
            drive_failed(db, biz, e if isinstance(e, gdrive.DriveError) else "The backup could not be made.")
            notify(db, "backup", "The nightly backup to Google Drive failed",
                   (str(e) if isinstance(e, gdrive.DriveError) else "The backup could not be made.") + " Open Backups to see what to do.", url_for("backup_page"))
            db.commit()
        finally:
            g.pop("backup_by", None)
            g.user = None
    return jsonify({"backed_up": done, "failed": failed})


# --- RESET THE APP (ADMIN) ---
# A reset wipes the records the app has logged for this business. It keeps what makes the app usable
# and was set up rather than logged: user accounts (with their permissions and appearance), the business
# profile and its settings, departments, and the HR statutory rules. Each group is (what an admin reads
# on the confirmation page, the tables behind it); the first table of a group is the one that is counted.
RESET_GROUPS = (
    ("Inventory items", ("products",)),
    ("Items listed for sale in the POS", ("pos_products",)),
    ("Purchases", ("procurements",)),
    ("Invoices and farm-use notes", ("invoices", "invoice_items")),
    ("Payment changes", ("payment_log",)),
    ("Deleted invoice records", ("deleted_invoices",)),
    ("Customer accounts", ("customers", "customer_aliases", "customer_distinct")),
    ("Supplier accounts", ("suppliers", "supplier_aliases", "supplier_distinct")),
    ("Supplier payments", ("supplier_payments", "supplier_payment_allocations")),
    ("Accounts: money in and out, and the balances counted", ("cashbook", "money_balances")),
    ("Employees", ("employees",)),
    ("Daily activities, plots and who works on them", ("activities", "plots", "plot_workers")),
    ("Payslips", ("payroll",)),
    ("Sowing batches", ("seedling_batches",)),
    ("Planting records", ("plantings",)),
    ("Pre-orders", ("preorders",)),
    ("Stock adjustments and removals", ("stock_adjustments",)),
    ("Alerts", ("notifications",)),
    ("Permission requests", ("permission_requests",)),
)
RESET_KEPT = (
    "User accounts: usernames, passwords, roles, permissions and each user's appearance.",
    "Business details and settings: name, contact, currency, low-stock threshold, alert switches and WhatsApp number.",
    "Departments and inventory categories.",
    "HR settings: statutory rates and non-working days.",
)
RESET_PHRASE = "RESET ALL DATA"

# A narrower version of the same reset, scoped to inventory alone: items, their POS listings, and the greenhouse
# records that live on top of them. Purchase history (Procurements) and sales history are kept, since those are
# financial records rather than stock, and deleting an item elsewhere already keeps its own history for the same reason.
CLEAR_INVENTORY_GROUPS = (
    ("Inventory items", ("products",)),
    ("Items listed for sale in the POS", ("pos_products",)),
    ("Sowing batches", ("seedling_batches",)),
    ("Planting records", ("plantings",)),
    ("Pre-orders", ("preorders",)),
    ("Stock adjustments and removals", ("stock_adjustments",)),
)
CLEAR_INVENTORY_KEPT = (
    "Procurements (purchase history) and supplier statements.",
    "Sales history, invoices and customer statements.",
    "Categories and departments.",
)
CLEAR_INVENTORY_PHRASE = "CLEAR INVENTORY"


def reset_counts(db, biz):
    # Table names come from the constants above, never from the request
    return [(label, db.one(f"SELECT COUNT(*) AS n FROM {tables[0]} WHERE business_id=?", (biz,))["n"]) for label, tables in RESET_GROUPS]


@app.get("/settings/reset")
@need("admin")
def reset_page():
    counts = reset_counts(get_db(), g.user["business_id"])
    return render_template("reset.html", counts=counts, total=sum(n for _, n in counts), phrase=RESET_PHRASE, kept=RESET_KEPT)


@app.post("/settings/reset")
@need("admin")
def reset_app():
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("reset_page"))

    # Two deliberate steps: type the phrase, and prove it is really the admin by giving their own password
    if request.form.get("phrase", "").strip() != RESET_PHRASE:
        flash(f"Type {RESET_PHRASE} exactly to confirm. Nothing was deleted.", "error")
        return back
    me = db.one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
    if not me or not check_password_hash(me["password_hash"], request.form.get("password", "")):
        flash("That password is not correct. Nothing was deleted.", "error")
        return back

    # Everything is deleted in one transaction and only for this business: if any step fails, nothing is lost
    deleted = 0
    try:
        for label, tables in RESET_GROUPS:
            for position, table in enumerate(tables):
                removed = db.execute(f"DELETE FROM {table} WHERE business_id=?", (biz,)).rowcount
                if position == 0:
                    deleted += removed
        # The only trace left is who did it, shown to the admins
        notify(
            db, "reset", "The app was reset",
            f"{g.user['username']} reset the app and deleted {deleted:,} records. Accounts and settings were kept.",
            url_for("notifications"),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The reset could not be completed, so nothing was deleted. Please try again.", "error")
        return back

    flash(f"The app has been reset: {deleted:,} records were deleted. User accounts and settings were kept.", "ok")
    return redirect(url_for("settings"))


def clear_inventory_counts(db, biz):
    return [(label, db.one(f"SELECT COUNT(*) AS n FROM {tables[0]} WHERE business_id=?", (biz,))["n"]) for label, tables in CLEAR_INVENTORY_GROUPS]


@app.get("/inventory/clear")
@need("admin")
def clear_inventory_page():
    counts = clear_inventory_counts(get_db(), g.user["business_id"])
    return render_template("clear_inventory.html", counts=counts, total=sum(n for _, n in counts), phrase=CLEAR_INVENTORY_PHRASE, kept=CLEAR_INVENTORY_KEPT)


@app.post("/inventory/clear")
@need("admin")
def clear_inventory():
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("clear_inventory_page"))

    if request.form.get("phrase", "").strip() != CLEAR_INVENTORY_PHRASE:
        flash(f"Type {CLEAR_INVENTORY_PHRASE} exactly to confirm. Nothing was deleted.", "error")
        return back
    me = db.one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
    if not me or not check_password_hash(me["password_hash"], request.form.get("password", "")):
        flash("That password is not correct. Nothing was deleted.", "error")
        return back

    deleted = 0
    try:
        for label, tables in CLEAR_INVENTORY_GROUPS:
            for position, table in enumerate(tables):
                removed = db.execute(f"DELETE FROM {table} WHERE business_id=?", (biz,)).rowcount
                if position == 0:
                    deleted += removed
        notify(
            db, "reset", "Inventory was cleared",
            f"{g.user['username']} cleared inventory and deleted {deleted:,} record(s). Purchase and sales history were kept.",
            url_for("notifications"),
        )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("Inventory could not be cleared, so nothing was deleted. Please try again.", "error")
        return back

    flash(f"Inventory was cleared: {deleted:,} record(s) were deleted. Purchase and sales history were kept.", "ok")
    return redirect(url_for("inventory"))


# --- SETTINGS (ADMIN) ---
@app.route("/settings", methods=["GET", "POST"])
@need("admin")
def settings():
    db, biz = get_db(), g.user["business_id"]
    if request.method == "POST":
        name = text("name")
        try:
            threshold = number("low_stock_threshold", int)
        except ValueError:
            threshold = -1
        if not name or threshold < 0:
            flash("Business name is required and the low-stock threshold must be a whole number.", "error")
        else:
            # The currency keeps its trailing space ("ZMW ") so amounts read "ZMW 10.00"
            currency = request.form.get("currency", "")[:8]
            db.execute(
                "UPDATE businesses SET name=?, subtitle=?, contact=?, currency=?, low_stock_threshold=? WHERE id=?",
                (name, text("subtitle"), text("contact"), currency, threshold, biz),
            )
            db.commit()
            flash("Business details saved.", "ok")
        return redirect(url_for("settings"))

    departments = db.all("SELECT id, name FROM departments WHERE business_id=? ORDER BY name", (biz,))
    return render_template(
        "settings.html", departments=departments, whatsapp=whatsapp_service(),
        assistant_info={"configured": assistant.configured(), "usage": assistant.usage_today(db, g.user), "limits": assistant.limits(g.user), "model": assistant.MODEL,
                        "costs": assistant.cost_report(db, g.user)},
        assistant_memory=assistant.memory_of(db, g.user), brief_hour=assistant.BRIEF_HOUR, cron_ready=bool(os.environ.get("CRON_SECRET")),
        reminder_options=REMINDER_EVERY, reminder_every=reminder_every(g.user), reminder_stale=reminder_stale_days(g.user),
    )


@app.post("/settings/departments")
@need("admin")
def add_department():
    db, biz = get_db(), g.user["business_id"]
    name = text("name")
    if not name:
        flash("Enter a department name.", "error")
    elif name in department_names(db, biz):
        flash("That department already exists.", "error")
    else:
        db.execute("INSERT INTO departments (business_id, name) VALUES (?, ?)", (biz, name))
        db.commit()
        flash(f"Department '{name}' created.", "ok")
    return redirect(url_for("settings"))


@app.post("/settings/departments/<int:dept_id>/delete")
@need("admin")
def delete_department(dept_id):
    db, biz = get_db(), g.user["business_id"]
    dept = db.one("SELECT name FROM departments WHERE id=? AND business_id=?", (dept_id, biz))
    if dept is None or dept["name"] == "General":
        flash("That department cannot be deleted.", "error")
        return redirect(url_for("settings"))

    db.execute("DELETE FROM departments WHERE id=? AND business_id=?", (dept_id, biz))
    db.execute("UPDATE products SET department='General' WHERE business_id=? AND department=?", (biz, dept["name"]))
    db.execute("UPDATE pos_products SET department='General' WHERE business_id=? AND department=?", (biz, dept["name"]))
    db.commit()
    flash(f"Department '{dept['name']}' deleted. Its items moved to 'General'.", "ok")
    return redirect(url_for("settings"))


@app.post("/settings/departments/<int:dept_id>/rename")
@need("admin")
def rename_department(dept_id):
    db, biz = get_db(), g.user["business_id"]
    row = db.one("SELECT id, name FROM departments WHERE id=? AND business_id=?", (dept_id, biz))
    if row is None:
        abort(404)
    new = clean_name(text("name"))
    clash = next((n for n in department_names(db, biz) if norm(n) == norm(new) and n != row["name"]), None)
    if not new:
        flash("Type the new name.", "error")
    elif row["name"] == "General":
        flash("The General department cannot be renamed.", "error")
    elif clash:
        flash(f"There is already a department called '{clash}'.", "error")
    else:
        # Items, purchases, POS listings, past sales and plantings all carry the department by name, so all of them follow
        try:
            db.execute("UPDATE departments SET name=? WHERE id=? AND business_id=?", (new, dept_id, biz))
            for table in ("products", "pos_products", "procurements", "invoice_items", "plantings"):
                retag(db, table, biz, row["name"], new, column="department")
            db.commit()
        except DB_ERRORS:
            db.rollback()
            flash("The department could not be renamed. Please try again.", "error")
        else:
            flash(f"'{row['name']}' is now '{new}'. Its items and past records moved with it.", "ok")
    return redirect(url_for("settings"))


@app.post("/settings/departments/merge")
@need("admin")
def merge_departments():
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("settings"))
    rows = db.all("SELECT id, name FROM departments WHERE business_id=? ORDER BY name", (biz,))
    keep, others, final, problem = plan_merge(rows, read_ids(), text("department_name"), "department")
    if problem:
        flash(problem, "error")
        return back

    # Items, purchases, POS listings and past sales all carry the department by name, so all of them move
    tables = ("products", "pos_products", "procurements", "invoice_items", "plantings")
    try:
        for r in others:
            for table in tables:
                retag(db, table, biz, r["name"], final, column="department")
            db.execute("DELETE FROM departments WHERE id=? AND business_id=?", (r["id"], biz))
        if keep["name"] != final:
            db.execute("UPDATE departments SET name=? WHERE id=? AND business_id=?", (final, keep["id"], biz))
            for table in tables:
                retag(db, table, biz, keep["name"], final, column="department")
        items = db.one("SELECT COUNT(*) AS n FROM products WHERE business_id=? AND department=?", (biz, final))["n"]
        sales = db.one("SELECT COUNT(*) AS n FROM invoice_items WHERE business_id=? AND department=?", (biz, final))["n"]
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The merge could not be completed, so nothing was changed. Please try again.", "error")
        return back

    names = ", ".join(f"'{r['name']}'" for r in [keep] + others)
    flash(f"Merged {names} into '{final}'. It now holds {items} item(s) and {sales} sale line(s); the others were removed.", "ok")
    return back


# --- OPERATORS & ADMINS (ADMIN) ---
@app.get("/operators")
@need("admin")
def operators():
    db, biz = get_db(), g.user["business_id"]
    rows = db.all(
        "SELECT id, username, can_sell, can_procure, can_transfer, can_delete FROM users WHERE business_id=? AND role='operator' ORDER BY username",
        (biz,),
    )
    editing = None
    edit_id = request.args.get("edit", type=int)
    if edit_id:
        editing = next((row for row in rows if row["id"] == edit_id), None)

    admins = db.all("SELECT id, username FROM users WHERE business_id=? AND role='admin' ORDER BY username", (biz,))
    editing_admin = None
    edit_admin_id = request.args.get("edit_admin", type=int)
    if edit_admin_id:
        editing_admin = next((row for row in admins if row["id"] == edit_admin_id), None)
    return render_template(
        "operators.html", operators=rows, editing=editing, admins=admins, editing_admin=editing_admin, admin_count=len(admins),
    )


@app.post("/operators/save")
@need("admin")
def save_operator():
    db, biz = get_db(), g.user["business_id"]
    op_id = request.form.get("id", type=int)
    username, password = text("username"), request.form.get("password", "")
    flags = [1 if request.form.get("can_" + perm) else 0 for perm in PERMISSIONS]
    back = redirect(url_for("operators", edit=op_id) if op_id else url_for("operators"))

    if not username:
        flash("Username is required.", "error")
        return back
    if password and len(password) < 8:
        flash("Choose a password of at least 8 characters.", "error")
        return back
    if not op_id and not password:
        flash("A new operator needs a password.", "error")
        return back
    taken = db.one("SELECT id FROM users WHERE username=?", (username,))
    if taken and taken["id"] != op_id:
        flash("That username is already taken.", "error")
        return back

    try:
        if op_id:
            db.execute(
                "UPDATE users SET username=?, can_sell=?, can_procure=?, can_transfer=?, can_delete=? WHERE id=? AND business_id=? AND role='operator'",
                (username, *flags, op_id, biz),
            )
            if password:
                db.execute(
                    "UPDATE users SET password_hash=? WHERE id=? AND business_id=? AND role='operator'",
                    (generate_password_hash(password), op_id, biz),
                )
        else:
            db.execute(
                """INSERT INTO users (business_id, username, password_hash, role, can_sell, can_procure, can_transfer, can_delete)
                   VALUES (?, ?, ?, 'operator', ?, ?, ?, ?)""",
                (biz, username, generate_password_hash(password), *flags),
            )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The operator could not be saved. Please try again.", "error")
        return back

    flash(f"Operator '{username}' saved.", "ok")
    return redirect(url_for("operators"))


@app.post("/operators/<int:op_id>/delete")
@need("admin")
def delete_operator(op_id):
    db = get_db()
    db.execute("DELETE FROM users WHERE id=? AND business_id=? AND role='operator'", (op_id, g.user["business_id"]))
    assistant.forget(db, g.user["business_id"], op_id)
    db.commit()
    flash("Operator account deleted.", "ok")
    return redirect(url_for("operators"))


@app.post("/admins/save")
@need("admin")
def save_admin():
    """Any admin can create another admin, or rename one / reset their password. Admins have every permission, so there are no flags to set."""
    db, biz = get_db(), g.user["business_id"]
    admin_id = request.form.get("id", type=int)
    username, password = text("username"), request.form.get("password", "")
    back = redirect(url_for("operators", edit_admin=admin_id) if admin_id else url_for("operators"))

    if not username:
        flash("Username is required.", "error")
        return back
    if password and len(password) < 8:
        flash("Choose a password of at least 8 characters.", "error")
        return back
    if not admin_id and not password:
        flash("A new admin needs a password.", "error")
        return back
    taken = db.one("SELECT id FROM users WHERE username=?", (username,))
    if taken and taken["id"] != admin_id:
        flash("That username is already taken.", "error")
        return back

    try:
        if admin_id:
            db.execute("UPDATE users SET username=? WHERE id=? AND business_id=? AND role='admin'", (username, admin_id, biz))
            if password:
                db.execute("UPDATE users SET password_hash=? WHERE id=? AND business_id=? AND role='admin'", (generate_password_hash(password), admin_id, biz))
        else:
            db.execute(
                """INSERT INTO users (business_id, username, password_hash, role, can_sell, can_procure, can_transfer, can_delete)
                   VALUES (?, ?, ?, 'admin', 1, 1, 1, 1)""",
                (biz, username, generate_password_hash(password)),
            )
        db.commit()
    except DB_ERRORS:
        db.rollback()
        flash("The admin account could not be saved. Please try again.", "error")
        return back

    flash(f"Admin '{username}' saved. They can see and manage every operator and admin for {g.user['business_name']}." if not admin_id else f"Admin '{username}' saved.", "ok")
    return redirect(url_for("operators"))


@app.post("/admins/<int:admin_id>/delete")
@need("admin")
def delete_admin(admin_id):
    db, biz = get_db(), g.user["business_id"]
    back = redirect(url_for("operators"))
    if admin_id == g.user["id"]:
        flash("You cannot delete your own admin account while signed in as them. Ask another admin, or create one first.", "error")
        return back
    target = db.one("SELECT username FROM users WHERE id=? AND business_id=? AND role='admin'", (admin_id, biz))
    if target is None:
        flash("That admin account could not be found.", "error")
        return back
    admin_count = db.one("SELECT COUNT(*) AS n FROM users WHERE business_id=? AND role='admin'", (biz,))["n"]
    if admin_count <= 1:
        flash("This is the only admin account, so it cannot be deleted. Create another admin first.", "error")
        return back
    db.execute("DELETE FROM users WHERE id=? AND business_id=? AND role='admin'", (admin_id, biz))
    assistant.forget(db, biz, admin_id)
    db.commit()
    flash(f"Admin '{target['username']}' deleted.", "ok")
    return redirect(url_for("operators"))


# --- ASSISTANT ---
# The chat panel (assistant.js) and its voice. The work is in assistant.py; these only check who is asking and answer as JSON. Every request
# is a form post (so the app's CSRF check covers it) and every one is limited to the signed-in person's own business.
def assistant_json(work):
    try:
        return jsonify(work())
    except assistant.Refused as e:
        return jsonify({"error": e.message}), e.status


@app.get("/assistant/state")
@need(None)
def assistant_state():
    if not assistant.enabled_for(g.user):
        return jsonify({"enabled": False, "can_enable": g.user["role"] == "admin"})
    return jsonify(dict(assistant.state(get_db(), g.user), enabled=True))


@app.post("/assistant/chat")
@need(None)
def assistant_chat():
    # A photo (a receipt, a tray tally) or a PDF can come with the question; the assistant reads it and nothing of it is kept
    upload = request.files.get("attachment")
    attachment = upload.read(assistant.MAX_ATTACHMENT_BYTES + 1) if upload and upload.filename else None
    return assistant_json(lambda: assistant.converse(
        get_db(), g.user, request.form.get("message", ""), voice=request.form.get("voice") == "1", attachment=attachment,
        handsfree=request.form.get("handsfree") == "1",
    ))


@app.post("/assistant/listen")
@need(None)
def assistant_listen():
    def work():
        recording = request.files.get("audio")
        data = recording.read(assistant.MAX_AUDIO_BYTES + 1) if recording else b""
        return {"text": assistant.listen(get_db(), g.user, data)}
    return assistant_json(work)


@app.post("/assistant/speak")
@need(None)
def assistant_speak():
    try:
        wav = assistant.speak(get_db(), g.user, request.form.get("text", ""), voice=request.form.get("voice") or None)
    except assistant.Refused as e:
        return jsonify({"error": e.message}), e.status
    return Response(wav, mimetype="audio/wav", headers={"Cache-Control": "no-store"})


@app.post("/assistant/actions/<int:action_id>/confirm")
@need(None)
def assistant_confirm(action_id):
    return assistant_json(lambda: assistant.confirm(get_db(), g.user, action_id))


@app.post("/assistant/actions/<int:action_id>/cancel")
@need(None)
def assistant_cancel(action_id):
    return assistant_json(lambda: assistant.cancel(get_db(), g.user, action_id))


@app.post("/assistant/clear")
@need(None)
def assistant_clear():
    assistant.clear(get_db(), g.user)
    return jsonify({"ok": True})


@app.post("/assistant/brief")
@need(None)
def assistant_brief():
    return assistant_json(lambda: assistant.give_brief(get_db(), g.user))


@app.post("/assistant/reminders")
@need(None)
def assistant_reminders():
    return assistant_json(lambda: assistant.give_reminders(get_db(), g.user))


@app.get("/assistant/memory")
@need(None)
def assistant_memory():
    return assistant_json(lambda: assistant.memory_of(get_db(), g.user))


def memory_reply(work, done):
    """The panel asks for JSON; the Settings page posts a form and goes back to Settings."""
    if request.headers.get("Accept", "").startswith("application/json"):
        return assistant_json(work)
    try:
        work()
        flash(done, "ok")
    except assistant.Refused as e:
        flash(e.message, "error")
    return redirect(url_for("settings") + "#assistant")


@app.post("/assistant/memory/<int:memory_id>/delete")
@need(None)
def assistant_forget_note(memory_id):
    return memory_reply(lambda: assistant.delete_memory(get_db(), g.user, memory_id), "The assistant has forgotten it.")


@app.post("/assistant/stock-preferences/<int:pref_id>/delete")
@need(None)
def assistant_forget_stock(pref_id):
    return memory_reply(lambda: assistant.delete_stock_preference(get_db(), g.user, pref_id), "That restock setting was removed.")


@app.post("/assistant/my-brief")
@need(None)
def assistant_my_brief():
    db = get_db()
    on = request.form.get("on") == "1"
    db.execute("UPDATE users SET evening_brief=? WHERE id=?", (None if on else 0, g.user["id"]))
    db.commit()
    return jsonify({"ok": True, "on": on})


@app.post("/assistant/voice")
@need(None)
def assistant_voice():
    return assistant_json(lambda: assistant.set_voice(get_db(), g.user, request.form.get("voice", "")))


@app.post("/settings/assistant/phrases")
@need("admin")
def save_assistant_phrases():
    db = get_db()
    wake, problem = assistant.phrase_check(request.form.get("wake_phrase", ""), "wake")
    stop, problem_stop = assistant.phrase_check(request.form.get("stop_phrase", ""), "stop")
    if problem or problem_stop:
        flash(problem or problem_stop, "error")
    elif wake.lower() == stop.lower():
        flash("The wake phrase and the stop phrase must be different.", "error")
    else:
        db.execute("UPDATE businesses SET wake_phrase=?, stop_phrase=? WHERE id=?",
                   (None if wake == assistant.DEFAULT_WAKE else wake, None if stop == assistant.DEFAULT_STOP else stop, g.user["business_id"]))
        db.commit()
        flash(f"Saved: say \u201c{wake}\u201d to ask the assistant something while it listens, and \u201c{stop}\u201d to switch its listening off.", "ok")
    return redirect(url_for("settings") + "#assistant")


@app.post("/settings/assistant/brief")
@need("admin")
def save_assistant_brief():
    db = get_db()
    try:
        keep = number("stock_cover_days", int, 30)
    except ValueError:
        keep = 0
    if not 1 <= keep <= 365:
        flash("Days of stock to keep must be a whole number from 1 to 365.", "error")
        return redirect(url_for("settings") + "#assistant")
    db.execute(
        "UPDATE businesses SET evening_brief=?, brief_whatsapp=?, stock_cover_days=? WHERE id=?",
        (1 if request.form.get("evening_brief") else 0, 1 if request.form.get("brief_whatsapp") else 0, keep, g.user["business_id"]),
    )
    db.commit()
    flash("Assistant settings saved.", "ok")
    return redirect(url_for("settings") + "#assistant")


# Once a day (vercel.json, at about the brief hour) Vercel calls this with the CRON_SECRET it was given. Each business with the assistant and
# the evening brief on gets, once that day: an alert for each person that their brief is waiting in the panel, and the app's short version
# of it by WhatsApp to the alert number in Settings (unless that copy is switched off).
@app.get("/cron/evening-brief")
def cron_evening_brief():
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return jsonify({"error": "CRON_SECRET is not set on the server."}), 503
    if not hmac.compare_digest(request.headers.get("Authorization", "").encode("utf-8"), f"Bearer {secret}".encode("utf-8")):
        abort(401)
    db, today, stamp, done = get_db(), today_str(), now().strftime("%Y-%m-%d %H:%M:%S"), 0
    for b in db.all("SELECT id, name, alert_phone, brief_whatsapp FROM businesses WHERE assistant_enabled=1 AND demo_expires_at IS NULL AND COALESCE(evening_brief, 1) <> 0"):
        # Taken first, so a second call the same day (Vercel can repeat one) sends nothing twice
        if db.execute("UPDATE businesses SET brief_sent_on=? WHERE id=? AND (brief_sent_on IS NULL OR brief_sent_on <> ?)", (today, b["id"], today)).rowcount != 1:
            db.commit()
            continue
        db.commit()
        users = [u for u in (user_row(db, r["id"]) for r in db.all("SELECT id FROM users WHERE business_id=? ORDER BY id", (b["id"],))) if u and assistant.brief_on(u)]
        if not users:
            continue
        sent = {}
        for u in users:
            sent[u["id"]] = db.insert(
                "INSERT INTO notifications (business_id, for_user_id, kind, title, body, link, created_at) VALUES (?, ?, 'brief', ?, ?, ?, ?)",
                (b["id"], u["id"], "Your evening brief", "What is due tomorrow, and what to restock or watch out for. Open it to read it.",
                 url_for("pos", assistant="brief"), stamp),
            )
            g.setdefault("push_outbox", []).append((sent[u["id"]], b["id"], u["id"], "Your evening brief",
                                                    "What is due tomorrow, and what to restock or watch out for. Open it to read it."))
        db.commit()
        done += 1
        admin = next((u for u in users if u["role"] == "admin"), None)
        if b["brief_whatsapp"] == 0 or not b["alert_phone"] or admin is None:
            continue
        try:
            text = assistant.brief_text(db, admin, one_line=True)
        except Exception:
            app.logger.exception("The evening brief for business %s could not be written", b["id"])
            db.rollback()
            continue
        status, detail = send_whatsapp(b["alert_phone"], f"{b['name']}: {text}")
        db.execute("UPDATE notifications SET wa_status=?, wa_error=? WHERE id=?", (status, detail, sent[admin["id"]]))
        db.commit()
    return jsonify({"businesses": done})


@app.post("/settings/assistant")
@need("admin")
def save_assistant():
    db = get_db()
    on = 1 if request.form.get("assistant_enabled") else 0
    db.execute("UPDATE businesses SET assistant_enabled=? WHERE id=?", (on, g.user["business_id"]))
    db.commit()
    flash("The assistant is on: everyone with a login can ask it, within what their account may see." if on else "The assistant is switched off.", "ok")
    return redirect(url_for("settings"))


assistant.bind(
    app=app, url_for=url_for, now=now, today_str=today_str, add_days=add_days, day_or_none=day_or_none, with_figures=with_figures, sold_url=sold_url,
    read_discount=read_discount, method_from_text=method_from_text, METHOD_NAMES=METHOD_NAMES, PAYMENT_STATUSES=PAYMENT_STATUSES,
    financial_standing=financial_standing, account_summary=account_summary, hr_settings_for=hr_settings_for,
    # what the prepare tools for purchases, stock transfers, plantings, tray pay, supplier payments and money entries check against
    is_seed_category=is_seed_category, department_names=department_names, seed_code_for=seed_code_for, phone_key=phone_key, name_key=name_key,
    planting_staff=planting_staff, match_person=match_person, unpaid_trays=unpaid_trays, tray_count=tray_count, seedlings_from=seedlings_from,
    planting_label=planting_label, MANUAL_IN=MANUAL_IN, MANUAL_OUT=MANUAL_OUT, CATEGORY_LABELS=CATEGORY_LABELS, MAX_TRAY_HOLES=MAX_TRAY_HOLES,
    RELEASE_DAYS=RELEASE_DAYS,
    # customer requests: what was asked for, matched to what we have
    request_fits=request_fits, requests_with_stock=requests_with_stock, known_item=known_item, most_requested=most_requested, request_label=request_label,
    customer_group_key=customer_group_key,
    # the reminder report
    reminder_report=reminder_report, reminder_lines=reminder_lines, reminder_stale_days=reminder_stale_days, REMINDER_ORDER_DAYS=REMINDER_ORDER_DAYS,
    # pay: each employee's rate for a tray and what their next payslip would pay
    rate_for=rate_for, pay_preview=pay_preview, schedule_words=schedule_words, EMPLOYEE_COLUMNS=EMPLOYEE_COLUMNS,
)


# --- START-UP ---
# The tables and their migrations, then the one-off repairs of records from before each feature existed. All of it runs only
# when the code doing it has changed since it last ran against this database (see startup_done in db.py), so an ordinary cold
# start costs one query here. Seed units are checked every time, as an item can still be moved into a seed category.
STARTUP_REPAIRS = (sync_categories, link_existing_customers, link_existing_suppliers, backfill_cashbook)


def start_up():
    signature = startup_signature(STARTUP_REPAIRS)
    if not startup_done(signature):
        init_schema()
        for repair in STARTUP_REPAIRS:
            repair()
        mark_startup_done(signature)
    backfill_units()


start_up()


if __name__ == "__main__":
    app.run(port=5000)
