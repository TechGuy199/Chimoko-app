import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import csv
import calendar

# Try importing OpenCV and pyzbar safely for offline camera barcode scanning
SCANNER_AVAILABLE = False
try:
    import cv2
    from pyzbar import pyzbar
    pyzbar.decode
    SCANNER_AVAILABLE = True
except Exception:
    SCANNER_AVAILABLE = False

# Try importing reportlab for PDF creation
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CSS_FILE = os.path.join(BASE_DIR, "styles.css")

DEFAULT_CONFIG = {
    "app_settings": {"title": "Beaver Passion & Skill", "currency": "ZMW ", "low_stock_threshold": 10},
    "database": {"db_name": "pos_system.db"},
    "labels": {
        "pos_tab": "🛒 Sales & POS",
        "inventory_tab": "📋 Inventory",
        "available_products_header": "Available Products"
    },
    "receipt_header": {
        "company_name": "BEAVER PASSION & SKILL",
        "subtitle": "Official Sales Invoice",
        "contact": ""
    }
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        return cfg

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    for section, values in loaded.items():
        if isinstance(values, dict) and isinstance(cfg.get(section), dict):
            cfg[section].update(values)
        else:
            cfg[section] = values
    return cfg


config = load_config()

_db_name = config["database"]["db_name"]
DB_PATH = _db_name if os.path.isabs(_db_name) else os.path.join(BASE_DIR, _db_name)


def parse_css_properties(css_file):
    styles = {}
    if not os.path.exists(css_file):
        return styles

    with open(css_file, "r", encoding="utf-8") as f:
        content = f.read()

    content = re.sub(r"/\*.*?\*/", "", content, flags=re.S)
    blocks = re.findall(r"([^{]+)\{([^}]+)\}", content)
    for selector, props in blocks:
        selector = selector.strip()
        prop_dict = {}
        for line in props.split(";"):
            if ":" in line:
                key, val = line.split(":", 1)
                prop_dict[key.strip()] = val.strip()
        styles[selector] = prop_dict
    return styles


# --- THEME (values come from the :root block of styles.css; the defaults apply if a value is missing or invalid) ---
_css_vars = parse_css_properties(CSS_FILE).get(":root", {})


def css_color(name, default):
    value = _css_vars.get(name, "").strip()
    return value if re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value) else default


def css_text(name, default):
    return _css_vars.get(name, "").strip().strip("\"'") or default


BG = css_color("--bg", "#121218")
SURFACE = css_color("--surface", "#1a1a24")
SURFACE_ALT = css_color("--surface-alt", "#222230")
BTN_BG = css_color("--button-bg", "#2b2b3d")
BTN_HOVER = css_color("--button-hover", "#3b3b52")
BTN_ACTIVE = css_color("--button-active", "#4e4e6d")
TEXT = css_color("--text", "#e2e8f0")
TEXT_MUTED = css_color("--text-muted", "#94a3b8")
TEXT_DIM = css_color("--text-dim", "#64748b")
TEXT_BRIGHT = css_color("--text-bright", "#ffffff")
ACCENT = css_color("--accent", "#89b4fa")
OK_TEXT = css_color("--ok-text", "#a6e3a1")
WARN_TEXT = css_color("--warn-text", "#f38ba8")
GOLD_TEXT = css_color("--gold-text", "#f9e2af")
PRIMARY = css_color("--primary", "#3b82f6")
PRIMARY_HOVER = css_color("--primary-hover", "#2563eb")
PRIMARY_ACTIVE = css_color("--primary-active", "#1d4ed8")
SUCCESS = css_color("--success", "#238636")
SUCCESS_HOVER = css_color("--success-hover", "#2ea043")
SUCCESS_ACTIVE = css_color("--success-active", "#196c2e")
DANGER = css_color("--danger", "#da3633")
DANGER_HOVER = css_color("--danger-hover", "#b62324")
FONT = css_text("--font-family", "Segoe UI")
FONT_MONO = css_text("--font-mono", "Courier New")


def safe_float(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def safe_int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


PBKDF2_ROUNDS = 200000


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()
    return f"pbkdf2${PBKDF2_ROUNDS}${salt}${digest}"


def verify_password(stored, supplied):
    """Returns (matches, needs_rehash). Plain-text values come from older databases."""
    stored = stored or ""
    if stored.startswith("pbkdf2$"):
        try:
            _, rounds, salt, digest = stored.split("$")
            check = hashlib.pbkdf2_hmac("sha256", supplied.encode("utf-8"), bytes.fromhex(salt), int(rounds)).hex()
        except ValueError:
            return False, False
        return hmac.compare_digest(check, digest), False
    return hmac.compare_digest(stored.encode("utf-8"), supplied.encode("utf-8")), True


def tree_row(tree, iid):
    # item()["values"] turns "0977123456" into 977123456; set() keeps the text as displayed
    return [str(tree.set(iid, col)) for col in tree["columns"]]


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


# --- MODERN GLASSY BUTTON HELPER ---
class GlassButton(tk.Button):
    def __init__(self, master, **kwargs):
        self.normal_bg = kwargs.pop("bg", BTN_BG)
        self.hover_bg = kwargs.pop("hover_bg", BTN_HOVER)
        self.active_bg = kwargs.pop("active_bg", BTN_ACTIVE)

        kwargs.setdefault("fg", TEXT)
        kwargs.setdefault("font", (FONT, 9, "bold"))
        kwargs.setdefault("relief", "flat")
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("padx", 12)
        kwargs.setdefault("pady", 6)
        kwargs.setdefault("cursor", "hand2")

        super().__init__(master, bg=self.normal_bg, activebackground=self.active_bg, activeforeground=TEXT_BRIGHT, **kwargs)

        self.bind("<Enter>", self.on_enter)
        self.bind("<Leave>", self.on_leave)

    def on_enter(self, e):
        self.config(bg=self.hover_bg)

    def on_leave(self, e):
        self.config(bg=self.normal_bg)


class SplashLoadingScreen:
    def __init__(self, root, on_complete):
        self.root = root
        self.on_complete = on_complete

        self.root.title("Loading...")
        self.root.geometry("460x300")
        self.root.config(bg=BG)
        self.root.resizable(False, False)

        self.center_window(460, 300)

        card = tk.Frame(self.root, bg=SURFACE, padx=30, pady=30)
        card.place(relx=0.5, rely=0.5, anchor="center", width=400, height=240)

        tk.Label(
            card, text=config["app_settings"]["title"], font=(FONT, 16, "bold"),
            bg=SURFACE, fg=ACCENT
        ).pack(pady=(10, 5))

        tk.Label(
            card, text="Enterprise Inventory & POS System", font=(FONT, 9),
            bg=SURFACE, fg=TEXT_MUTED
        ).pack(pady=(0, 20))

        self.progress = ttk.Progressbar(card, orient="horizontal", length=320, mode="determinate")
        self.progress.pack(pady=(0, 15))

        self.lbl_status = tk.Label(
            card, text="Initializing secure environment...", font=(FONT, 8),
            bg=SURFACE, fg=TEXT_DIM
        )
        self.lbl_status.pack(anchor="w")

        tk.Label(
            card, text="Software developed by MS industries LTD", font=(FONT, 8, "italic"),
            bg=SURFACE, fg=OK_TEXT
        ).pack(pady=(15, 0), anchor="center")

        self.step_counter = 0
        self.run_loader()

    def center_window(self, w, h):
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        x = (ws / 2) - (w / 2)
        y = (hs / 2) - (h / 2)
        self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

    def run_loader(self):
        if self.step_counter <= 100:
            self.progress["value"] = self.step_counter
            if self.step_counter == 30:
                self.lbl_status.config(text="Loading database schemas & drivers...")
            elif self.step_counter == 60:
                self.lbl_status.config(text="Configuring interface styles & HR payroll...")
            elif self.step_counter == 90:
                self.lbl_status.config(text="Ready.")

            self.step_counter += 1
            self.root.after(100, self.run_loader)
        else:
            for widget in self.root.winfo_children():
                widget.destroy()
            self.on_complete(self.root)


class BusinessSetupDialog:
    def __init__(self, root, callback):
        self.dialog = tk.Toplevel(root)
        self.dialog.title("Business Registration Setup")
        self.dialog.geometry("400x385")
        self.dialog.config(bg=BG)
        self.dialog.resizable(False, False)
        self.dialog.grab_set()

        self.callback = callback

        tk.Label(self.dialog, text="🏢 Register Your Business", font=(FONT, 13, "bold"), bg=BG, fg=ACCENT).pack(pady=(20, 10))
        tk.Label(self.dialog, text="As the primary admin, please enter your business details for official receipts and reporting.", font=(FONT, 8), bg=BG, fg=TEXT_MUTED, wraplength=350).pack(pady=(0, 15))

        form = tk.Frame(self.dialog, bg=BG, padx=25)
        form.pack(fill="both", expand=True)

        tk.Label(form, text="Company / Business Name:", font=(FONT, 9), bg=BG, fg=TEXT).pack(anchor="w")
        self.ent_name = tk.Entry(form, font=(FONT, 10), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        self.ent_name.pack(fill="x", pady=(2, 8), ipady=3)
        self.ent_name.insert(0, config.get("receipt_header", {}).get("company_name", ""))

        tk.Label(form, text="Subtitle / TPIN / Reg No:", font=(FONT, 9), bg=BG, fg=TEXT).pack(anchor="w")
        self.ent_sub = tk.Entry(form, font=(FONT, 10), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        self.ent_sub.pack(fill="x", pady=(2, 8), ipady=3)
        self.ent_sub.insert(0, config.get("receipt_header", {}).get("subtitle", ""))

        tk.Label(form, text="Contact / Address Info:", font=(FONT, 9), bg=BG, fg=TEXT).pack(anchor="w")
        self.ent_contact = tk.Entry(form, font=(FONT, 10), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        self.ent_contact.pack(fill="x", pady=(2, 15), ipady=3)
        self.ent_contact.insert(0, config.get("receipt_header", {}).get("contact", ""))

        GlassButton(form, text="Save & Complete Registration", bg=SUCCESS, hover_bg=SUCCESS_HOVER, active_bg=SUCCESS, command=self.save).pack(fill="x")

    def save(self):
        name = self.ent_name.get().strip()
        sub = self.ent_sub.get().strip()
        contact = self.ent_contact.get().strip()

        if not name:
            messagebox.showwarning("Validation Error", "Business name is required.")
            return

        config["receipt_header"]["company_name"] = name
        config["receipt_header"]["subtitle"] = sub
        config["receipt_header"]["contact"] = contact
        config["app_settings"]["title"] = name

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)

        self.dialog.destroy()
        if self.callback:
            self.callback()


class AuthWindow:
    def __init__(self, root):
        self.root = root
        self.root.title("User Authentication - " + config["app_settings"]["title"])
        self.root.geometry("420x390")
        self.root.config(bg=BG)
        self.root.resizable(False, False)

        self.center_window(420, 390)
        self.init_auth_db()
        self.current_user = None

        card = tk.Frame(self.root, bg=SURFACE, padx=30, pady=25)
        card.place(relx=0.5, rely=0.5, anchor="center", width=380, height=350)

        tk.Label(
            card, text="🔐 Secure POS Login", font=(FONT, 14, "bold"),
            bg=SURFACE, fg=ACCENT
        ).pack(pady=(10, 15))

        tk.Label(card, text="Username:", font=(FONT, 9), bg=SURFACE, fg=TEXT_MUTED).pack(anchor="w")
        self.entry_user = tk.Entry(card, font=(FONT, 10), bg=SURFACE_ALT, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        self.entry_user.pack(fill="x", pady=(2, 10), ipady=3)

        tk.Label(card, text="Password:", font=(FONT, 9), bg=SURFACE, fg=TEXT_MUTED).pack(anchor="w")
        self.entry_pass = tk.Entry(card, font=(FONT, 10), show="*", bg=SURFACE_ALT, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        self.entry_pass.pack(fill="x", pady=(2, 15), ipady=3)

        GlassButton(card, text="Login", bg=PRIMARY, hover_bg=PRIMARY_HOVER, active_bg=PRIMARY_ACTIVE, command=self.handle_login).pack(fill="x", pady=(0, 8))
        GlassButton(card, text="Register New Admin Account", bg=SUCCESS, hover_bg=SUCCESS_HOVER, active_bg=SUCCESS_ACTIVE, command=self.handle_register_admin).pack(fill="x")

    def center_window(self, w, h):
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        x = (ws / 2) - (w / 2)
        y = (hs / 2) - (h / 2)
        self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

    def init_auth_db(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.cursor = self.conn.cursor()
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT DEFAULT 'admin',
                can_sell INTEGER DEFAULT 1,
                can_procure INTEGER DEFAULT 1,
                can_transfer INTEGER DEFAULT 1,
                can_delete INTEGER DEFAULT 0
            )
        """
        )
        for col_def in [
            "role TEXT DEFAULT 'admin'",
            "can_sell INTEGER DEFAULT 1",
            "can_procure INTEGER DEFAULT 1",
            "can_transfer INTEGER DEFAULT 1",
            "can_delete INTEGER DEFAULT 0",
            "owner_id INTEGER"
        ]:
            try:
                self.cursor.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
        # Operators created before owner_id existed are assigned to the first admin
        self.cursor.execute("UPDATE users SET owner_id=(SELECT MIN(id) FROM users WHERE role='admin') WHERE role='operator' AND owner_id IS NULL")
        self.conn.commit()

    def handle_login(self):
        username = self.entry_user.get().strip()
        password = self.entry_pass.get().strip()

        if not username or not password:
            messagebox.showwarning("Validation Error", "Please fill in both username and password.")
            return

        self.cursor.execute("SELECT id, role, can_sell, can_procure, can_transfer, can_delete, password, owner_id FROM users WHERE username=?", (username,))
        user = self.cursor.fetchone()
        matches, needs_rehash = verify_password(user[6], password) if user else (False, False)

        if matches:
            if needs_rehash:
                self.cursor.execute("UPDATE users SET password=? WHERE id=?", (hash_password(password), user[0]))
                self.conn.commit()
            role = user[1] or "admin"
            # Operators work on the stock, sales and payroll of the admin who created them
            self.current_user = {
                "id": user[0], "username": username, "role": role,
                "data_id": user[7] if role == "operator" and user[7] else user[0],
                "permissions": {
                    "sell": bool(user[2]), "procure": bool(user[3]),
                    "transfer": bool(user[4]), "delete": bool(user[5])
                }
            }
            self.conn.close()
            self.open_main_app()
        else:
            messagebox.showerror("Login Failed", "Invalid username or password.")

    def handle_register_admin(self):
        username = self.entry_user.get().strip()
        password = self.entry_pass.get().strip()

        if not username or not password:
            messagebox.showwarning("Validation Error", "Please fill in username and password to register Admin.")
            return

        self.cursor.execute("SELECT password FROM users WHERE role='admin' OR role IS NULL")
        admins = self.cursor.fetchall()
        if admins:
            admin_pass = simpledialog.askstring(
                "Admin Authorization Required",
                "An admin account already exists.\nEnter an existing Admin password to register another:", show="*", parent=self.root
            )
            if not admin_pass:
                return
            if not any(verify_password(a[0], admin_pass.strip())[0] for a in admins):
                messagebox.showerror("Access Denied", "Incorrect Admin password.")
                return

        try:
            self.cursor.execute(
                "INSERT INTO users (username, password, role, can_sell, can_procure, can_transfer, can_delete) VALUES (?, ?, 'admin', 1, 1, 1, 1)",
                (username, hash_password(password))
            )
            self.conn.commit()
            messagebox.showinfo("Success", "Admin account created successfully!")
            if not admins:
                BusinessSetupDialog(self.root, lambda: None)
        except sqlite3.IntegrityError:
            messagebox.showerror("Error", "Username already exists. Please choose a different username.")

    def open_main_app(self):
        for widget in self.root.winfo_children():
            widget.destroy()
        MainApp(self.root, self.current_user)


class OperatorManagementDialog:
    def __init__(self, parent, conn, owner_id):
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Operator Account Management")
        self.dialog.geometry("720x540")
        self.dialog.config(bg=BG)
        self.dialog.grab_set()

        self.conn = conn
        self.owner_id = owner_id
        self.cursor = self.conn.cursor()

        tk.Label(self.dialog, text="👥 Manage Operator Accounts", font=(FONT, 13, "bold"), bg=BG, fg=ACCENT).pack(pady=(15, 10))

        body = tk.Frame(self.dialog, bg=BG)
        body.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        left_frame = tk.Frame(body, bg=BG)
        left_frame.pack(side="left", fill="both", expand=True, padx=(0, 10))

        tk.Label(left_frame, text="Existing Operators:", font=(FONT, 9, "bold"), bg=BG, fg=TEXT).pack(anchor="w", pady=(0, 5))

        cols = ("ID", "Username")
        self.op_tree = ttk.Treeview(left_frame, columns=cols, show="headings", selectmode="browse", height=15)
        for col in cols:
            self.op_tree.heading(col, text=col)
        self.op_tree.column("ID", width=35, anchor="center")
        self.op_tree.column("Username", width=220)

        op_scroll = ttk.Scrollbar(left_frame, orient="vertical", command=self.op_tree.yview)
        self.op_tree.configure(yscrollcommand=op_scroll.set)
        self.op_tree.pack(side="left", fill="both", expand=True)
        op_scroll.pack(side="right", fill="y")
        self.op_tree.bind("<<TreeviewSelect>>", self.on_operator_select)

        right_frame = tk.Frame(body, bg=SURFACE, padx=15, pady=15)
        right_frame.pack(side="right", fill="both", expand=True)

        self.lbl_form_title = tk.Label(right_frame, text="Create New Operator", font=(FONT, 11, "bold"), bg=SURFACE, fg=OK_TEXT)
        self.lbl_form_title.pack(anchor="w", pady=(0, 10))

        tk.Label(right_frame, text="Username:", font=(FONT, 9), bg=SURFACE, fg=TEXT_MUTED).pack(anchor="w")
        self.ent_user = tk.Entry(right_frame, font=(FONT, 9), bg=SURFACE_ALT, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        self.ent_user.pack(fill="x", pady=(2, 8), ipady=2)

        self.lbl_pass = tk.Label(right_frame, text="Password:", font=(FONT, 9), bg=SURFACE, fg=TEXT_MUTED)
        self.lbl_pass.pack(anchor="w")
        self.ent_pass = tk.Entry(right_frame, show="*", font=(FONT, 9), bg=SURFACE_ALT, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        self.ent_pass.pack(fill="x", pady=(2, 12), ipady=2)

        tk.Label(right_frame, text="Permissions:", font=(FONT, 9, "bold"), bg=SURFACE, fg=ACCENT).pack(anchor="w", pady=(0, 5))

        self.var_sell = tk.IntVar(value=1)
        self.var_procure = tk.IntVar(value=1)
        self.var_transfer = tk.IntVar(value=1)
        self.var_delete = tk.IntVar(value=0)

        for text, var in [
            ("Can Process Sales (POS)", self.var_sell),
            ("Can Log Purchases / Inventory", self.var_procure),
            ("Can Perform Farm Transfers & Use", self.var_transfer),
            ("Can Delete Inventory Items", self.var_delete)
        ]:
            tk.Checkbutton(right_frame, text=text, variable=var, bg=SURFACE, fg=TEXT, selectcolor=SURFACE_ALT, activebackground=SURFACE, activeforeground=TEXT_BRIGHT).pack(anchor="w", pady=2)

        btn_bar = tk.Frame(right_frame, bg=SURFACE)
        btn_bar.pack(fill="x", pady=(10, 0))

        self.btn_save = GlassButton(btn_bar, text="Create Operator", bg=SUCCESS, hover_bg=SUCCESS_HOVER, command=self.save_operator)
        self.btn_save.pack(side="left", fill="x", expand=True, padx=(0, 4))

        GlassButton(btn_bar, text="Clear", bg=PRIMARY, hover_bg=PRIMARY_HOVER, command=self.clear_form).pack(side="left", padx=(0, 4))
        GlassButton(btn_bar, text="Delete", bg=DANGER, hover_bg=DANGER_HOVER, command=self.delete_operator).pack(side="left")

        self.selected_op_id = None
        self.load_operators()

    def load_operators(self):
        for item in self.op_tree.get_children():
            self.op_tree.delete(item)
        self.cursor.execute("SELECT id, username FROM users WHERE role='operator' AND owner_id=?", (self.owner_id,))
        for row in self.cursor.fetchall():
            self.op_tree.insert("", "end", values=row)

    def on_operator_select(self, event):
        selected = self.op_tree.selection()
        if selected:
            row = self.op_tree.item(selected[0])["values"]
            self.selected_op_id = row[0]
            self.cursor.execute("SELECT username, can_sell, can_procure, can_transfer, can_delete FROM users WHERE id=? AND role='operator' AND owner_id=?", (self.selected_op_id, self.owner_id))
            op = self.cursor.fetchone()
            if op:
                self.clear_form(keep_selection=True)
                self.selected_op_id = row[0]
                self.ent_user.insert(0, op[0])
                self.var_sell.set(op[1])
                self.var_procure.set(op[2])
                self.var_transfer.set(op[3])
                self.var_delete.set(op[4])
                self.lbl_form_title.config(text=f"Edit Operator #{self.selected_op_id}")
                self.lbl_pass.config(text="New Password (leave blank to keep current):")
                self.btn_save.config(text="Save Changes")

    def clear_form(self, keep_selection=False):
        if not keep_selection:
            self.selected_op_id = None
            self.op_tree.selection_remove(self.op_tree.selection())
            self.lbl_form_title.config(text="Create New Operator")
            self.lbl_pass.config(text="Password:")
            self.btn_save.config(text="Create Operator")
        self.ent_user.delete(0, tk.END)
        self.ent_pass.delete(0, tk.END)
        self.var_sell.set(1)
        self.var_procure.set(1)
        self.var_transfer.set(1)
        self.var_delete.set(0)

    def save_operator(self):
        uname = self.ent_user.get().strip()
        upass = self.ent_pass.get().strip()

        if not uname or (not upass and not self.selected_op_id):
            messagebox.showwarning("Validation Error", "Provide both username and password.")
            return

        try:
            if self.selected_op_id:
                self.cursor.execute(
                    """UPDATE users SET username=?, can_sell=?, can_procure=?, can_transfer=?, can_delete=? WHERE id=? AND role='operator' AND owner_id=?""",
                    (uname, self.var_sell.get(), self.var_procure.get(), self.var_transfer.get(), self.var_delete.get(), self.selected_op_id, self.owner_id)
                )
                if upass:
                    self.cursor.execute(
                        "UPDATE users SET password=? WHERE id=? AND role='operator' AND owner_id=?",
                        (hash_password(upass), self.selected_op_id, self.owner_id)
                    )
                success_msg = f"Operator '{uname}' updated successfully!"
            else:
                self.cursor.execute(
                    """INSERT INTO users (username, password, role, can_sell, can_procure, can_transfer, can_delete, owner_id)
                       VALUES (?, ?, 'operator', ?, ?, ?, ?, ?)""",
                    (uname, hash_password(upass), self.var_sell.get(), self.var_procure.get(), self.var_transfer.get(), self.var_delete.get(), self.owner_id)
                )
                success_msg = f"Operator account '{uname}' created successfully!"

            self.conn.commit()
            messagebox.showinfo("Success", success_msg)
            self.load_operators()
            self.clear_form()
        except sqlite3.IntegrityError:
            self.conn.rollback()
            messagebox.showerror("Error", "Username already exists. Choose another.")

    def delete_operator(self):
        if not self.selected_op_id:
            messagebox.showwarning("Selection Error", "Please select an operator from the list to delete.")
            return

        if messagebox.askyesno("Confirm Delete", "Permanently delete this operator account?"):
            self.cursor.execute("DELETE FROM users WHERE id=? AND role='operator' AND owner_id=?", (self.selected_op_id, self.owner_id))
            self.conn.commit()
            self.load_operators()
            self.clear_form()
            messagebox.showinfo("Deleted", "Operator account deleted.")


class MainApp:
    def __init__(self, root, current_user):
        self.root = root
        self.current_user = current_user
        self.data_uid = current_user.get("data_id", current_user["id"])

        role_label = current_user['role'].upper()
        self.root.title(f"{config['app_settings']['title']} — {current_user['username']} ({role_label})")

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        if screen_w < 1300 or screen_h < 800:
            width, height = 1100, 680
            self.is_compact = True
        else:
            width, height = 1320, 800
            self.is_compact = False

        self.root.geometry(f"{width}x{height}")
        self.center_window(width, height)
        self.root.config(bg=BG)

        self.cart_items = []

        self.init_db()
        self.setup_styles()

        nav_frame = tk.Frame(self.root, bg=SURFACE, height=55)
        nav_frame.pack(side="top", fill="x")

        self.btn_pos = GlassButton(nav_frame, text=config["labels"]["pos_tab"], bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.show_sales_page)
        self.btn_pos.pack(side="left", padx=6, pady=10)

        self.btn_inv = GlassButton(nav_frame, text=config["labels"]["inventory_tab"], bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.show_inventory_page)
        self.btn_inv.pack(side="left", padx=6, pady=10)

        self.btn_procure = GlassButton(nav_frame, text="📦 Procurements", bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.show_procurements_page)
        self.btn_procure.pack(side="left", padx=6, pady=10)

        self.btn_hr = GlassButton(nav_frame, text="👥 HR & Payroll", bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.show_hr_page)
        self.btn_hr.pack(side="left", padx=6, pady=10)

        self.btn_history = GlassButton(nav_frame, text="📜 Sales & Invoices", bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.show_history_page)
        self.btn_history.pack(side="left", padx=6, pady=10)

        GlassButton(nav_frame, text="🔒 Logout", bg=DANGER, hover_bg=DANGER_HOVER, command=self.logout_user).pack(side="right", padx=15, pady=10)

        if self.current_user["role"] == "admin":
            GlassButton(
                nav_frame, text="👥 Operators", bg=PRIMARY, hover_bg=PRIMARY_HOVER,
                command=lambda: OperatorManagementDialog(self.root, self.conn, self.data_uid)
            ).pack(side="right", padx=5, pady=10)

            GlassButton(
                nav_frame, text="⚙️ Departments", bg=SUCCESS, hover_bg=SUCCESS_HOVER,
                command=self.open_department_management_dialog
            ).pack(side="right", padx=5, pady=10)

        self.container = tk.Frame(self.root, bg=BG)
        self.container.pack(side="bottom", fill="both", expand=True)

        self.sales_page = tk.Frame(self.container, bg=BG)
        self.inventory_page = tk.Frame(self.container, bg=BG)
        self.procurements_page = tk.Frame(self.container, bg=BG)
        self.hr_page = tk.Frame(self.container, bg=BG)
        self.history_page = tk.Frame(self.container, bg=BG)

        self.setup_sales_page()
        self.setup_inventory_page()
        self.setup_procurements_page()
        self.setup_hr_page()
        self.setup_history_page()

        self.show_sales_page()

    def center_window(self, w, h):
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        x = (ws / 2) - (w / 2)
        y = (hs / 2) - (h / 2)
        self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

    def logout_user(self):
        if messagebox.askyesno("Logout", "Are you sure you want to log out?"):
            self.conn.close()
            for widget in self.root.winfo_children():
                widget.destroy()
            AuthWindow(self.root)

    def init_db(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.cursor = self.conn.cursor()

        self.cursor.execute("""CREATE TABLE IF NOT EXISTS departments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT NOT NULL)""")
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_code TEXT NOT NULL, name TEXT NOT NULL, category TEXT, department TEXT DEFAULT 'General', batch_no TEXT DEFAULT 'N/A', date_planted TEXT DEFAULT 'N/A', quantity INTEGER NOT NULL, price REAL NOT NULL, supplier_name TEXT DEFAULT 'N/A', invoice_no TEXT DEFAULT 'N/A', purchase_date TEXT DEFAULT 'N/A', unit_cost REAL DEFAULT 0.0, expiry_date TEXT DEFAULT 'N/A')""")
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS procurements (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_code TEXT, name TEXT, category TEXT, department TEXT, supplier_name TEXT, invoice_no TEXT, purchase_date TEXT, quantity INTEGER, unit_cost REAL, total_cost REAL, expiry_date TEXT)""")
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS pos_products (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_code TEXT NOT NULL, name TEXT NOT NULL, category TEXT, department TEXT DEFAULT 'General', batch_no TEXT DEFAULT 'N/A', date_planted TEXT DEFAULT 'N/A', quantity INTEGER NOT NULL, price REAL NOT NULL)""")
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, invoice_no TEXT, item_code TEXT, item_name TEXT, department TEXT, batch_no TEXT, date_planted TEXT, qty_sold INTEGER, unit_price REAL, total_price REAL, payment_status TEXT DEFAULT 'Paid', amount_paid REAL DEFAULT 0.0, balance_due REAL DEFAULT 0.0, customer_name TEXT, customer_phone TEXT, fulfillment_type TEXT, transaction_type TEXT DEFAULT 'SALE', timestamp TEXT)""")

        # Employees Table for HR
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, full_name TEXT NOT NULL, nrc_number TEXT NOT NULL,
            basic_salary REAL NOT NULL, employment_type TEXT DEFAULT 'Permanent', address TEXT, age INTEGER,
            start_date TEXT, expected_days_week INTEGER
        )""")

        # HR Non-Working Days & Statutory Configuration Table
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS hr_settings (
            user_id INTEGER PRIMARY KEY, non_working_days TEXT, napsa_pct REAL, nhima_pct REAL,
            napsa_bearer TEXT DEFAULT 'employee', nhima_bearer TEXT DEFAULT 'employee'
        )""")

        # Payroll Records Table
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS payroll (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, employee_id INTEGER, employee_name TEXT,
            pay_month TEXT, basic_salary REAL, expected_days INTEGER, worked_days INTEGER,
            calculated_basic REAL, bonuses_json TEXT, deductions_json TEXT, napsa_amt REAL, nhima_amt REAL, net_payable REAL, timestamp TEXT,
            napsa_bearer TEXT DEFAULT 'employee', nhima_bearer TEXT DEFAULT 'employee', employer_napsa REAL DEFAULT 0.0, employer_nhima REAL DEFAULT 0.0
        )""")

        payroll_columns = [
            ("user_id", "INTEGER"), ("employee_id", "INTEGER"), ("employee_name", "TEXT"),
            ("pay_month", "TEXT"), ("basic_salary", "REAL"), ("expected_days", "INTEGER"),
            ("worked_days", "INTEGER"), ("calculated_basic", "REAL"), ("bonuses_json", "TEXT"),
            ("deductions_json", "TEXT"), ("napsa_amt", "REAL"), ("nhima_amt", "REAL"),
            ("net_payable", "REAL"), ("timestamp", "TEXT"), ("napsa_bearer", "TEXT"),
            ("nhima_bearer", "TEXT"), ("employer_napsa", "REAL"), ("employer_nhima", "REAL")
        ]
        for col_name, col_type in payroll_columns:
            try:
                self.cursor.execute(f"ALTER TABLE payroll ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

        for hr_col in [("napsa_bearer", "TEXT DEFAULT 'employee'"), ("nhima_bearer", "TEXT DEFAULT 'employee'")]:
            try:
                self.cursor.execute(f"ALTER TABLE hr_settings ADD COLUMN {hr_col[0]} {hr_col[1]}")
            except sqlite3.OperationalError:
                pass

        for tbl in ["departments", "products", "procurements", "pos_products", "sales", "employees"]:
            try:
                self.cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER")
            except sqlite3.OperationalError:
                pass

        # Older databases gained a duplicate 'General' row on every login
        self.cursor.execute("DELETE FROM departments WHERE id NOT IN (SELECT MIN(id) FROM departments GROUP BY user_id, name)")
        self.cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_departments_user_name ON departments (user_id, name)")
        self.conn.commit()
        self.cursor.execute("INSERT OR IGNORE INTO departments (user_id, name) VALUES (?, 'General')", (self.data_uid,))
        self.cursor.execute("INSERT OR IGNORE INTO hr_settings (user_id, non_working_days, napsa_pct, nhima_pct, napsa_bearer, nhima_bearer) VALUES (?, 'Saturday,Sunday', 5.0, 2.0, 'employee', 'employee')", (self.data_uid,))
        self.conn.commit()

    def check_permission(self, perm_key):
        if self.current_user["role"] == "admin" or self.current_user["permissions"].get(perm_key, False):
            return True

        admin_pass = simpledialog.askstring("Admin Authorization Required", f"Permission denied for '{perm_key}'. Enter Admin Password:", show="*")
        if not admin_pass:
            return False

        self.cursor.execute("SELECT password FROM users WHERE id=?", (self.data_uid,))
        owner = self.cursor.fetchone()
        if owner and verify_password(owner[0], admin_pass.strip())[0]:
            messagebox.showinfo("Authorized", "Admin permission granted.")
            return True
        else:
            messagebox.showerror("Access Denied", "Incorrect Admin password.")
            return False

    def get_departments(self):
        self.cursor.execute("SELECT name FROM departments WHERE user_id=?", (self.data_uid,))
        return [row[0] for row in self.cursor.fetchall()]

    def open_department_management_dialog(self):
        if not self.check_permission("delete"):
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Department Management")
        dialog.geometry("400x380")
        dialog.config(bg=BG)
        dialog.grab_set()

        tk.Label(dialog, text="🏢 Department Management", font=(FONT, 12, "bold"), bg=BG, fg=ACCENT).pack(pady=(15, 5))
        tk.Label(dialog, text="Create new departments or delete existing ones.", font=(FONT, 8), bg=BG, fg=TEXT_MUTED).pack(pady=(0, 15))

        create_frame = tk.LabelFrame(dialog, text="Create New Department", bg=BG, fg=OK_TEXT, font=(FONT, 9, "bold"), padx=10, pady=10)
        create_frame.pack(fill="x", padx=20, pady=(0, 15))

        tk.Label(create_frame, text="Department Name:", bg=BG, fg=TEXT, font=(FONT, 8)).pack(anchor="w")
        ent_new_dept = tk.Entry(create_frame, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        ent_new_dept.pack(fill="x", pady=(2, 8), ipady=2)

        def create_dept():
            dept_name = ent_new_dept.get().strip()
            if dept_name:
                try:
                    self.cursor.execute("INSERT INTO departments (user_id, name) VALUES (?, ?)", (self.data_uid, dept_name))
                    self.conn.commit()
                    messagebox.showinfo("Success", f"Department '{dept_name}' created!")
                    ent_new_dept.delete(0, tk.END)
                    refresh_dept_list()
                    self.update_department_dropdowns()
                except sqlite3.IntegrityError:
                    self.conn.rollback()
                    messagebox.showerror("Error", "Department already exists.")

        GlassButton(create_frame, text="Create Department", bg=SUCCESS, hover_bg=SUCCESS_HOVER, command=create_dept).pack(fill="x")

        delete_frame = tk.LabelFrame(dialog, text="Delete Department", bg=BG, fg=WARN_TEXT, font=(FONT, 9, "bold"), padx=10, pady=10)
        delete_frame.pack(fill="x", padx=20)

        tk.Label(delete_frame, text="Select Department (Assigned items move to 'General'):", bg=BG, fg=TEXT, font=(FONT, 8)).pack(anchor="w")
        combo_del = ttk.Combobox(delete_frame, state="readonly", font=(FONT, 9))
        combo_del.pack(fill="x", pady=(2, 8))

        def refresh_dept_list():
            depts = self.get_departments()
            if "General" in depts:
                depts.remove("General")
            combo_del["values"] = depts
            if depts:
                combo_del.set(depts[0])
            else:
                combo_del.set("")

        refresh_dept_list()

        def delete_dept():
            dept_to_del = combo_del.get()
            if not dept_to_del:
                messagebox.showwarning("Selection", "No department selected for deletion.")
                return

            if messagebox.askyesno("Confirm Delete", f"Delete department '{dept_to_del}'? All assigned items will be reallocated to 'General'."):
                uid = self.data_uid
                self.cursor.execute("DELETE FROM departments WHERE user_id=? AND name=?", (uid, dept_to_del))
                self.cursor.execute("UPDATE products SET department='General' WHERE user_id=? AND department=?", (uid, dept_to_del))
                self.cursor.execute("UPDATE pos_products SET department='General' WHERE user_id=? AND department=?", (uid, dept_to_del))
                self.conn.commit()
                messagebox.showinfo("Deleted", f"Department '{dept_to_del}' deleted successfully.")
                refresh_dept_list()
                self.update_department_dropdowns()
                self.load_inventory_data()
                self.refresh_sales_page()

        GlassButton(delete_frame, text="Delete Department", bg=DANGER, hover_bg=DANGER_HOVER, command=delete_dept).pack(fill="x")

    def update_department_dropdowns(self):
        depts = self.get_departments()
        all_depts_filter = ["All Departments"] + depts

        self.combo_pos_dept["values"] = all_depts_filter
        self.combo_inv_filter["values"] = all_depts_filter
        self.combo_inv_dept["values"] = depts
        self.combo_hist_dept["values"] = all_depts_filter

        self.cursor.execute("SELECT DISTINCT category FROM procurements WHERE user_id=? AND category IS NOT NULL AND category != ''", (self.data_uid,))
        cats = [row[0] for row in self.cursor.fetchall()]
        self.combo_procure_cat["values"] = ["All Categories"] + cats

    def setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("Treeview", background=SURFACE, foreground=TEXT, fieldbackground=SURFACE, rowheight=28, font=(FONT, 9))
        style.configure("Treeview.Heading", background=SURFACE_ALT, foreground=TEXT_MUTED, font=(FONT, 9, "bold"))
        style.map("Treeview", background=[("selected", PRIMARY)], foreground=[("selected", TEXT_BRIGHT)])

    def set_nav_states(self, active_btn):
        for btn in (self.btn_pos, self.btn_inv, self.btn_procure, self.btn_hr, self.btn_history):
            if btn == active_btn:
                btn.config(bg=PRIMARY)
            else:
                btn.config(bg=SURFACE_ALT)

    def show_sales_page(self):
        for page in (self.inventory_page, self.procurements_page, self.hr_page, self.history_page):
            page.pack_forget()
        self.sales_page.pack(fill="both", expand=True)
        self.set_nav_states(self.btn_pos)
        self.update_department_dropdowns()
        self.refresh_sales_page()

    def show_inventory_page(self):
        for page in (self.sales_page, self.procurements_page, self.hr_page, self.history_page):
            page.pack_forget()
        self.inventory_page.pack(fill="both", expand=True)
        self.set_nav_states(self.btn_inv)
        self.update_department_dropdowns()
        self.load_inventory_data()

    def show_procurements_page(self):
        for page in (self.sales_page, self.inventory_page, self.hr_page, self.history_page):
            page.pack_forget()
        self.procurements_page.pack(fill="both", expand=True)
        self.set_nav_states(self.btn_procure)
        self.update_department_dropdowns()
        self.load_procurements_data()

    def show_hr_page(self):
        for page in (self.sales_page, self.inventory_page, self.procurements_page, self.history_page):
            page.pack_forget()
        self.hr_page.pack(fill="both", expand=True)
        self.set_nav_states(self.btn_hr)
        self.load_employees_data()

    def show_history_page(self):
        for page in (self.sales_page, self.inventory_page, self.procurements_page, self.hr_page):
            page.pack_forget()
        self.history_page.pack(fill="both", expand=True)
        self.set_nav_states(self.btn_history)
        self.update_department_dropdowns()
        self.load_sales_history()

    def open_barcode_scanner(self, callback_mode="inventory"):
        if not SCANNER_AVAILABLE:
            messagebox.showerror("Scanner Unavailable", "OpenCV and pyzbar are required for camera barcode scanning.")
            return

        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            messagebox.showerror("Camera Error", "Could not access the built-in camera/webcam.")
            return

        messagebox.showinfo("Barcode Scanner Active", "Point your webcam at a barcode.\nPress 'Q' or close the camera window when done.")
        scanned_code = None

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            decoded_objects = pyzbar.decode(frame)
            for obj in decoded_objects:
                scanned_code = obj.data.decode("utf-8")
                (x, y, w, h) = obj.rect
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                cv2.putText(frame, scanned_code, (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            cv2.imshow("Barcode Scanner (Press Q to Exit)", frame)
            if scanned_code or (cv2.waitKey(1) & 0xFF == ord('q')):
                break

        cap.release()
        cv2.destroyAllWindows()

        if scanned_code:
            if callback_mode == "inventory":
                self.handle_scanned_inventory_code(scanned_code)
            elif callback_mode == "sales":
                self.handle_scanned_sales_code(scanned_code)

    INV_FORM_COLS = "item_code, name, category, department, batch_no, date_planted, quantity, price, supplier_name, invoice_no, purchase_date, unit_cost, expiry_date"

    def fill_inv_form(self, p):
        self.clear_inv_entries()
        self.entry_pdate.delete(0, tk.END)
        self.entry_code.insert(0, str(p[0]))
        self.entry_name.insert(0, str(p[1]))
        self.entry_cat.insert(0, str(p[2]))
        self.combo_inv_dept.set(str(p[3]))
        self.entry_batch.insert(0, str(p[4]))
        self.entry_planted.insert(0, str(p[5]))
        self.entry_qty.insert(0, str(p[6]))
        self.entry_price.insert(0, str(p[7]))
        self.entry_supplier.insert(0, str(p[8]))
        self.entry_invoice.insert(0, str(p[9]))
        self.entry_pdate.insert(0, str(p[10]))
        self.entry_ucost.insert(0, str(p[11]))
        self.entry_expiry.insert(0, str(p[12]))

    def handle_scanned_inventory_code(self, code):
        self.cursor.execute(f"SELECT {self.INV_FORM_COLS} FROM products WHERE user_id=? AND item_code=?", (self.data_uid, code))
        p = self.cursor.fetchone()
        if p:
            self.fill_inv_form(p)
            messagebox.showinfo("Found", f"Existing item found for code '{code}'. Details loaded.")
        else:
            self.entry_code.delete(0, tk.END)
            self.entry_code.insert(0, code)
            messagebox.showinfo("New", f"Scanned new code '{code}'. Fill in details to log purchase.")

    def handle_scanned_sales_code(self, code):
        self.cursor.execute("SELECT id FROM pos_products WHERE user_id=? AND item_code=? AND quantity > 0", (self.data_uid, code))
        hit = self.cursor.fetchone()
        if not hit:
            messagebox.showwarning("Not Found", f"No active POS product found with code: {code}")
            return

        iid = str(hit[0])
        if not self.pos_tree.exists(iid):
            self.combo_pos_dept.set("All Departments")
            self.refresh_sales_page()
        self.pos_tree.selection_set(iid)
        self.pos_tree.see(iid)
        self.add_item_to_cart()

    # --- TAB 1: SALES & POS ---
    def setup_sales_page(self):
        filter_bar = tk.Frame(self.sales_page, bg=BG)
        filter_bar.pack(fill="x", padx=15, pady=(10, 0))

        tk.Label(filter_bar, text="Filter Department:", bg=BG, fg=TEXT, font=(FONT, 9, "bold")).pack(side="left", padx=(0, 8))
        self.combo_pos_dept = ttk.Combobox(filter_bar, state="readonly", font=(FONT, 9), width=18)
        self.combo_pos_dept.pack(side="left")
        self.combo_pos_dept.set("All Departments")
        self.combo_pos_dept.bind("<<ComboboxSelected>>", lambda e: self.refresh_sales_page())

        GlassButton(filter_bar, text="📷 Scan Barcode", bg=SURFACE_ALT, hover_bg=PRIMARY, command=lambda: self.open_barcode_scanner("sales")).pack(side="right", padx=5)

        dash_frame = tk.Frame(self.sales_page, bg=BG)
        dash_frame.pack(fill="x", padx=15, pady=8)

        thresh = config["app_settings"]["low_stock_threshold"]
        curr = config["app_settings"]["currency"]

        self.card_stock = self.create_card(dash_frame, "Listed POS Items", "0", ACCENT)
        self.card_low = self.create_card(dash_frame, f"Low Stock (<{thresh})", "0", WARN_TEXT)
        self.card_units = self.create_card(dash_frame, "Units Sold", "0", OK_TEXT)
        self.card_rev = self.create_card(dash_frame, "Revenue", f"{curr}0.00", GOLD_TEXT)

        self.card_stock.pack(side="left", fill="both", expand=True, padx=4)
        self.card_low.pack(side="left", fill="both", expand=True, padx=4)
        self.card_units.pack(side="left", fill="both", expand=True, padx=4)
        self.card_rev.pack(side="left", fill="both", expand=True, padx=4)

        lower_frame = tk.Frame(self.sales_page, bg=BG)
        lower_frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        cart_width = 360 if self.is_compact else 420

        p_frame = tk.LabelFrame(lower_frame, text=config["labels"]["available_products_header"], bg=BG, fg=TEXT_MUTED, font=(FONT, 9, "bold"), padx=8, pady=8)
        p_frame.pack(side="left", fill="both", expand=True, padx=(0, 6))

        cols = ("Code", "Name", "Dept", "Stock", "Price")
        self.pos_tree = ttk.Treeview(p_frame, columns=cols, show="headings", selectmode="browse")
        for col in cols:
            self.pos_tree.heading(col, text=col)
        self.pos_tree.column("Code", width=45, anchor="center")
        self.pos_tree.column("Name", width=100)
        self.pos_tree.column("Dept", width=65)
        self.pos_tree.column("Stock", width=40, anchor="center")
        self.pos_tree.column("Price", width=55, anchor="e")

        pos_scroll = ttk.Scrollbar(p_frame, orient="vertical", command=self.pos_tree.yview)
        self.pos_tree.configure(yscrollcommand=pos_scroll.set)
        self.pos_tree.pack(side="left", fill="both", expand=True)
        pos_scroll.pack(side="right", fill="y")

        mid_control = tk.Frame(lower_frame, bg=BG, width=75)
        mid_control.pack(side="left", fill="y", padx=4)
        mid_control.pack_propagate(False)

        tk.Label(mid_control, text="Qty:", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(pady=(110, 2))
        self.entry_cart_qty = tk.Entry(mid_control, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, justify="center", insertbackground=TEXT_BRIGHT, relief="flat")
        self.entry_cart_qty.pack(fill="x", padx=3, pady=(0, 8), ipady=2)
        self.entry_cart_qty.insert(0, "1")

        GlassButton(mid_control, text="Add ➡", bg=PRIMARY, hover_bg=PRIMARY_HOVER, command=self.add_item_to_cart).pack(fill="x", padx=3, pady=3)
        GlassButton(mid_control, text="⬅ Remove", bg=DANGER, hover_bg=DANGER_HOVER, command=self.remove_item_from_cart).pack(fill="x", padx=3, pady=3)

        cart_frame = tk.LabelFrame(lower_frame, text="Active Invoice Cart", bg=BG, fg=TEXT_MUTED, font=(FONT, 9, "bold"), padx=10, pady=8, width=cart_width)
        cart_frame.pack(side="right", fill="both")
        cart_frame.pack_propagate(False)

        cart_cols = ("Name", "Qty", "Price", "Total")
        self.cart_tree = ttk.Treeview(cart_frame, columns=cart_cols, show="headings", selectmode="browse", height=6 if self.is_compact else 8)
        for col in cart_cols:
            self.cart_tree.heading(col, text=col)
        self.cart_tree.column("Name", width=95)
        self.cart_tree.column("Qty", width=30, anchor="center")
        self.cart_tree.column("Price", width=55, anchor="e")
        self.cart_tree.column("Total", width=65, anchor="e")
        self.cart_tree.pack(fill="x", pady=(0, 6))

        checkout_form = tk.Frame(cart_frame, bg=BG)
        checkout_form.pack(fill="both", expand=True)

        tk.Label(checkout_form, text="Customer Name:", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
        self.entry_cust_name = tk.Entry(checkout_form, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        self.entry_cust_name.pack(fill="x", pady=(1, 3), ipady=2)

        tk.Label(checkout_form, text="Phone Number:", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
        self.entry_cust_phone = tk.Entry(checkout_form, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        self.entry_cust_phone.pack(fill="x", pady=(1, 3), ipady=2)

        row_pay = tk.Frame(checkout_form, bg=BG)
        row_pay.pack(fill="x", pady=2)

        tk.Label(row_pay, text="Payment Status:", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(side="left")
        self.combo_payment_status = ttk.Combobox(row_pay, state="readonly", font=(FONT, 8), width=14)
        self.combo_payment_status["values"] = ["Paid", "Partial Payment", "Unpaid"]
        self.combo_payment_status.set("Paid")
        self.combo_payment_status.pack(side="right")

        row_amt = tk.Frame(checkout_form, bg=BG)
        row_amt.pack(fill="x", pady=2)
        tk.Label(row_amt, text="Amount Paid:", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(side="left")
        self.entry_amount_paid = tk.Entry(row_amt, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, width=14, relief="flat")
        self.entry_amount_paid.pack(side="right", ipady=2)

        self.lbl_cart_total = tk.Label(checkout_form, text=f"Total: {curr}0.00", bg=SURFACE, fg=OK_TEXT, font=(FONT, 10, "bold"), pady=4)
        self.lbl_cart_total.pack(fill="x", pady=(4, 6))

        GlassButton(checkout_form, text="📄 Complete Invoice & Generate PDF", bg=SUCCESS, hover_bg=SUCCESS_HOVER, command=self.process_multi_item_sale).pack(fill="x", ipady=2)

    def create_card(self, parent, title, value, color):
        frame = tk.Frame(parent, bg=SURFACE, padx=10, pady=8)
        tk.Label(frame, text=title, bg=SURFACE, fg=TEXT_MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
        lbl_val = tk.Label(frame, text=value, bg=SURFACE, fg=color, font=(FONT, 14, "bold"))
        lbl_val.pack(anchor="w", pady=(2, 0))
        frame.lbl_val = lbl_val
        return frame

    def refresh_sales_page(self):
        for item in self.pos_tree.get_children():
            self.pos_tree.delete(item)

        selected_dept = self.combo_pos_dept.get()
        uid = self.data_uid
        all_depts = selected_dept == "All Departments" or not selected_dept
        if all_depts:
            self.cursor.execute("SELECT id, item_code, name, department, quantity, price FROM pos_products WHERE user_id=? AND quantity > 0", (uid,))
            prod_query = "SELECT COUNT(*) FROM pos_products WHERE user_id=? AND quantity < ? AND quantity > 0"
            prod_total_query = "SELECT COUNT(*) FROM pos_products WHERE user_id=? AND quantity > 0"
            sales_query = "SELECT SUM(qty_sold), SUM(amount_paid) FROM sales WHERE user_id=? AND transaction_type='SALE'"
            params = (uid, config["app_settings"]["low_stock_threshold"])
            sales_params = (uid,)
        else:
            self.cursor.execute("SELECT id, item_code, name, department, quantity, price FROM pos_products WHERE user_id=? AND department=? AND quantity > 0", (uid, selected_dept))
            prod_query = "SELECT COUNT(*) FROM pos_products WHERE user_id=? AND quantity < ? AND department=? AND quantity > 0"
            prod_total_query = "SELECT COUNT(*) FROM pos_products WHERE user_id=? AND department=? AND quantity > 0"
            sales_query = "SELECT SUM(qty_sold), SUM(amount_paid) FROM sales WHERE user_id=? AND department=? AND transaction_type='SALE'"
            params = (uid, config["app_settings"]["low_stock_threshold"], selected_dept)
            sales_params = (uid, selected_dept)

        for p in self.cursor.fetchall():
            self.pos_tree.insert("", "end", iid=str(p[0]), values=(p[1], p[2], p[3], safe_int(p[4]), f"{safe_float(p[5]):,.2f}"))

        self.cursor.execute(prod_query, params)
        low_count = self.cursor.fetchone()[0] or 0

        if all_depts:
            self.cursor.execute(prod_total_query, (uid,))
        else:
            self.cursor.execute(prod_total_query, (uid, selected_dept))
        total_items = self.cursor.fetchone()[0] or 0

        self.cursor.execute(sales_query, sales_params)
        s_data = self.cursor.fetchone()
        units_sold = s_data[0] or 0
        total_rev = safe_float(s_data[1])

        self.card_stock.lbl_val.config(text=str(total_items))
        self.card_low.lbl_val.config(text=str(low_count))
        self.card_units.lbl_val.config(text=str(units_sold))
        self.card_rev.lbl_val.config(text=f"{config['app_settings']['currency']}{total_rev:,.2f}")

    def add_item_to_cart(self):
        selected = self.pos_tree.selection()
        if not selected:
            messagebox.showwarning("Selection Error", "Please select a product from the available list.")
            return

        # Read the product from the database: the table shows formatted text ("1,234.00") and drops leading zeros from codes
        self.cursor.execute("SELECT id, item_code, name, department, quantity, price FROM pos_products WHERE id=? AND user_id=?", (int(selected[0]), self.data_uid))
        p = self.cursor.fetchone()
        if not p:
            self.refresh_sales_page()
            return
        pos_id, code, name, dept, stock, price = p[0], str(p[1]), str(p[2]), str(p[3]), safe_int(p[4]), safe_float(p[5])

        try:
            qty = int(self.entry_cart_qty.get().strip())
        except ValueError:
            messagebox.showerror("Invalid Input", "Enter a valid numeric quantity.")
            return

        if qty <= 0 or qty > stock:
            messagebox.showerror("Stock Error", f"Quantity must be between 1 and {stock}.")
            return

        for item in self.cart_items:
            if item["pos_id"] == pos_id:
                new_qty = item["qty"] + qty
                if new_qty > stock:
                    messagebox.showerror("Stock Error", f"Total requested quantity exceeds stock ({stock}).")
                    return
                item["qty"] = new_qty
                item["total"] = new_qty * price
                self.refresh_cart_display()
                return

        self.cart_items.append({
            "pos_id": pos_id, "code": code, "name": name, "dept": dept, "qty": qty, "price": price, "total": qty * price
        })
        self.refresh_cart_display()

    def remove_item_from_cart(self):
        selected = self.cart_tree.selection()
        if not selected:
            messagebox.showwarning("Selection Error", "Please select an item from the active cart to remove.")
            return
        idx = self.cart_tree.index(selected[0])
        del self.cart_items[idx]
        self.refresh_cart_display()

    def refresh_cart_display(self):
        for item in self.cart_tree.get_children():
            self.cart_tree.delete(item)

        grand_total = 0.0
        for item in self.cart_items:
            self.cart_tree.insert("", "end", values=(item["name"], item["qty"], f"{item['price']:,.2f}", f"{item['total']:,.2f}"))
            grand_total += item["total"]

        self.lbl_cart_total.config(text=f"Total: {config['app_settings']['currency']}{grand_total:,.2f}")
        self.entry_amount_paid.delete(0, tk.END)
        self.entry_amount_paid.insert(0, f"{grand_total:.2f}")

    def get_next_invoice_number(self):
        year_str = datetime.datetime.now().strftime("%Y")
        self.cursor.execute("SELECT invoice_no FROM sales WHERE user_id=? AND invoice_no LIKE ? ORDER BY id DESC LIMIT 1", (self.data_uid, f"INV-{year_str}-%"))
        last_inv = self.cursor.fetchone()

        if last_inv and last_inv[0]:
            try:
                parts = last_inv[0].split("-")
                seq = int(parts[-1]) + 1
                return f"INV-{year_str}-{seq:04d}"
            except Exception:
                pass
        return f"INV-{year_str}-0001"

    def process_multi_item_sale(self):
        if not self.check_permission("sell"):
            return

        if not self.cart_items:
            messagebox.showwarning("Empty Cart", "Please add items to the cart before completing the invoice.")
            return

        cust_name = self.entry_cust_name.get().strip() or "Walk-in Customer"
        cust_phone = self.entry_cust_phone.get().strip() or "N/A"
        payment_status = self.combo_payment_status.get()

        grand_total = sum(i["total"] for i in self.cart_items)
        if payment_status == "Paid":
            amount_paid = grand_total
        elif payment_status == "Unpaid":
            amount_paid = 0.0
        else:
            try:
                amount_paid = float(self.entry_amount_paid.get().strip().replace(",", ""))
            except ValueError:
                messagebox.showerror("Invalid Input", "Enter a valid numeric Amount Paid.")
                return
            if amount_paid <= 0 or amount_paid >= grand_total:
                messagebox.showerror("Invalid Input", "A partial payment must be more than zero and less than the invoice total.")
                return
        balance_due = grand_total - amount_paid

        uid = self.data_uid

        # Check every line before writing anything, so a shortfall cannot leave a half-saved invoice
        stock_rows = []
        for item in self.cart_items:
            self.cursor.execute("SELECT quantity, batch_no, date_planted, department FROM pos_products WHERE id=? AND user_id=?", (item["pos_id"], uid))
            p = self.cursor.fetchone()
            available = safe_int(p[0]) if p else 0
            if item["qty"] > available:
                messagebox.showerror("Stock Error", f"Insufficient stock for {item['name']}. Available: {available}")
                return
            stock_rows.append(p)

        invoice_no = self.get_next_invoice_number()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            for item, p in zip(self.cart_items, stock_rows):
                batch_no, date_planted, dept = p[1], p[2], p[3]
                # Each line carries its share of the payment; the invoice figure is the sum of its lines
                share = item["total"] / grand_total if grand_total else 1.0 / len(self.cart_items)

                self.cursor.execute("UPDATE pos_products SET quantity=quantity-? WHERE id=? AND user_id=?", (item["qty"], item["pos_id"], uid))
                self.cursor.execute(
                    """INSERT INTO sales (user_id, invoice_no, item_code, item_name, department, batch_no, date_planted, qty_sold, unit_price, total_price, payment_status, amount_paid, balance_due, customer_name, customer_phone, fulfillment_type, transaction_type, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Delivery', 'SALE', ?)""",
                    (uid, invoice_no, item["code"], item["name"], dept, batch_no, date_planted, item["qty"], item["price"], item["total"], payment_status, amount_paid * share, balance_due * share, cust_name, cust_phone, timestamp)
                )
            self.conn.commit()
        except sqlite3.Error as e:
            self.conn.rollback()
            messagebox.showerror("Database Error", f"The sale was not saved:\n{e}")
            return

        receipt_data = {
            "invoice_no": invoice_no, "timestamp": timestamp, "cust_name": cust_name, "cust_phone": cust_phone,
            "items": list(self.cart_items), "grand_total": grand_total, "payment_status": payment_status,
            "amount_paid": amount_paid, "balance_due": balance_due
        }

        self.show_invoice_popup(receipt_data)
        self.cart_items.clear()
        self.refresh_cart_display()
        self.entry_cust_name.delete(0, tk.END)
        self.entry_cust_phone.delete(0, tk.END)
        self.refresh_sales_page()

    # --- TAB 2: INVENTORY MANAGEMENT ---
    def setup_inventory_page(self):
        panel_w = 290 if self.is_compact else 320
        table_x = 310 if self.is_compact else 355
        table_w = 770 if self.is_compact else 930

        outer_container = tk.LabelFrame(self.inventory_page, text="Company Procurement / Inventory", bg=BG, fg=TEXT_MUTED, font=(FONT, 9, "bold"), padx=5, pady=5)
        outer_container.place(x=15, y=15, width=panel_w, height=575)

        canvas = tk.Canvas(outer_container, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer_container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=BG)

        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_wheel(e):
            # bind_all fires on every tab, so only scroll while the pointer is over this form
            try:
                under = str(self.root.winfo_containing(e.x_root, e.y_root) or "")
            except KeyError:
                return
            if under.startswith(str(outer_container)):
                canvas.yview_scroll(int(-1*(e.delta/120)), "units")

        canvas.bind_all("<MouseWheel>", on_wheel)

        GlassButton(scrollable_frame, text="📷 Scan Barcode via Camera", bg=SURFACE_ALT, hover_bg=PRIMARY, command=lambda: self.open_barcode_scanner("inventory")).pack(fill="x", pady=(0, 6), padx=5)

        fields = [
            ("Item Code:", "entry_code"), ("Item Name:", "entry_name"), ("Category (e.g. Seeds, Fert):", "entry_cat"),
            ("Batch No:", "entry_batch"), ("Date Planted (YYYY-MM-DD):", "entry_planted"), ("Supplier Name:", "entry_supplier"),
            ("Invoice Number:", "entry_invoice"), ("Purchase Date (YYYY-MM-DD):", "entry_pdate"), ("Quantity Bought:", "entry_qty"),
            ("Unit Cost:", "entry_ucost"), ("Selling Price (blank = Unit Cost):", "entry_price"),
            ("Expiry Date (YYYY-MM-DD):", "entry_expiry")
        ]

        self.inv_entries = {}
        for label, attr in fields:
            tk.Label(scrollable_frame, text=label, bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w", padx=5)
            ent = tk.Entry(scrollable_frame, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
            if attr == "entry_pdate":
                ent.insert(0, datetime.datetime.now().strftime("%Y-%m-%d"))
            ent.pack(fill="x", pady=1, padx=5, ipady=2)
            setattr(self, attr, ent)

        tk.Label(scrollable_frame, text="Department:", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w", padx=5)
        self.combo_inv_dept = ttk.Combobox(scrollable_frame, state="readonly", font=(FONT, 9))
        self.combo_inv_dept.pack(fill="x", pady=1, padx=5)

        btn_frame = tk.Frame(scrollable_frame, bg=BG)
        btn_frame.pack(fill="x", pady=8, padx=5)

        GlassButton(btn_frame, text="Log Purchase & Add", bg=SUCCESS, hover_bg=SUCCESS_HOVER, command=self.add_product).pack(fill="x", pady=2)
        GlassButton(btn_frame, text="✏ Update Selected Details", bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.update_product).pack(fill="x", pady=2)
        GlassButton(btn_frame, text="🔁 Transfer / Use", bg=PRIMARY, hover_bg=PRIMARY_HOVER, command=self.open_transfer_dialog).pack(fill="x", pady=2)
        GlassButton(btn_frame, text="Delete Selected", bg=DANGER, hover_bg=DANGER_HOVER, command=self.delete_product).pack(fill="x", pady=2)

        right_frame = tk.Frame(self.inventory_page, bg=BG)
        right_frame.place(x=table_x, y=15, width=table_w, height=575)

        inv_filter_bar = tk.Frame(right_frame, bg=BG)
        inv_filter_bar.pack(fill="x", pady=(0, 5))
        tk.Label(inv_filter_bar, text="Filter Dept:", bg=BG, fg=TEXT, font=(FONT, 9, "bold")).pack(side="left", padx=(0, 5))
        self.combo_inv_filter = ttk.Combobox(inv_filter_bar, state="readonly", font=(FONT, 9), width=18)
        self.combo_inv_filter.pack(side="left")
        self.combo_inv_filter.set("All Departments")
        self.combo_inv_filter.bind("<<ComboboxSelected>>", lambda e: self.load_inventory_data())

        cols = ("ID", "Code", "Name", "Category", "Dept", "Supplier", "Invoice", "Qty", "Unit Cost", "Sell Price", "Expiry")
        self.inv_tree = ttk.Treeview(right_frame, columns=cols, show="headings", selectmode="browse")
        for col in cols:
            self.inv_tree.heading(col, text=col)

        self.inv_tree.column("ID", width=25, anchor="center")
        self.inv_tree.column("Code", width=50, anchor="center")
        self.inv_tree.column("Name", width=110)
        self.inv_tree.column("Category", width=70)
        self.inv_tree.column("Dept", width=70)
        self.inv_tree.column("Supplier", width=100)
        self.inv_tree.column("Invoice", width=75, anchor="center")
        self.inv_tree.column("Qty", width=40, anchor="center")
        self.inv_tree.column("Unit Cost", width=70, anchor="e")
        self.inv_tree.column("Sell Price", width=70, anchor="e")
        self.inv_tree.column("Expiry", width=80, anchor="center")

        inv_scroll = ttk.Scrollbar(right_frame, orient="vertical", command=self.inv_tree.yview)
        self.inv_tree.configure(yscrollcommand=inv_scroll.set)
        self.inv_tree.pack(side="left", fill="both", expand=True)
        inv_scroll.pack(side="right", fill="y")
        self.inv_tree.bind("<<TreeviewSelect>>", self.on_inv_select)

    def load_inventory_data(self):
        for item in self.inv_tree.get_children():
            self.inv_tree.delete(item)

        selected_dept = self.combo_inv_filter.get()
        uid = self.data_uid
        if selected_dept == "All Departments" or not selected_dept:
            self.cursor.execute("SELECT id, item_code, name, category, department, supplier_name, invoice_no, quantity, unit_cost, price, expiry_date FROM products WHERE user_id=? ORDER BY id ASC", (uid,))
        else:
            self.cursor.execute("SELECT id, item_code, name, category, department, supplier_name, invoice_no, quantity, unit_cost, price, expiry_date FROM products WHERE user_id=? AND department=? ORDER BY id ASC", (uid, selected_dept))

        for row in self.cursor.fetchall():
            r = list(row)
            r[7] = safe_int(r[7])
            r[8] = f"{safe_float(r[8]):,.2f}"
            r[9] = f"{safe_float(r[9]):,.2f}"
            self.inv_tree.insert("", "end", values=r)

    def add_product(self):
        if not self.check_permission("procure"):
            return

        code = self.entry_code.get().strip()
        name = self.entry_name.get().strip()
        category = self.entry_cat.get().strip() or "General"
        dept = self.combo_inv_dept.get().strip() or "General"
        batch_no = self.entry_batch.get().strip() or "N/A"
        date_planted = self.entry_planted.get().strip() or "N/A"
        supplier = self.entry_supplier.get().strip() or "N/A"
        invoice = self.entry_invoice.get().strip() or "N/A"
        pdate = self.entry_pdate.get().strip() or datetime.datetime.now().strftime("%Y-%m-%d")
        qty_str = self.entry_qty.get().strip()
        ucost_str = self.entry_ucost.get().strip()
        price_str = self.entry_price.get().strip()
        expiry = self.entry_expiry.get().strip() or "N/A"

        if not code or not name or not qty_str or not ucost_str:
            messagebox.showwarning("Error", "Please fill in Code, Name, Quantity, and Unit Cost.")
            return

        try:
            qty = int(qty_str)
            unit_cost = float(ucost_str.replace(",", ""))
            price = float(price_str.replace(",", "")) if price_str else unit_cost
        except ValueError:
            messagebox.showerror("Error", "Quantity must be a whole number; Unit Cost and Selling Price must be numeric.")
            return

        if qty <= 0 or unit_cost < 0 or price < 0:
            messagebox.showerror("Error", "Quantity must be above zero and prices cannot be negative.")
            return

        total_cost = qty * unit_cost
        uid = self.data_uid

        self.cursor.execute("SELECT id, name FROM products WHERE user_id=? AND item_code=?", (uid, code))
        existing = self.cursor.fetchone()
        if existing and not messagebox.askyesno("Item Code Exists", f"Item code '{code}' is already in your inventory as '{existing[1]}'.\n\nAdd these {qty} unit(s) to the existing stock?"):
            return

        try:
            if existing:
                self.cursor.execute(
                    "UPDATE products SET quantity=quantity+?, price=?, unit_cost=?, batch_no=?, supplier_name=?, invoice_no=?, purchase_date=?, expiry_date=? WHERE id=? AND user_id=?",
                    (qty, price, unit_cost, batch_no, supplier, invoice, pdate, expiry, existing[0], uid)
                )
            else:
                self.cursor.execute("INSERT INTO products (user_id, item_code, name, category, department, batch_no, date_planted, quantity, price, supplier_name, invoice_no, purchase_date, unit_cost, expiry_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (uid, code, name, category, dept, batch_no, date_planted, qty, price, supplier, invoice, pdate, unit_cost, expiry))
            self.cursor.execute("INSERT INTO procurements (user_id, item_code, name, category, department, supplier_name, invoice_no, purchase_date, quantity, unit_cost, total_cost, expiry_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (uid, code, name, category, dept, supplier, invoice, pdate, qty, unit_cost, total_cost, expiry))
            self.cursor.execute("UPDATE pos_products SET price=? WHERE user_id=? AND item_code=?", (price, uid, code))
            self.conn.commit()
        except sqlite3.Error as e:
            self.conn.rollback()
            messagebox.showerror("Database Error", f"The purchase was not saved:\n{e}")
            return

        self.load_inventory_data()
        self.clear_inv_entries()
        messagebox.showinfo("Success", f"Purchase logged for '{name}' and added to Inventory!")

    def update_product(self):
        if not self.check_permission("procure"):
            return

        selected = self.inv_tree.selection()
        if not selected:
            messagebox.showwarning("Selection Error", "Please select an inventory item to update.")
            return

        name = self.entry_name.get().strip()
        if not name:
            messagebox.showwarning("Error", "Item Name is required.")
            return

        try:
            unit_cost = float(self.entry_ucost.get().strip().replace(",", ""))
            price_str = self.entry_price.get().strip()
            price = float(price_str.replace(",", "")) if price_str else unit_cost
        except ValueError:
            messagebox.showerror("Error", "Unit Cost and Selling Price must be numeric.")
            return

        if unit_cost < 0 or price < 0:
            messagebox.showerror("Error", "Prices cannot be negative.")
            return

        item_id = self.inv_tree.item(selected[0])["values"][0]
        uid = self.data_uid
        category = self.entry_cat.get().strip() or "General"
        dept = self.combo_inv_dept.get().strip() or "General"

        self.cursor.execute("SELECT item_code FROM products WHERE id=? AND user_id=?", (item_id, uid))
        found = self.cursor.fetchone()
        if not found:
            return

        # Item code and quantity stay as they are: stock only moves through purchases, transfers and sales
        try:
            self.cursor.execute(
                "UPDATE products SET name=?, category=?, department=?, batch_no=?, date_planted=?, price=?, supplier_name=?, invoice_no=?, purchase_date=?, unit_cost=?, expiry_date=? WHERE id=? AND user_id=?",
                (
                    name, category, dept, self.entry_batch.get().strip() or "N/A", self.entry_planted.get().strip() or "N/A", price,
                    self.entry_supplier.get().strip() or "N/A", self.entry_invoice.get().strip() or "N/A",
                    self.entry_pdate.get().strip() or "N/A", unit_cost, self.entry_expiry.get().strip() or "N/A", item_id, uid
                )
            )
            self.cursor.execute("UPDATE pos_products SET name=?, category=?, department=?, price=? WHERE user_id=? AND item_code=?", (name, category, dept, price, uid, found[0]))
            self.conn.commit()
        except sqlite3.Error as e:
            self.conn.rollback()
            messagebox.showerror("Database Error", f"The item was not updated:\n{e}")
            return

        self.load_inventory_data()
        self.clear_inv_entries()
        messagebox.showinfo("Updated", f"Details saved for '{name}'.")

    def delete_product(self):
        if not self.check_permission("delete"):
            return

        selected = self.inv_tree.selection()
        if not selected:
            messagebox.showwarning("Selection Error", "Please select an inventory item to delete.")
            return

        item_id = self.inv_tree.item(selected[0])["values"][0]
        if messagebox.askyesno("Confirm Delete", "Delete product from inventory permanently?"):
            self.cursor.execute("DELETE FROM products WHERE id=? AND user_id=?", (item_id, self.data_uid))
            self.conn.commit()
            self.load_inventory_data()
            self.clear_inv_entries()

    def on_inv_select(self, event):
        selected = self.inv_tree.selection()
        if selected:
            row_id = self.inv_tree.item(selected[0])["values"][0]
            self.cursor.execute(f"SELECT {self.INV_FORM_COLS} FROM products WHERE id=? AND user_id=?", (row_id, self.data_uid))
            p = self.cursor.fetchone()
            if p:
                self.fill_inv_form(p)

    def clear_inv_entries(self):
        for attr in ["entry_code", "entry_name", "entry_cat", "entry_batch", "entry_planted", "entry_supplier", "entry_invoice", "entry_pdate", "entry_qty", "entry_ucost", "entry_price", "entry_expiry"]:
            getattr(self, attr).delete(0, tk.END)
        self.entry_pdate.insert(0, datetime.datetime.now().strftime("%Y-%m-%d"))
        self.combo_inv_dept.set("")

    def open_transfer_dialog(self):
        if not self.check_permission("transfer"):
            return

        selected = self.inv_tree.selection()
        if not selected:
            messagebox.showwarning("Selection Error", "Please select an inventory item to transfer/use.")
            return

        row_id = self.inv_tree.item(selected[0])["values"][0]
        self.cursor.execute("SELECT id, item_code, name, category, department, batch_no, date_planted, quantity, price FROM products WHERE id=? AND user_id=?", (row_id, self.data_uid))
        p = self.cursor.fetchone()
        if not p:
            return

        item_id, code, name, category, dept, batch_no, date_planted, current_inv_qty, unit_price = p
        current_inv_qty, unit_price = safe_int(current_inv_qty), safe_float(unit_price)

        dialog = tk.Toplevel(self.root)
        dialog.title("Transfer / Use Inventory Item")
        dialog.geometry("400x420")
        dialog.config(bg=BG)
        dialog.grab_set()

        tk.Label(dialog, text=f"Product: {name} (Code: {code})", font=(FONT, 10, "bold"), bg=BG, fg=ACCENT).pack(pady=(12, 3))
        tk.Label(dialog, text=f"Available Qty: {current_inv_qty}", font=(FONT, 9), bg=BG, fg=TEXT_MUTED).pack(pady=(0, 10))

        tk.Label(dialog, text="Destination:", bg=BG, fg=TEXT).pack(anchor="w", padx=25)
        combo_destination = ttk.Combobox(dialog, state="readonly", font=(FONT, 9))
        combo_destination["values"] = ["Farm Use", "Stock Listing"]
        combo_destination.set("Farm Use")
        combo_destination.pack(fill="x", padx=25, pady=(1, 8))

        tk.Label(dialog, text="Quantity to Transfer:", bg=BG, fg=TEXT).pack(anchor="w", padx=25)
        entry_transfer_qty = tk.Entry(dialog, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        entry_transfer_qty.pack(fill="x", padx=25, pady=(1, 8), ipady=2)
        entry_transfer_qty.insert(0, "1")

        tk.Label(dialog, text="Operator / Recipient Name:", bg=BG, fg=TEXT).pack(anchor="w", padx=25)
        entry_operator = tk.Entry(dialog, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        entry_operator.pack(fill="x", padx=25, pady=(1, 8), ipady=2)

        tk.Label(dialog, text="Phone Number:", bg=BG, fg=TEXT).pack(anchor="w", padx=25)
        entry_phone = tk.Entry(dialog, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        entry_phone.pack(fill="x", padx=25, pady=(1, 12), ipady=2)

        def execute_transfer():
            dest = combo_destination.get()
            try:
                qty_trans = int(entry_transfer_qty.get().strip())
            except ValueError:
                messagebox.showerror("Error", "Enter valid quantity.")
                return

            if qty_trans <= 0 or qty_trans > current_inv_qty:
                messagebox.showerror("Error", f"Quantity must be between 1 and {current_inv_qty}.")
                return

            op_name = entry_operator.get().strip() or "Farm Internal"
            phone = entry_phone.get().strip() or "N/A"
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            uid = self.data_uid
            invoice_no = self.get_next_invoice_number() if dest == "Farm Use" else None

            try:
                self.cursor.execute("UPDATE products SET quantity=quantity-? WHERE id=? AND user_id=?", (qty_trans, item_id, uid))
                if dest == "Farm Use":
                    self.cursor.execute("INSERT INTO sales (user_id, invoice_no, item_code, item_name, department, batch_no, date_planted, qty_sold, unit_price, total_price, payment_status, amount_paid, balance_due, customer_name, customer_phone, fulfillment_type, transaction_type, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 'INTERNAL', 0.0, 0.0, ?, ?, 'Farm Pickup', 'FARM_USE', ?)", (uid, invoice_no, code, name, dept, batch_no, date_planted, qty_trans, unit_price, op_name, phone, timestamp))
                else:
                    self.cursor.execute("SELECT id FROM pos_products WHERE user_id=? AND item_code=?", (uid, code))
                    pos_item = self.cursor.fetchone()
                    if pos_item:
                        self.cursor.execute("UPDATE pos_products SET quantity=quantity+?, price=? WHERE id=?", (qty_trans, unit_price, pos_item[0]))
                    else:
                        self.cursor.execute("INSERT INTO pos_products (user_id, item_code, name, category, department, batch_no, date_planted, quantity, price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (uid, code, name, category, dept, batch_no, date_planted, qty_trans, unit_price))
                self.conn.commit()
            except sqlite3.Error as e:
                self.conn.rollback()
                messagebox.showerror("Database Error", f"The transfer was not saved:\n{e}")
                return

            if dest == "Farm Use":
                dialog.destroy()
                self.load_inventory_data()
                self.show_invoice_popup({
                    "invoice_no": invoice_no, "timestamp": timestamp, "cust_name": op_name, "cust_phone": phone,
                    "items": [{"name": name, "qty": qty_trans, "price": unit_price, "total": 0.0}],
                    "grand_total": 0.0, "payment_status": "INTERNAL", "amount_paid": 0.0, "balance_due": 0.0, "is_farm_use": True
                })
            else:
                dialog.destroy()
                self.load_inventory_data()
                messagebox.showinfo("Success", f"Transferred {qty_trans} unit(s) to Sales & POS Dashboard!")

        GlassButton(dialog, text="Confirm Transfer", bg=SUCCESS, hover_bg=SUCCESS_HOVER, command=execute_transfer).pack(fill="x", padx=25, pady=5)

    # --- TAB 3: PROCUREMENTS PAGE ---
    def setup_procurements_page(self):
        filter_bar = tk.Frame(self.procurements_page, bg=BG)
        filter_bar.pack(fill="x", padx=15, pady=10)

        tk.Label(filter_bar, text="Filter Category:", bg=BG, fg=TEXT, font=(FONT, 9, "bold")).pack(side="left", padx=(0, 6))
        self.combo_procure_cat = ttk.Combobox(filter_bar, state="readonly", font=(FONT, 9), width=18)
        self.combo_procure_cat.pack(side="left", padx=(0, 15))
        self.combo_procure_cat.set("All Categories")
        self.combo_procure_cat.bind("<<ComboboxSelected>>", lambda e: self.load_procurements_data())

        GlassButton(filter_bar, text="📊 Export Summary Report", bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.export_procurement_summary).pack(side="right", padx=5)

        table_frame = tk.Frame(self.procurements_page, bg=BG)
        table_frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        cols = ("ID", "Date", "Supplier", "Invoice No", "Product Name", "Category", "Dept", "Qty", "Unit Cost", "Total Cost", "Expiry")
        self.proc_tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="extended")
        for col in cols:
            self.proc_tree.heading(col, text=col)

        self.proc_tree.column("ID", width=30, anchor="center")
        self.proc_tree.column("Date", width=85, anchor="center")
        self.proc_tree.column("Supplier", width=110)
        self.proc_tree.column("Invoice No", width=80, anchor="center")
        self.proc_tree.column("Product Name", width=110)
        self.proc_tree.column("Category", width=80)
        self.proc_tree.column("Dept", width=70)
        self.proc_tree.column("Qty", width=45, anchor="center")
        self.proc_tree.column("Unit Cost", width=70, anchor="e")
        self.proc_tree.column("Total Cost", width=75, anchor="e")
        self.proc_tree.column("Expiry", width=80, anchor="center")

        proc_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.proc_tree.yview)
        self.proc_tree.configure(yscrollcommand=proc_scroll.set)
        self.proc_tree.pack(side="left", fill="both", expand=True)
        proc_scroll.pack(side="right", fill="y")

    def load_procurements_data(self):
        for item in self.proc_tree.get_children():
            self.proc_tree.delete(item)

        cat_filter = self.combo_procure_cat.get()
        curr = config["app_settings"]["currency"]
        uid = self.data_uid

        if cat_filter == "All Categories" or not cat_filter:
            self.cursor.execute("SELECT id, purchase_date, supplier_name, invoice_no, name, category, department, quantity, unit_cost, total_cost, expiry_date FROM procurements WHERE user_id=? ORDER BY id ASC", (uid,))
        else:
            self.cursor.execute("SELECT id, purchase_date, supplier_name, invoice_no, name, category, department, quantity, unit_cost, total_cost, expiry_date FROM procurements WHERE user_id=? AND category=? ORDER BY id ASC", (uid, cat_filter))

        for r in self.cursor.fetchall():
            row = list(r)
            row[7] = safe_int(row[7])
            row[8] = f"{curr}{safe_float(row[8]):,.2f}"
            row[9] = f"{curr}{safe_float(row[9]):,.2f}"
            self.proc_tree.insert("", "end", values=row)

    def export_procurement_summary(self):
        selected_items = self.proc_tree.selection()
        rows = [tree_row(self.proc_tree, item_id) for item_id in (selected_items if selected_items else self.proc_tree.get_children())]

        if not rows:
            messagebox.showwarning("Warning", "No procurement records found to export.")
            return

        category_totals = {}
        grand_total = 0.0
        for r in rows:
            cat = str(r[5])
            cost_val = safe_float(str(r[9]).replace(config["app_settings"]["currency"], "").replace(",", ""))
            category_totals[cat] = category_totals.get(cat, 0.0) + cost_val
            grand_total += cost_val

        fmt = simpledialog.askstring("Export Format", "Choose export format (type 'pdf' or 'csv'):", initialvalue="pdf")
        if not fmt:
            return
        fmt = fmt.strip().lower()
        if fmt not in ("pdf", "csv"):
            messagebox.showwarning("Export Format", "Please type either 'pdf' or 'csv'.")
            return

        if fmt == "csv":
            fpath = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV Files", "*.csv")], title="Save Procurements Summary CSV")
            if fpath:
                with open(fpath, mode="w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["ID", "Date", "Supplier", "Invoice No", "Product Name", "Category", "Dept", "Qty", "Unit Cost", "Total Cost", "Expiry"])
                    for r in rows:
                        writer.writerow(r)
                    writer.writerow([])
                    writer.writerow(["CATEGORY SPENDING BREAKDOWN"])
                    for cat, total in category_totals.items():
                        writer.writerow([cat, f"{total:,.2f}"])
                    writer.writerow(["GRAND TOTAL", f"{grand_total:,.2f}"])
                messagebox.showinfo("Exported", f"Procurement summary exported successfully to:\n{fpath}")

        elif fmt == "pdf":
            if not REPORTLAB_AVAILABLE:
                messagebox.showerror("Error", "ReportLab not installed.")
                return

            fpath = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF Documents", "*.pdf")], title="Save Procurements Summary PDF")
            if fpath:
                c = canvas.Canvas(fpath, pagesize=letter)
                w, h = letter

                c.setFont("Helvetica-Bold", 14)
                c.drawString(40, h - 40, "Company Procurement & Category Spending Report")
                c.setFont("Helvetica", 9)
                c.drawString(40, h - 55, f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                c.line(40, h - 65, w - 40, h - 65)

                y = h - 85
                c.setFont("Helvetica-Bold", 8)
                headers = ["Date", "Supplier", "Invoice", "Product", "Category", "Qty", "Total Cost"]
                x_pos = [40, 110, 200, 275, 360, 440, 485]

                for head, xp in zip(headers, x_pos):
                    c.drawString(xp, y, head)
                y -= 10
                c.line(40, y, w - 40, y)
                y -= 15

                c.setFont("Helvetica", 8)
                for r in rows:
                    if y < 100:
                        c.showPage()
                        y = h - 50
                    vals = [str(r[1]), str(r[2])[:14], str(r[3]), str(r[4])[:14], str(r[5]), str(r[7]), str(r[9])]
                    for val, xp in zip(vals, x_pos):
                        c.drawString(xp, y, val)
                    y -= 14

                y -= 15
                c.line(40, y, w - 40, y)
                y -= 20
                c.setFont("Helvetica-Bold", 10)
                c.drawString(40, y, "SPENDING SUMMARY BY CATEGORY:")
                y -= 18

                c.setFont("Helvetica", 9)
                for cat, total in category_totals.items():
                    c.drawString(50, y, f"• {cat}: {config['app_settings']['currency']}{total:,.2f}")
                    y -= 15

                y -= 5
                c.setFont("Helvetica-Bold", 10)
                c.drawString(50, y, f"GRAND TOTAL SPENT: {config['app_settings']['currency']}{grand_total:,.2f}")
                c.save()
                messagebox.showinfo("Exported", f"Procurement PDF Report saved to:\n{fpath}")

    # --- TAB 4: HR & PAYROLL MANAGEMENT PAGE ---
    def setup_hr_page(self):
        top_bar = tk.Frame(self.hr_page, bg=BG)
        top_bar.pack(fill="x", padx=15, pady=10)

        tk.Label(top_bar, text="👥 Human Resources & Labor Cost Center", bg=BG, fg=ACCENT, font=(FONT, 11, "bold")).pack(side="left")

        GlassButton(top_bar, text="📊 Monthly Summary", bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.open_monthly_summary_dialog).pack(side="right", padx=5)
        GlassButton(top_bar, text="⚙️ HR Settings", bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.open_hr_settings_dialog).pack(side="right", padx=5)
        GlassButton(top_bar, text="➕ Register Employee", bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.open_register_employee_dialog).pack(side="right", padx=5)
        GlassButton(top_bar, text="🖨 Print Selected Payslips", bg=SUCCESS, hover_bg=SUCCESS_HOVER, command=self.print_selected_payslips_pdf).pack(side="right", padx=5)
        GlassButton(top_bar, text="💰 Compute Payslip", bg=PRIMARY, hover_bg=PRIMARY_HOVER, command=self.open_compute_payroll_dialog).pack(side="right", padx=5)

        content_body = tk.Frame(self.hr_page, bg=BG)
        content_body.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        left_emp = tk.LabelFrame(content_body, text="Registered Employees", bg=BG, fg=TEXT_MUTED, font=(FONT, 9, "bold"), padx=5, pady=5)
        left_emp.pack(side="left", fill="both", expand=True, padx=(0, 8))

        emp_cols = ("ID", "Full Name", "NRC", "Type", "Basic Salary", "Days/Wk", "Start Date")
        self.emp_tree = ttk.Treeview(left_emp, columns=emp_cols, show="headings", selectmode="browse")
        for col in emp_cols:
            self.emp_tree.heading(col, text=col)
        self.emp_tree.column("ID", width=25, anchor="center")
        self.emp_tree.column("Full Name", width=110)
        self.emp_tree.column("NRC", width=85, anchor="center")
        self.emp_tree.column("Type", width=75, anchor="center")
        self.emp_tree.column("Basic Salary", width=75, anchor="e")
        self.emp_tree.column("Days/Wk", width=50, anchor="center")
        self.emp_tree.column("Start Date", width=80, anchor="center")

        emp_scroll = ttk.Scrollbar(left_emp, orient="vertical", command=self.emp_tree.yview)
        self.emp_tree.configure(yscrollcommand=emp_scroll.set)
        self.emp_tree.pack(side="left", fill="both", expand=True)
        emp_scroll.pack(side="right", fill="y")

        right_pay = tk.LabelFrame(content_body, text="Computed Payroll & Salary History (Multi-Select for PDF)", bg=BG, fg=TEXT_MUTED, font=(FONT, 9, "bold"), padx=5, pady=5)
        right_pay.pack(side="right", fill="both", expand=True)

        pay_cols = ("ID", "Month", "Employee", "Basic", "Exp Days", "Work Days", "NAPSA", "NHIMA", "Net Pay")
        self.pay_tree = ttk.Treeview(right_pay, columns=pay_cols, show="headings", selectmode="extended")
        for col in pay_cols:
            self.pay_tree.heading(col, text=col)
        self.pay_tree.column("ID", width=30, anchor="center")
        self.pay_tree.column("Month", width=65, anchor="center")
        self.pay_tree.column("Employee", width=90)
        self.pay_tree.column("Basic", width=60, anchor="e")
        self.pay_tree.column("Exp Days", width=45, anchor="center")
        self.pay_tree.column("Work Days", width=45, anchor="center")
        self.pay_tree.column("NAPSA", width=55, anchor="e")
        self.pay_tree.column("NHIMA", width=55, anchor="e")
        self.pay_tree.column("Net Pay", width=65, anchor="e")

        pay_scroll = ttk.Scrollbar(right_pay, orient="vertical", command=self.pay_tree.yview)
        self.pay_tree.configure(yscrollcommand=pay_scroll.set)
        self.pay_tree.pack(side="left", fill="both", expand=True)
        pay_scroll.pack(side="right", fill="y")

    def load_employees_data(self):
        for item in self.emp_tree.get_children():
            self.emp_tree.delete(item)
        for item in self.pay_tree.get_children():
            self.pay_tree.delete(item)

        curr = config["app_settings"]["currency"]
        uid = self.data_uid

        self.cursor.execute("SELECT id, full_name, nrc_number, employment_type, basic_salary, expected_days_week, start_date FROM employees WHERE user_id=? ORDER BY id ASC", (uid,))
        for r in self.cursor.fetchall():
            row = list(r)
            row[4] = f"{curr}{safe_float(row[4]):,.2f}"
            self.emp_tree.insert("", "end", values=row)

        self.cursor.execute("SELECT id, pay_month, employee_name, basic_salary, expected_days, worked_days, napsa_amt, nhima_amt, net_payable FROM payroll WHERE user_id=? ORDER BY id DESC", (uid,))
        for r in self.cursor.fetchall():
            row = list(r)
            row[3] = f"{curr}{safe_float(row[3]):,.2f}"
            row[6] = f"{curr}{safe_float(row[6]):,.2f}"
            row[7] = f"{curr}{safe_float(row[7]):,.2f}"
            row[8] = f"{curr}{safe_float(row[8]):,.2f}"
            self.pay_tree.insert("", "end", values=row)

    def open_hr_settings_dialog(self):
        if not self.check_permission("delete"):
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("HR & Statutory Configuration")
        dialog.geometry("400x440")
        dialog.config(bg=BG)
        dialog.grab_set()

        uid = self.data_uid
        self.cursor.execute("SELECT non_working_days, napsa_pct, nhima_pct, napsa_bearer, nhima_bearer FROM hr_settings WHERE user_id=?", (uid,))
        settings = self.cursor.fetchone()
        nw_days = settings[0] if settings else "Saturday,Sunday"
        napsa = settings[1] if settings else 5.0
        nhima = settings[2] if settings else 2.0
        napsa_b = settings[3] if settings else "employee"
        nhima_b = settings[4] if settings else "employee"

        tk.Label(dialog, text="⚙️ HR & Statutory Settings", font=(FONT, 11, "bold"), bg=BG, fg=ACCENT).pack(pady=(15, 10))

        form = tk.Frame(dialog, bg=BG, padx=25)
        form.pack(fill="both", expand=True)

        tk.Label(form, text="Non-Working Days (comma separated):", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
        ent_nw = tk.Entry(form, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        ent_nw.insert(0, nw_days)
        ent_nw.pack(fill="x", pady=(1, 8), ipady=2)

        tk.Label(form, text="NAPSA Deduction Rate (%):", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
        ent_napsa = tk.Entry(form, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        ent_napsa.insert(0, str(napsa))
        ent_napsa.pack(fill="x", pady=(1, 8), ipady=2)

        tk.Label(form, text="NAPSA Paid By:", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
        combo_napsa_b = ttk.Combobox(form, state="readonly", font=(FONT, 9))
        combo_napsa_b["values"] = ["employee", "employer"]
        combo_napsa_b.set(napsa_b)
        combo_napsa_b.pack(fill="x", pady=(1, 8))

        tk.Label(form, text="NHIMA Deduction Rate (%):", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
        ent_nhima = tk.Entry(form, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        ent_nhima.insert(0, str(nhima))
        ent_nhima.pack(fill="x", pady=(1, 8), ipady=2)

        tk.Label(form, text="NHIMA Paid By:", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
        combo_nhima_b = ttk.Combobox(form, state="readonly", font=(FONT, 9))
        combo_nhima_b["values"] = ["employee", "employer"]
        combo_nhima_b.set(nhima_b)
        combo_nhima_b.pack(fill="x", pady=(1, 15))

        def save_settings():
            try:
                n_days = ent_nw.get().strip()
                n_val = safe_float(ent_napsa.get().strip())
                h_val = safe_float(ent_nhima.get().strip())
                n_bear = combo_napsa_b.get()
                h_bear = combo_nhima_b.get()

                self.cursor.execute("UPDATE hr_settings SET non_working_days=?, napsa_pct=?, nhima_pct=?, napsa_bearer=?, nhima_bearer=? WHERE user_id=?", (n_days, n_val, h_val, n_bear, h_bear, uid))
                self.conn.commit()
                messagebox.showinfo("Success", "HR & Statutory settings updated successfully!")
                dialog.destroy()
            except ValueError:
                messagebox.showerror("Error", "Enter valid percentage numbers.")

        GlassButton(dialog, text="Save Settings", bg=SUCCESS, hover_bg=SUCCESS_HOVER, command=save_settings).pack(fill="x", padx=25, pady=(0, 20))

    def open_register_employee_dialog(self):
        if not self.check_permission("procure"):
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Register New Employee")
        dialog.geometry("420x500")
        dialog.config(bg=BG)
        dialog.grab_set()

        tk.Label(dialog, text="👤 Register Employee Profile", font=(FONT, 11, "bold"), bg=BG, fg=ACCENT).pack(pady=(15, 10))

        form = tk.Frame(dialog, bg=BG, padx=25)
        form.pack(fill="both", expand=True)

        fields = [
            ("Full Name:", "ent_name"), ("NRC Number:", "ent_nrc"), (f"Basic Salary ({config['app_settings']['currency'].strip()}):", "ent_salary"),
            ("Address:", "ent_address"), ("Age:", "ent_age"), ("Time Started Work (YYYY-MM-DD):", "ent_start"),
            ("Expected Work Days per Week:", "ent_days_week")
        ]

        entries = {}
        for label, key in fields:
            tk.Label(form, text=label, bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
            ent = tk.Entry(form, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
            if key == "ent_start":
                ent.insert(0, datetime.datetime.now().strftime("%Y-%m-%d"))
            elif key == "ent_days_week":
                ent.insert(0, "5")
            ent.pack(fill="x", pady=(1, 5), ipady=2)
            entries[key] = ent

        tk.Label(form, text="Employment Status:", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
        combo_type = ttk.Combobox(form, state="readonly", font=(FONT, 9))
        combo_type["values"] = ["Permanent", "Casual Worker"]
        combo_type.set("Permanent")
        combo_type.pack(fill="x", pady=(1, 10))

        def save_employee():
            name = entries["ent_name"].get().strip()
            nrc = entries["ent_nrc"].get().strip()
            salary_str = entries["ent_salary"].get().strip()
            address = entries["ent_address"].get().strip() or "N/A"
            age_str = entries["ent_age"].get().strip()
            start_date = entries["ent_start"].get().strip()
            days_wk_str = entries["ent_days_week"].get().strip()
            emp_type = combo_type.get()

            if not name or not nrc or not salary_str:
                messagebox.showwarning("Validation Error", "Full Name, NRC Number, and Basic Salary are required.")
                return

            try:
                salary = float(salary_str.replace(",", ""))
                age = int(age_str) if age_str else 0
                days_wk = int(days_wk_str) if days_wk_str else 5
                uid = self.data_uid

                self.cursor.execute(
                    """INSERT INTO employees (user_id, full_name, nrc_number, basic_salary, employment_type, address, age, start_date, expected_days_week)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (uid, name, nrc, salary, emp_type, address, age, start_date, days_wk)
                )
                self.conn.commit()
                messagebox.showinfo("Success", f"Employee '{name}' registered successfully!")
                dialog.destroy()
                self.load_employees_data()
            except ValueError:
                messagebox.showerror("Error", "Please enter valid numeric values for Salary, Age, and Days per Week.")

        GlassButton(dialog, text="Save Employee Profile", bg=SUCCESS, hover_bg=SUCCESS_HOVER, command=save_employee).pack(fill="x", padx=25, pady=(0, 20))

    def open_compute_payroll_dialog(self):
        if not self.check_permission("procure"):
            return

        uid = self.data_uid
        self.cursor.execute("SELECT id, full_name, basic_salary, expected_days_week FROM employees WHERE user_id=?", (uid,))
        employees = self.cursor.fetchall()

        if not employees:
            messagebox.showwarning("Notice", "No registered employees found. Please register an employee first.")
            return

        self.cursor.execute("SELECT non_working_days, napsa_pct, nhima_pct, napsa_bearer, nhima_bearer FROM hr_settings WHERE user_id=?", (uid,))
        hr_set = self.cursor.fetchone()
        nw_days_list = [d.strip().lower() for d in (hr_set[0] if hr_set else "Saturday,Sunday").split(",")]
        napsa_rate = (hr_set[1] if hr_set else 5.0) / 100.0
        nhima_rate = (hr_set[2] if hr_set else 2.0) / 100.0
        napsa_bearer = hr_set[3] if hr_set else "employee"
        nhima_bearer = hr_set[4] if hr_set else "employee"

        dialog = tk.Toplevel(self.root)
        dialog.title("Compute Monthly Payslip & Payroll")
        dialog.geometry("540x720")
        dialog.config(bg=BG)
        dialog.grab_set()

        tk.Label(dialog, text="💰 Interactive Monthly Payslip Calculator", font=(FONT, 11, "bold"), bg=BG, fg=ACCENT).pack(pady=(12, 6))

        container = tk.Frame(dialog, bg=BG)
        container.pack(fill="both", expand=True, padx=15, pady=(0, 10))

        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        form = tk.Frame(canvas, bg=BG)

        form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=form, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        tk.Label(form, text="Select Employee:", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
        emp_names = [f"{e[0]} - {e[1]} (Basic: {e[2]:,.2f})" for e in employees]
        combo_emp = ttk.Combobox(form, state="readonly", font=(FONT, 9))
        combo_emp["values"] = emp_names
        combo_emp.set(emp_names[0])
        combo_emp.pack(fill="x", pady=(1, 6))

        tk.Label(form, text="Pay Month (e.g., June 2026):", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
        ent_month = tk.Entry(form, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        ent_month.insert(0, datetime.datetime.now().strftime("%B %Y"))
        ent_month.pack(fill="x", pady=(1, 6), ipady=2)

        def expected_days_for(month_text):
            for fmt in ("%B %Y", "%b %Y", "%Y-%m", "%m/%Y"):
                try:
                    period = datetime.datetime.strptime(month_text.strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
            days_in_month = calendar.monthrange(period.year, period.month)[1]
            return sum(
                1 for day_num in range(1, days_in_month + 1)
                if WEEKDAYS[datetime.date(period.year, period.month, day_num).weekday()] not in nw_days_list
            )

        auto_expected_days = expected_days_for(ent_month.get()) or 0

        def on_month_change(event=None):
            days = expected_days_for(ent_month.get())
            if days is None:
                return
            # Keep "days worked" in step unless it was already edited to something different
            follow = ent_worked_days.get().strip() == ent_expected_days.get().strip()
            ent_expected_days.delete(0, tk.END)
            ent_expected_days.insert(0, str(days))
            if follow:
                ent_worked_days.delete(0, tk.END)
                ent_worked_days.insert(0, str(days))

        ent_month.bind("<KeyRelease>", on_month_change)
        ent_month.bind("<FocusOut>", on_month_change)

        tk.Label(form, text="Expected Work Days (auto-calculated from the Pay Month above):", bg=BG, fg=OK_TEXT, font=(FONT, 8, "bold")).pack(anchor="w")
        ent_expected_days = tk.Entry(form, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        ent_expected_days.insert(0, str(auto_expected_days))
        ent_expected_days.pack(fill="x", pady=(1, 6), ipady=2)

        tk.Label(form, text="Actual Days Worked by Employee:", bg=BG, fg=TEXT_MUTED, font=(FONT, 8)).pack(anchor="w")
        ent_worked_days = tk.Entry(form, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, relief="flat")
        ent_worked_days.insert(0, str(auto_expected_days))
        ent_worked_days.pack(fill="x", pady=(1, 10), ipady=2)

        # Dynamic Named Bonuses Frame
        bonus_frame = tk.LabelFrame(form, text="Bonuses & Additions (Named)", bg=BG, fg=OK_TEXT, font=(FONT, 9, "bold"), padx=8, pady=6)
        bonus_frame.pack(fill="x", pady=(0, 8))

        bonus_entries = []
        def add_bonus_row(name="", amt="0.0"):
            row = tk.Frame(bonus_frame, bg=BG)
            row.pack(fill="x", pady=2)
            ent_n = tk.Entry(row, font=(FONT, 8), bg=SURFACE, fg=TEXT_BRIGHT, width=18, relief="flat")
            ent_n.insert(0, name)
            ent_n.pack(side="left", padx=(0, 4))
            ent_a = tk.Entry(row, font=(FONT, 8), bg=SURFACE, fg=TEXT_BRIGHT, width=10, relief="flat")
            ent_a.insert(0, amt)
            ent_a.pack(side="left")
            bonus_entries.append((ent_n, ent_a))

        add_bonus_row("Transport Allowance", "0.00")
        GlassButton(bonus_frame, text="+ Add Bonus Item", bg=SURFACE_ALT, hover_bg=PRIMARY, command=lambda: add_bonus_row("Bonus/Overtime", "0.00")).pack(anchor="w", pady=2)

        # Dynamic Named Deductions Frame
        ded_frame = tk.LabelFrame(form, text="Custom Deductions (Named)", bg=BG, fg=WARN_TEXT, font=(FONT, 9, "bold"), padx=8, pady=6)
        ded_frame.pack(fill="x", pady=(0, 10))

        ded_entries = []
        def add_ded_row(name="", amt="0.0"):
            row = tk.Frame(ded_frame, bg=BG)
            row.pack(fill="x", pady=2)
            ent_n = tk.Entry(row, font=(FONT, 8), bg=SURFACE, fg=TEXT_BRIGHT, width=18, relief="flat")
            ent_n.insert(0, name)
            ent_n.pack(side="left", padx=(0, 4))
            ent_a = tk.Entry(row, font=(FONT, 8), bg=SURFACE, fg=TEXT_BRIGHT, width=10, relief="flat")
            ent_a.insert(0, amt)
            ent_a.pack(side="left")
            ded_entries.append((ent_n, ent_a))

        add_ded_row("Advance Repayment", "0.00")
        GlassButton(ded_frame, text="+ Add Deduction Item", bg=SURFACE_ALT, hover_bg=PRIMARY, command=lambda: add_ded_row("Loan / Penalty", "0.00")).pack(anchor="w", pady=2)

        def compute_payroll_action():
            try:
                selected_idx = combo_emp.current()
                emp = employees[selected_idx]
                emp_id, emp_name, basic_salary = emp[0], emp[1], safe_float(emp[2])

                pay_month = ent_month.get().strip()
                expected_days = safe_float(ent_expected_days.get().strip())
                worked_days = safe_float(ent_worked_days.get().strip())

                if expected_days <= 0:
                    expected_days = 22

                calculated_basic = (basic_salary / expected_days) * worked_days

                total_bonuses = 0.0
                bonuses_dict = {}
                for en, ea in bonus_entries:
                    b_name = en.get().strip()
                    b_val = safe_float(ea.get().strip())
                    if b_name:
                        bonuses_dict[b_name] = b_val
                        total_bonuses += b_val

                total_custom_ded = 0.0
                ded_dict = {}
                for en, ea in ded_entries:
                    d_name = en.get().strip()
                    d_val = safe_float(ea.get().strip())
                    if d_name:
                        ded_dict[d_name] = d_val
                        total_custom_ded += d_val

                raw_napsa = calculated_basic * napsa_rate
                raw_nhima = calculated_basic * nhima_rate

                emp_napsa = raw_napsa if napsa_bearer == "employee" else 0.0
                emp_nhima = raw_nhima if nhima_bearer == "employee" else 0.0
                employer_napsa = raw_napsa if napsa_bearer == "employer" else 0.0
                employer_nhima = raw_nhima if nhima_bearer == "employer" else 0.0

                net_payable = calculated_basic + total_bonuses - total_custom_ded - emp_napsa - emp_nhima

                self.cursor.execute(
                    """INSERT INTO payroll (user_id, employee_id, employee_name, pay_month, basic_salary, expected_days, worked_days, calculated_basic, bonuses_json, deductions_json, napsa_amt, nhima_amt, net_payable, timestamp, napsa_bearer, nhima_bearer, employer_napsa, employer_nhima)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (uid, emp_id, emp_name, pay_month, basic_salary, expected_days, worked_days, calculated_basic, json.dumps(bonuses_dict), json.dumps(ded_dict), emp_napsa, emp_nhima, net_payable, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), napsa_bearer, nhima_bearer, employer_napsa, employer_nhima)
                )
                self.conn.commit()
                messagebox.showinfo("Payslip Computed", f"Payslip generated successfully for {emp_name}!\nNet Payable: {config['app_settings']['currency']}{net_payable:,.2f}")
                dialog.destroy()
                self.load_employees_data()
            except ValueError:
                messagebox.showerror("Error", "Please verify numeric inputs.")

        GlassButton(dialog, text="Generate & Save Payslip", bg=SUCCESS, hover_bg=SUCCESS_HOVER, command=compute_payroll_action).pack(fill="x", padx=20, pady=(5, 15))

    def open_monthly_summary_dialog(self):
        uid = self.data_uid
        self.cursor.execute("""
            SELECT pay_month,
                   COUNT(id) as total_slips,
                   SUM(calculated_basic) as total_basic,
                   SUM(net_payable) as total_net,
                   SUM(napsa_amt) as total_emp_napsa,
                   SUM(nhima_amt) as total_emp_nhima,
                   SUM(employer_napsa) as total_employer_napsa,
                   SUM(employer_nhima) as total_employer_nhima
            FROM payroll
            WHERE user_id=?
            GROUP BY pay_month
            ORDER BY MAX(id) DESC
        """, (uid,))
        summary_rows = self.cursor.fetchall()

        dialog = tk.Toplevel(self.root)
        dialog.title("Monthly Statutory & Wage Summary")
        dialog.geometry("820x450")
        dialog.config(bg=BG)
        dialog.grab_set()

        tk.Label(dialog, text="📊 Monthly Payroll & Statutory Summary", font=(FONT, 12, "bold"), bg=BG, fg=ACCENT).pack(pady=(15, 10))

        table_frame = tk.Frame(dialog, bg=BG)
        table_frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        cols = ("Month", "Slips", "Total Basic", "Emp. NAPSA", "Emp. NHIMA", "Empr. NAPSA", "Empr. NHIMA", "Net Payout")
        sum_tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        for col in cols:
            sum_tree.heading(col, text=col)

        sum_tree.column("Month", width=90, anchor="center")
        sum_tree.column("Slips", width=45, anchor="center")
        sum_tree.column("Total Basic", width=95, anchor="e")
        sum_tree.column("Emp. NAPSA", width=85, anchor="e")
        sum_tree.column("Emp. NHIMA", width=85, anchor="e")
        sum_tree.column("Empr. NAPSA", width=85, anchor="e")
        sum_tree.column("Empr. NHIMA", width=85, anchor="e")
        sum_tree.column("Net Payout", width=100, anchor="e")

        sum_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=sum_tree.yview)
        sum_tree.configure(yscrollcommand=sum_scroll.set)
        sum_tree.pack(side="left", fill="both", expand=True)
        sum_scroll.pack(side="right", fill="y")

        curr = config["app_settings"]["currency"]
        for r in summary_rows:
            month_str, count, basic, emp_n, emp_h, empr_n, empr_h, net = r
            sum_tree.insert("", "end", values=(
                month_str, count,
                f"{curr}{safe_float(basic):,.2f}",
                f"{curr}{safe_float(emp_n):,.2f}",
                f"{curr}{safe_float(emp_h):,.2f}",
                f"{curr}{safe_float(empr_n):,.2f}",
                f"{curr}{safe_float(empr_h):,.2f}",
                f"{curr}{safe_float(net):,.2f}"
            ))

    def print_selected_payslips_pdf(self):
        if not REPORTLAB_AVAILABLE:
            messagebox.showerror("Error", "ReportLab library required for PDF generation.")
            return

        selected_items = self.pay_tree.selection()
        if not selected_items:
            messagebox.showwarning("Selection Error", "Please select one or more payroll records from the list to print payslips.")
            return

        per_page_str = simpledialog.askstring("Payslips Layout", "How many payslips should fit per A4 paper? (Type 1, 2, or 3):", initialvalue="2")
        if not per_page_str:
            return
        try:
            per_page = int(per_page_str.strip())
            if per_page not in [1, 2, 3]:
                per_page = 2
        except ValueError:
            per_page = 2

        payroll_ids = [self.pay_tree.item(i)["values"][0] for i in selected_items]
        uid = self.data_uid

        placeholders = ",".join(["?"] * len(payroll_ids))
        self.cursor.execute(f"SELECT id, employee_name, pay_month, basic_salary, expected_days, worked_days, calculated_basic, bonuses_json, deductions_json, napsa_amt, nhima_amt, net_payable FROM payroll WHERE user_id=? AND id IN ({placeholders}) ORDER BY id", [uid] + payroll_ids)
        records = self.cursor.fetchall()

        if not records:
            messagebox.showwarning("Warning", "No valid payroll records retrieved.")
            return

        fpath = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF Document", "*.pdf")], title="Save Batch Payslips PDF")
        if not fpath:
            return

        c = canvas.Canvas(fpath, pagesize=letter)
        w, h = letter
        header = config.get("receipt_header", {})
        curr = config["app_settings"]["currency"]

        margin = 35
        usable_height = h - (margin * 2)
        slip_height = usable_height / per_page

        current_on_page = 0
        for r in records:
            emp_name, pay_month = r[1], r[2]
            basic_sal = safe_float(r[3])
            exp_d = safe_int(r[4])
            work_d = safe_float(r[5])
            calc_basic = safe_float(r[6])
            bonuses = json.loads(r[7] or "{}")
            deductions = json.loads(r[8] or "{}")
            napsa = safe_float(r[9])
            nhima = safe_float(r[10])
            net_pay = safe_float(r[11])

            y_top = h - margin - (current_on_page * slip_height)

            c.setStrokeColorRGB(0.7, 0.7, 0.7)
            c.setLineWidth(1)
            c.rect(margin, y_top - slip_height + 15, w - (margin * 2), slip_height - 10)

            c.setFont("Helvetica-Bold", 10)
            c.drawString(margin + 12, y_top - 20, f"{header.get('company_name', 'BEAVER PASSION & SKILL')} — OFFICIAL PAYSLIP")
            c.setFont("Helvetica", 8)
            c.drawString(w - margin - 150, y_top - 20, f"Pay Period: {pay_month}")

            c.line(margin + 10, y_top - 26, w - margin - 10, y_top - 26)

            c.setFont("Helvetica-Bold", 8)
            c.drawString(margin + 12, y_top - 40, f"Employee: {emp_name}")
            c.drawString(margin + 220, y_top - 40, f"Full Basic Salary: {curr}{basic_sal:,.2f}")

            c.setFont("Helvetica", 8)
            c.drawString(margin + 12, y_top - 54, f"Expected Work Days: {exp_d} | Actual Days Worked: {work_d}")
            c.drawString(margin + 260, y_top - 54, f"Pro-Rata Basic Pay: {curr}{calc_basic:,.2f}")

            c.setFont("Helvetica-Bold", 8)
            c.drawString(margin + 12, y_top - 68, "ADDITIONS & BONUSES:")
            y_line = y_top - 80
            c.setFont("Helvetica", 8)
            if bonuses:
                for b_name, b_val in bonuses.items():
                    c.drawString(margin + 20, y_line, f"• {b_name}: {curr}{safe_float(b_val):,.2f}")
                    y_line -= 12
            else:
                c.drawString(margin + 20, y_line, "• None")
                y_line -= 12

            y_line -= 4
            c.setFont("Helvetica-Bold", 8)
            c.drawString(margin + 12, y_line, "STATUTORY & CUSTOM DEDUCTIONS:")
            y_line -= 12
            c.setFont("Helvetica", 8)
            c.drawString(margin + 20, y_line, f"• NAPSA Contribution: {curr}{napsa:,.2f}")
            y_line -= 12
            c.drawString(margin + 20, y_line, f"• NHIMA Contribution: {curr}{nhima:,.2f}")
            y_line -= 12
            for d_name, d_val in deductions.items():
                c.drawString(margin + 20, y_line, f"• {d_name}: {curr}{safe_float(d_val):,.2f}")
                y_line -= 12

            y_line -= 6
            c.setFont("Helvetica-Bold", 9)
            c.setFillColorRGB(0.1, 0.4, 0.1)
            c.drawString(margin + 12, y_line, f"NET PAYABLE SALARY: {curr}{net_pay:,.2f}")
            c.setFillColorRGB(0, 0, 0)

            current_on_page += 1
            if current_on_page >= per_page:
                current_on_page = 0
                c.showPage()

        c.save()
        messagebox.showinfo("Success", f"Batch payslips PDF successfully generated and saved to:\n{fpath}")

    # --- TAB 5: SALES & INVOICES HISTORY PAGE ---
    def setup_history_page(self):
        filter_bar = tk.Frame(self.history_page, bg=BG)
        filter_bar.pack(fill="x", padx=15, pady=10)

        tk.Label(filter_bar, text="Search:", bg=BG, fg=TEXT, font=(FONT, 9, "bold")).pack(side="left", padx=(0, 4))
        self.entry_hist_search = tk.Entry(filter_bar, font=(FONT, 9), bg=SURFACE, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT, width=16, relief="flat")
        self.entry_hist_search.pack(side="left", padx=(0, 10), ipady=2)
        self.entry_hist_search.bind("<KeyRelease>", lambda e: self.load_sales_history())

        tk.Label(filter_bar, text="Dept:", bg=BG, fg=TEXT, font=(FONT, 9, "bold")).pack(side="left", padx=(0, 4))
        self.combo_hist_dept = ttk.Combobox(filter_bar, state="readonly", font=(FONT, 9), width=14)
        self.combo_hist_dept.pack(side="left", padx=(0, 10))
        self.combo_hist_dept.set("All Departments")
        self.combo_hist_dept.bind("<<ComboboxSelected>>", lambda e: self.load_sales_history())

        GlassButton(filter_bar, text="📊 Export Summary", bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.export_summary_report).pack(side="right", padx=5)
        GlassButton(filter_bar, text="🖨 Reprint Invoice", bg=SURFACE_ALT, hover_bg=PRIMARY, command=self.reprint_selected_invoice).pack(side="right", padx=5)

        table_frame = tk.Frame(self.history_page, bg=BG)
        table_frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        cols = ("Invoice No", "Date/Time", "Customer", "Phone", "Product", "Dept", "Qty", "Total", "Paid", "Balance", "Status")
        self.hist_tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="extended")
        for col in cols:
            self.hist_tree.heading(col, text=col)

        self.hist_tree.column("Invoice No", width=95, anchor="center")
        self.hist_tree.column("Date/Time", width=120, anchor="center")
        self.hist_tree.column("Customer", width=110)
        self.hist_tree.column("Phone", width=85, anchor="center")
        self.hist_tree.column("Product", width=105)
        self.hist_tree.column("Dept", width=75)
        self.hist_tree.column("Qty", width=40, anchor="center")
        self.hist_tree.column("Total", width=75, anchor="e")
        self.hist_tree.column("Paid", width=75, anchor="e")
        self.hist_tree.column("Balance", width=75, anchor="e")
        self.hist_tree.column("Status", width=70, anchor="center")

        hist_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=hist_scroll.set)
        self.hist_tree.pack(side="left", fill="both", expand=True)
        hist_scroll.pack(side="right", fill="y")

    def load_sales_history(self):
        for item in self.hist_tree.get_children():
            self.hist_tree.delete(item)

        search_q = self.entry_hist_search.get().strip()
        dept_filter = self.combo_hist_dept.get()
        curr = config["app_settings"]["currency"]
        uid = self.data_uid

        query = "SELECT invoice_no, timestamp, customer_name, customer_phone, item_name, department, qty_sold, total_price, amount_paid, balance_due, payment_status FROM sales WHERE user_id=?"
        params = [uid]

        if dept_filter and dept_filter != "All Departments":
            query += " AND department=?"
            params.append(dept_filter)

        if search_q:
            query += " AND (customer_name LIKE ? OR item_name LIKE ? OR invoice_no LIKE ? OR customer_phone LIKE ?)"
            s = f"%{search_q}%"
            params.extend([s, s, s, s])

        query += " ORDER BY id DESC"
        self.cursor.execute(query, params)
        for r in self.cursor.fetchall():
            row = list(r)
            row[7] = f"{curr}{safe_float(r[7]):,.2f}"
            row[8] = f"{curr}{safe_float(r[8]):,.2f}"
            row[9] = f"{curr}{safe_float(r[9]):,.2f}"
            self.hist_tree.insert("", "end", values=row)

    def export_summary_report(self):
        selected_items = self.hist_tree.selection()
        rows = [tree_row(self.hist_tree, item_id) for item_id in (selected_items if selected_items else self.hist_tree.get_children())]

        if not rows:
            messagebox.showwarning("Warning", "No transaction records found to export.")
            return

        fmt = simpledialog.askstring("Export Format", "Choose export format (type 'pdf' or 'csv'):", initialvalue="pdf")
        if not fmt:
            return
        fmt = fmt.strip().lower()
        if fmt not in ("pdf", "csv"):
            messagebox.showwarning("Export Format", "Please type either 'pdf' or 'csv'.")
            return

        if fmt == "csv":
            fpath = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV Files", "*.csv")], title="Save Invoices Summary CSV")
            if fpath:
                with open(fpath, mode="w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["Invoice No", "Date/Time", "Customer", "Phone", "Product", "Dept", "Qty", "Total", "Paid", "Balance", "Status"])
                    for r in rows:
                        writer.writerow(r)
                messagebox.showinfo("Exported", f"Invoices summary exported successfully to:\n{fpath}")
        elif fmt == "pdf":
            if not REPORTLAB_AVAILABLE:
                messagebox.showerror("Error", "ReportLab not installed.")
                return
            fpath = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF Documents", "*.pdf")], title="Save Invoices Summary PDF")
            if fpath:
                c = canvas.Canvas(fpath, pagesize=letter)
                w, h = letter
                c.setFont("Helvetica-Bold", 14)
                c.drawString(40, h - 40, "Invoices & Sales Transaction Summary")
                c.setFont("Helvetica", 9)
                c.drawString(40, h - 55, f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                c.line(40, h - 65, w - 40, h - 65)

                y = h - 85
                c.setFont("Helvetica-Bold", 8)
                headers = ["Invoice No", "Date/Time", "Customer", "Product", "Qty", "Total", "Status"]
                x_pos = [40, 115, 210, 305, 395, 435, 490]
                for h_text, xp in zip(headers, x_pos):
                    c.drawString(xp, y, h_text)
                y -= 10
                c.line(40, y, w - 40, y)
                y -= 15

                c.setFont("Helvetica", 8)
                for r in rows:
                    if y < 50:
                        c.showPage()
                        y = h - 50
                    vals = [str(r[0]), str(r[1]), str(r[2])[:14], str(r[4])[:14], str(r[6]), str(r[7]), str(r[10])]
                    for val, xp in zip(vals, x_pos):
                        c.drawString(xp, y, val)
                    y -= 14
                c.save()
                messagebox.showinfo("Exported", f"Invoices summary PDF saved to:\n{fpath}")

    def reprint_selected_invoice(self):
        selected = self.hist_tree.selection()
        if not selected:
            messagebox.showwarning("Selection Error", "Please select an invoice record to reprint.")
            return
        inv_no = str(self.hist_tree.set(selected[0], "Invoice No"))
        self.cursor.execute("SELECT item_name, qty_sold, unit_price, total_price, payment_status, amount_paid, balance_due, customer_name, customer_phone, transaction_type, timestamp FROM sales WHERE invoice_no=? AND user_id=? ORDER BY id", (inv_no, self.data_uid))
        rows = self.cursor.fetchall()
        if rows:
            first = rows[0]
            items = []
            grand_total = amount_paid = balance_due = 0.0
            for r in rows:
                t_price = safe_float(r[3])
                items.append({"name": r[0], "qty": r[1], "price": safe_float(r[2]), "total": t_price})
                grand_total += t_price
                amount_paid += safe_float(r[5])
                balance_due += safe_float(r[6])

            self.show_invoice_popup({
                "invoice_no": inv_no, "timestamp": first[10], "cust_name": first[7], "cust_phone": first[8],
                "items": items, "grand_total": grand_total, "payment_status": first[4],
                "amount_paid": amount_paid, "balance_due": balance_due, "is_farm_use": (first[9] == 'FARM_USE')
            })

    def show_invoice_popup(self, data):
        header = config.get("receipt_header", {})
        curr = config["app_settings"]["currency"]

        items_str = ""
        for i in data["items"]:
            items_str += f"- {i['name']} (x{i['qty']}) @ {curr}{i['price']:,.2f} = {curr}{i['total']:,.2f}\n"

        invoice_text = f"""
============================================================
           {header.get('company_name', 'BEAVER PASSION & SKILL')}
             {header.get('subtitle', 'Official Sales Invoice')}
             {header.get('contact', '')}
============================================================
Invoice No  : {data['invoice_no']}
Date & Time : {data['timestamp']}
User Account: {self.current_user['username']} ({self.current_user['role'].upper()})

------------------------------------------------------------
CUSTOMER INFORMATION
------------------------------------------------------------
Name            : {data['cust_name']}
Phone           : {data['cust_phone']}

------------------------------------------------------------
BOUGHT ITEMS / LINE ITEMS
------------------------------------------------------------
{items_str}
------------------------------------------------------------
PAYMENT & TOTALS
------------------------------------------------------------
Payment Status  : {data['payment_status']}
Grand Total     : {curr}{data['grand_total']:,.2f}
Amount Paid     : {curr}{data['amount_paid']:,.2f}
Balance Due     : {curr}{data['balance_due']:,.2f}
============================================================
             Thank you for your business!
============================================================
"""

        popup = tk.Toplevel(self.root)
        popup.title("Official Invoice")
        popup.geometry("520x640")
        popup.config(bg=BG)

        text_area = tk.Text(popup, bg=SURFACE, fg=OK_TEXT, font=(FONT_MONO, 9, "bold"), padx=10, pady=10, relief="flat")
        text_area.insert(tk.END, invoice_text)
        text_area.config(state="disabled")
        text_area.pack(fill="both", expand=True, padx=10, pady=10)

        btn_bar = tk.Frame(popup, bg=BG)
        btn_bar.pack(fill="x", padx=10, pady=(0, 10))

        def save_pdf():
            if not REPORTLAB_AVAILABLE:
                messagebox.showerror("Error", "ReportLab library required for PDF generation.", parent=popup)
                return
            fpath = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF Document", "*.pdf")], title="Save Invoice PDF")
            if fpath:
                c = canvas.Canvas(fpath, pagesize=letter)
                w, h = letter
                c.setFont("Helvetica-Bold", 14)
                c.drawCentredString(w/2.0, h-50, header.get('company_name', 'BEAVER PASSION & SKILL'))
                c.setFont("Helvetica", 10)
                c.drawCentredString(w/2.0, h-65, "INTERNAL FARM USE ISSUE NOTE" if data.get("is_farm_use") else "OFFICIAL SALES INVOICE")
                c.line(40, h-75, w-40, h-75)

                y = h - 95
                c.setFont("Helvetica", 9)
                for line in invoice_text.split("\n"):
                    if line.strip() and not line.startswith("="):
                        c.drawString(50, y, line)
                        y -= 14
                c.save()
                messagebox.showinfo("Saved", f"Invoice PDF saved to:\n{fpath}")

        GlassButton(btn_bar, text="📄 Save PDF", bg=PRIMARY, hover_bg=PRIMARY_HOVER, command=save_pdf).pack(side="left", padx=5)
        GlassButton(btn_bar, text="Close", bg=DANGER, hover_bg=DANGER_HOVER, command=popup.destroy).pack(side="right", padx=5)


if __name__ == "__main__":
    root = tk.Tk()
    SplashLoadingScreen(root, lambda r: AuthWindow(r))
    root.mainloop()