"""What the assistant (assistant.py) may do, as tools Gemini can choose from.

Every tool is read-only, except the prepare_* ones, which only write a card for the person to confirm, and the ones that save what the person
asked to be remembered (the assistant's own memory, shown to them as it is saved). A tool never sees more than the signed-in
person could open in the app: each query is scoped to their business (ctx.biz), and a tool for an admin-only part of the app (admin=True) is
not even offered to an operator. Lists are capped, text from the records is shortened, and links are made here, never by Gemini.
"""
import datetime
import json
import math
import re

import assistant
from assistant import H, ToolError, clip, tool

# ---------- Descriptions of what a tool takes (kept terse: every call to the model carries all of them) ----------
def S(description=None):
    return {"type": "string", "description": description} if description else {"type": "string"}


def N(description=None):
    return {"type": "integer", "description": description} if description else {"type": "integer"}


def B(description=None):
    return {"type": "boolean", "description": description} if description else {"type": "boolean"}


def E(*values):
    return {"type": "string", "enum": list(values)}


PERIOD_NAMES = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_year", "all_time")
PERIOD = E(*PERIOD_NAMES)
FROM = S("First day, YYYY-MM-DD")
TO = S("Last day, YYYY-MM-DD")
LIMIT = N("Max rows")


# ---------- Reading what was asked ----------
def text_of(args, key, n=80):
    return clip(args.get(key), n) if args.get(key) is not None else ""


def whole(args, key, default, low=1, high=40):
    try:
        return max(low, min(high, int(args.get(key, default))))
    except (TypeError, ValueError):
        return default


def choice(args, key, options, default):
    return args.get(key) if args.get(key) in options else default


def like(term):
    """A term as a LIKE pattern. The characters LIKE treats as wild are dropped, so a name is only ever matched as written."""
    return "%" + "".join(" " if c in "%_\\" else c for c in term.strip().lower()) + "%"


def r2(value):
    return round(float(value or 0), 2)


def day_of(raw):
    day = H.day_or_none(str(raw)) if raw else None
    return datetime.date.fromisoformat(day) if day else None


def period(ctx, args, default="this_month"):
    """The days a question is about: (first day or None, last day or None, a name for it). Days typed win over a named period."""
    t = datetime.date.fromisoformat(ctx.today)
    first, last = day_of(args.get("from_date")), day_of(args.get("to_date"))
    if first or last:
        return first, last, f"{first.isoformat() if first else 'the beginning'} to {last.isoformat() if last else ctx.today}"
    name = choice(args, "period", PERIOD_NAMES, default)
    monday = t - datetime.timedelta(days=t.weekday())
    month_start = t.replace(day=1)
    if name == "today":
        return t, t, "today"
    if name == "yesterday":
        y = t - datetime.timedelta(days=1)
        return y, y, "yesterday"
    if name == "this_week":
        return monday, t, "this week (from Monday)"
    if name == "last_week":
        return monday - datetime.timedelta(days=7), monday - datetime.timedelta(days=1), "last week"
    if name == "this_month":
        return month_start, t, "this month"
    if name == "last_month":
        end = month_start - datetime.timedelta(days=1)
        return end.replace(day=1), end, "last month"
    if name == "this_year":
        return t.replace(month=1, day=1), t, "this year"
    return None, None, "all time"


def between(column, first, last):
    """A SQL condition (starting with AND) keeping rows whose text date in column is within the days, and its parameters."""
    sql, params = "", []
    if first:
        sql += f" AND {column} >= ?"
        params.append(first.isoformat())
    if last:
        sql += f" AND {column} < ?"
        params.append((last + datetime.timedelta(days=1)).isoformat())
    return sql, params


def between_days(column, first, last):
    """The same for a column holding just a day (YYYY-MM-DD), where the last day itself is included."""
    sql, params = "", []
    if first:
        sql += f" AND {column} >= ?"
        params.append(first.isoformat())
    if last:
        sql += f" AND {column} <= ?"
        params.append(last.isoformat())
    return sql, params


SALE = "i.kind='SALE' AND i.payment_status <> 'Cancelled'"


def handover_of(row):
    """Where an invoice stands on delivery and collection, in a few words (or None)."""
    if row["deliver_on"]:
        return "delivered" if row["delivered_at"] else f"to be delivered {row['deliver_on']}"
    if row["collect_on"]:
        return "collected" if row["collected_at"] else f"to be collected {row['collect_on']}"
    return None


# ================= Read-only tools, for anyone who can open the app =================
@tool("Overview: sales today and this month, what customers owe, POS stock, pre-orders, deliveries and collections due. Start here for 'how are we doing?'.")
def business_snapshot(ctx, args):
    db, biz, today = ctx.db, ctx.biz, ctx.today
    tomorrow, month_start = H.add_days(today, 1), today[:8] + "01"

    def sales(since, until):
        r = db.one(
            f"""SELECT COUNT(*) AS n, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.amount_paid), 0) AS paid FROM invoices i
                WHERE i.business_id=? AND {SALE} AND i.created_at >= ? AND i.created_at < ?""",
            (biz, since, until),
        )
        return {"invoices": r["n"], "total": r2(r["total"]), "paid": r2(r["paid"])}

    owed = db.one(f"SELECT COUNT(*) AS n, COALESCE(SUM(i.balance_due), 0) AS owed FROM invoices i WHERE i.business_id=? AND {SALE} AND i.balance_due > 0.005", (biz,))
    stock = db.one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(quantity), 0) AS units, COALESCE(SUM(CASE WHEN quantity < ? THEN 1 ELSE 0 END), 0) AS low FROM pos_products WHERE business_id=? AND quantity > 0",
        (ctx.user["low_stock_threshold"], biz),
    )
    held = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(qty), 0) AS qty FROM preorders WHERE business_id=? AND status='pending'", (biz,))

    def due(column, done):
        r = db.one(
            f"""SELECT COALESCE(SUM(CASE WHEN {column} < ? THEN 1 ELSE 0 END), 0) AS overdue, COALESCE(SUM(CASE WHEN {column} = ? THEN 1 ELSE 0 END), 0) AS today,
                       COALESCE(SUM(CASE WHEN {column} > ? THEN 1 ELSE 0 END), 0) AS later
                FROM invoices WHERE business_id=? AND kind='SALE' AND {column} IS NOT NULL AND {done} IS NULL AND COALESCE(payment_status, '') <> 'Cancelled'""",
            (today, today, today, biz),
        )
        return {"overdue": r["overdue"], "today": r["today"], "later": r["later"]}

    ctx.source("Sales & Invoices", H.url_for("history"))
    return {
        "today": today, "sales_today": sales(today, tomorrow), "sales_this_month": sales(month_start, tomorrow),
        "customers_owe_us": {"unpaid_invoices": owed["n"], "amount": r2(owed["owed"])},
        "pos_stock": {"items_in_stock": stock["n"], "units": stock["units"], "items_low": stock["low"], "low_means_under": ctx.user["low_stock_threshold"]},
        "preorders_waiting": {"preorders": held["n"], "seedlings": held["qty"]},
        "deliveries_to_make": due("deliver_on", "delivered_at"), "collections_waiting": due("collect_on", "collected_at"),
    }


@tool("Sales totals for a period, optionally grouped. Amounts are after discounts.",
      {"period": PERIOD, "from_date": FROM, "to_date": TO, "group_by": E("day", "month", "item", "department", "customer", "payment_status", "seller"), "limit": LIMIT})
def sales_report(ctx, args):
    db, biz = ctx.db, ctx.biz
    first, last, label = period(ctx, args)
    when, when_params = between("i.created_at", first, last)
    totals = db.one(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.amount_paid), 0) AS paid, COALESCE(SUM(i.balance_due), 0) AS owed
            FROM invoices i WHERE i.business_id=? AND {SALE}{when}""",
        [biz] + when_params,
    )
    discount = db.one(
        f"""SELECT COALESCE(SUM(COALESCE(ii.discount, 0)), 0) AS d FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
            WHERE ii.business_id=? AND {SALE}{when}""",
        [biz] + when_params,
    )
    out = {"period": label, "invoices": totals["n"], "total": r2(totals["total"]), "received": r2(totals["paid"]), "still_owed": r2(totals["owed"]), "discounts_given": r2(discount["d"])}
    group, limit = args.get("group_by"), whole(args, "limit", 10)
    by_invoice = {
        "day": "SUBSTR(i.created_at, 1, 10)", "month": "SUBSTR(i.created_at, 1, 7)", "payment_status": "i.payment_status",
        "customer": "COALESCE(NULLIF(i.customer_name, ''), 'Walk-in Customer')", "seller": "COALESCE(i.created_by, '')",
    }
    by_line = {"item": "ii.item_name", "department": "COALESCE(NULLIF(ii.department, ''), 'General')"}
    if group in by_invoice:
        order = "grp DESC" if group in ("day", "month") else "total DESC"
        rows = db.all(
            f"""SELECT {by_invoice[group]} AS grp, COUNT(*) AS n, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.balance_due), 0) AS owed
                FROM invoices i WHERE i.business_id=? AND {SALE}{when} GROUP BY 1 ORDER BY {order} LIMIT ?""",
            [biz] + when_params + [limit],
        )
        out["groups"] = [{group: clip(r["grp"], 80), "invoices": r["n"], "total": r2(r["total"]), "still_owed": r2(r["owed"])} for r in rows]
    elif group in by_line:
        rows = db.all(
            f"""SELECT {by_line[group]} AS grp, COUNT(DISTINCT i.id) AS n, COALESCE(SUM(ii.qty), 0) AS units, COALESCE(SUM(ii.line_total), 0) AS total
                FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id WHERE ii.business_id=? AND {SALE}{when} GROUP BY 1 ORDER BY total DESC LIMIT ?""",
            [biz] + when_params + [limit],
        )
        out["groups"] = [{group: clip(r["grp"], 80), "invoices": r["n"], "units": r["units"], "total": r2(r["total"])} for r in rows]
    ctx.source("Sales & Invoices", H.url_for("history"))
    ctx.source("Reports", H.url_for("reports"))
    return out


@tool("Find sales invoices by number, customer name or phone, status, period, unpaid, or delivery/collection state. Newest first, with the count and total of all matches.",
      {"query": S("Invoice no, customer name or phone"), "status": E("Paid", "Partial Payment", "Unpaid", "Cancelled"), "unpaid_only": B(), "handover": E("to_deliver", "to_collect", "delivered"),
       "period": PERIOD, "from_date": FROM, "to_date": TO, "limit": LIMIT})
def find_invoices(ctx, args):
    db, biz = ctx.db, ctx.biz
    q = text_of(args, "query")
    first, last, label = period(ctx, args, "all_time" if q or args.get("unpaid_only") or args.get("handover") else "this_month")
    where, params = "i.business_id=? AND i.kind='SALE'", [biz]
    status = args.get("status")
    if status == "Cancelled":
        where += " AND i.payment_status = 'Cancelled'"
    else:
        where += " AND i.payment_status <> 'Cancelled'"
        if status in ("Paid", "Partial Payment", "Unpaid"):
            where += " AND i.payment_status = ?"
            params.append(status)
    if args.get("unpaid_only"):
        where += " AND i.balance_due > 0.005"
    handover = args.get("handover")
    if handover == "to_deliver":
        where += " AND i.deliver_on IS NOT NULL AND i.delivered_at IS NULL"
    elif handover == "to_collect":
        where += " AND i.collect_on IS NOT NULL AND i.collected_at IS NULL"
    elif handover == "delivered":
        where += " AND i.delivered_at IS NOT NULL"
    if q:
        where += " AND (LOWER(i.invoice_no) LIKE ? OR LOWER(i.customer_name) LIKE ? OR COALESCE(i.customer_phone, '') LIKE ?)"
        params += [like(q), like(q), "%" + q.replace(" ", "") + "%"]
    when, when_params = between("i.created_at", first, last)
    where += when
    params += when_params
    totals = db.one(f"SELECT COUNT(*) AS n, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.balance_due), 0) AS owed FROM invoices i WHERE {where}", params)
    rows = db.all(
        f"""SELECT i.id, i.invoice_no, i.created_at, i.customer_name, i.customer_phone, i.payment_status, i.total, i.amount_paid, i.balance_due, i.created_by,
                   i.deliver_on, i.delivered_at, i.collect_on, i.collected_at FROM invoices i WHERE {where} ORDER BY i.created_at DESC, i.id DESC LIMIT ?""",
        params + [whole(args, "limit", 12)],
    )
    for r in rows[:3]:
        ctx.source(r["invoice_no"], H.url_for("invoice", invoice_id=r["id"]))
    ctx.source("Sales & Invoices", H.url_for("history", **({"q": q} if q else {})))
    return {
        "period": label, "matching": totals["n"], "total_value": r2(totals["total"]), "total_still_owed": r2(totals["owed"]), "shown": len(rows),
        "invoices": [
            {"invoice_no": r["invoice_no"], "date": (r["created_at"] or "")[:16], "customer": clip(r["customer_name"], 60), "phone": r["customer_phone"] or "",
             "status": r["payment_status"], "total": r2(r["total"]), "paid": r2(r["amount_paid"]), "balance": r2(r["balance_due"]), "sold_by": r["created_by"], "handover": handover_of(r)}
            for r in rows
        ],
    }


@tool("One invoice in full: items, prices, discounts, totals, delivery or collection; for admins the payments too.", {"invoice_no": S("e.g. INV-2026-0012")}, required=["invoice_no"])
def invoice_details(ctx, args):
    db, biz = ctx.db, ctx.biz
    number = text_of(args, "invoice_no", 40).upper()
    inv = db.one("SELECT * FROM invoices WHERE business_id=? AND kind='SALE' AND UPPER(invoice_no)=?", (biz, number))
    if inv is None:
        raise ToolError(f"There is no sales invoice numbered {number}.")
    lines = db.all(
        "SELECT item_code, item_name, qty, unit_price, COALESCE(discount, 0) AS discount, line_total FROM invoice_items WHERE invoice_id=? AND business_id=? ORDER BY id",
        (inv["id"], biz),
    )
    out = {
        "invoice_no": inv["invoice_no"], "date": (inv["created_at"] or "")[:16], "customer": clip(inv["customer_name"], 60), "phone": inv["customer_phone"] or "",
        "status": inv["payment_status"], "total": r2(inv["total"]), "paid": r2(inv["amount_paid"]), "balance": r2(inv["balance_due"]), "sold_by": inv["created_by"],
        "handover": handover_of(inv), "items": [{"code": l["item_code"], "item": clip(l["item_name"], 80), "qty": l["qty"], "unit_price": r2(l["unit_price"]), "discount": r2(l["discount"]), "line_total": r2(l["line_total"])} for l in lines],
    }
    if inv["payment_status"] == "Cancelled":
        out["cancelled"] = {"by": inv.get("cancelled_by"), "reason": clip(inv.get("cancel_reason"), 160)}
    if ctx.user["role"] == "admin":
        pays = db.all("SELECT direction, amount, method, occurred_at FROM cashbook WHERE business_id=? AND source='invoice' AND source_id=? ORDER BY occurred_at, id", (biz, inv["id"]))
        out["money_movements"] = [{"when": (p["occurred_at"] or "")[:16], "direction": p["direction"], "amount": r2(p["amount"]), "method": p["method"]} for p in pays]
    ctx.source(inv["invoice_no"], H.url_for("invoice", invoice_id=inv["id"]))
    return out


@tool("Who bought a product: customers, phones, quantities, prices, discounts, newest first, with totals.",
      {"product": S("Code or part of the name"), "period": PERIOD, "from_date": FROM, "to_date": TO, "limit": LIMIT}, required=["product"])
def product_buyers(ctx, args):
    db, biz = ctx.db, ctx.biz
    term = text_of(args, "product", 80)
    if not term:
        raise ToolError("Which product?")
    first, last, label = period(ctx, args, "all_time")
    when, when_params = between("i.created_at", first, last)
    base = f"ii.business_id=? AND {SALE} AND (UPPER(ii.item_code) = ? OR LOWER(ii.item_name) LIKE ?){when}"
    params = [biz, term.upper(), like(term)] + when_params
    summary = db.one(
        f"""SELECT COUNT(*) AS lines, COUNT(DISTINCT ii.invoice_id) AS invoices, COALESCE(SUM(ii.qty), 0) AS units, COALESCE(SUM(ii.line_total), 0) AS value,
                   COALESCE(SUM(COALESCE(ii.discount, 0)), 0) AS discount, COUNT(DISTINCT COALESCE(CAST(i.customer_id AS TEXT), i.customer_name)) AS customers,
                   COUNT(DISTINCT ii.item_code) AS codes, MIN(ii.item_code) AS code, MIN(ii.item_name) AS name
            FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id WHERE {base}""",
        params,
    )
    rows = db.all(
        f"""SELECT i.invoice_no, i.created_at, i.customer_name, i.customer_phone, ii.item_code, ii.item_name, ii.qty, ii.unit_price, COALESCE(ii.discount, 0) AS discount, ii.line_total
            FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id WHERE {base} ORDER BY i.created_at DESC, ii.id DESC LIMIT ?""",
        params + [whole(args, "limit", 12)],
    )
    if summary["codes"] == 1 and summary["code"]:
        ctx.source("Who bought " + clip(summary["name"], 40), H.sold_url(summary["code"], summary["name"]))
    return {
        "period": label, "sales_lines": summary["lines"], "invoices": summary["invoices"], "units_sold": summary["units"], "value_after_discounts": r2(summary["value"]),
        "discounts_given": r2(summary["discount"]), "customers": summary["customers"], "different_products_matched": summary["codes"],
        "latest": [
            {"date": (r["created_at"] or "")[:16], "invoice_no": r["invoice_no"], "customer": clip(r["customer_name"], 60), "phone": r["customer_phone"] or "", "product": clip(r["item_name"], 70),
             "qty": r["qty"], "unit_price": r2(r["unit_price"]), "discount": r2(r["discount"]), "total": r2(r["line_total"])}
            for r in rows
        ],
    }


@tool("Stock on hand: POS stock (selling price) and inventory (cost, supplier). Can show only low stock.",
      {"query": S("Part of a name or code"), "where": E("pos", "inventory", "both"), "low_only": B(), "department": S(), "limit": LIMIT})
def find_stock(ctx, args):
    db, biz = ctx.db, ctx.biz
    q, dept, limit = text_of(args, "query"), text_of(args, "department", 40), whole(args, "limit", 12)
    where_, low = choice(args, "where", ("pos", "inventory", "both"), "both"), bool(args.get("low_only"))
    threshold = ctx.user["low_stock_threshold"]
    out = {"low_stock_means_under": threshold}

    def filters(columns):
        sql, params = "", []
        if q:
            sql += " AND (LOWER(name) LIKE ? OR UPPER(item_code) LIKE ?)"
            params += [like(q), like(q).upper()]
        if dept:
            sql += " AND LOWER(department) = ?"
            params.append(dept.lower())
        if low:
            sql += " AND quantity < ?"
            params.append(threshold)
        return sql, params

    if where_ in ("pos", "both"):
        sql, params = filters(None)
        count = db.one(f"SELECT COUNT(*) AS n, COALESCE(SUM(quantity), 0) AS units FROM pos_products WHERE business_id=?{sql}", [biz] + params)
        rows = db.all(f"SELECT item_code, name, department, quantity, price, date_planted FROM pos_products WHERE business_id=?{sql} ORDER BY name LIMIT ?", [biz] + params + [limit])
        out["pos"] = {"matching": count["n"], "units": count["units"], "items": [
            {"code": r["item_code"], "name": clip(r["name"], 70), "department": r["department"], "in_stock": r["quantity"], "price": r2(r["price"]), "planted": r["date_planted"] or None} for r in rows]}
        ctx.source("Sales & POS", H.url_for("pos"))
    if where_ in ("inventory", "both"):
        sql, params = filters(None)
        count = db.one(f"SELECT COUNT(*) AS n, COALESCE(SUM(quantity), 0) AS units, COALESCE(SUM(quantity * unit_cost), 0) AS value FROM products WHERE business_id=?{sql}", [biz] + params)
        rows = db.all(
            f"SELECT item_code, name, category, department, quantity, unit_cost, price, supplier_name, expiry_date FROM products WHERE business_id=?{sql} ORDER BY name LIMIT ?", [biz] + params + [limit]
        )
        out["inventory"] = {"matching": count["n"], "units": count["units"], "value_at_cost": r2(count["value"]), "items": [
            {"code": r["item_code"], "name": clip(r["name"], 70), "category": r["category"], "department": r["department"], "quantity": r["quantity"], "unit_cost": r2(r["unit_cost"]),
             "price": r2(r["price"]), "supplier": clip(r["supplier_name"], 50), "expires": r["expiry_date"] or None} for r in rows]}
        ctx.source("Inventory", H.url_for("inventory"))
    return out


@tool("Greenhouse batches: planted, seedlings, ready date, pre-ordered, sold, available.", {"query": S("Plant, seed or batch name"), "stage": E("growing", "due", "ready", "record", "all"), "limit": LIMIT})
def plantings(ctx, args):
    db, biz = ctx.db, ctx.biz
    q, stage = text_of(args, "query"), choice(args, "stage", ("growing", "due", "ready", "record", "all"), "all")
    sql, params = "", []
    if q:
        sql = " AND (LOWER(name) LIKE ? OR LOWER(batch_no) LIKE ? OR LOWER(seed_name) LIKE ?)"
        params = [like(q)] * 3
    rows = db.all(f"SELECT * FROM plantings WHERE business_id=?{sql} ORDER BY ready_on DESC, id DESC LIMIT 200", [biz] + params)
    figures = [f for f in H.with_figures(db, biz, rows) if stage == "all" or f["stage"] == stage]
    limit = whole(args, "limit", 12)
    for f in figures[:2]:
        ctx.source(f["label"], H.url_for("planting_detail", planting_id=f["id"]))
    ctx.source("Planting Records", H.url_for("planting_list"))
    shown = figures[:limit]
    planted_by = {}
    if shown:
        marks = ",".join("?" for _ in shown)
        for r in db.all(f"SELECT planting_id, employee_name, trays FROM planting_trays WHERE business_id=? AND planting_id IN ({marks}) ORDER BY id", [biz] + [f["id"] for f in shown]):
            planted_by.setdefault(r["planting_id"], []).append({"name": clip(r["employee_name"], 60), "trays": r2(r["trays"])})
    return {
        "matching": len(figures), "batches": [
            {"batch": f["label"], "planted_on": f["planted_on"], "seeds_planted": f["seeds_planted"], "trays": f["trays"], "holes_per_tray": f["holes_per_tray"], "planted_by": planted_by.get(f["id"]),
             "seedlings": f["seedlings"], "ready_on": f["actual_ready_on"] or f["ready_on"],
             "stage": f["stage_label"], "days_to_ready": f["days"] if f["stage"] == "growing" else None, "pre_ordered_not_collected": f["pending"], "sold": f["sold"],
             "available": f["available"], "price": r2(f["price"]), "department": f["department"]}
            for f in shown
        ],
    }


@tool("Customers' seedling pre-orders: who, what, how many, collection day, status. due_within_days: only pending ones due by then (collection "
      "day, or the batch's ready day) and any overdue, soonest first, to remind those customers.",
      {"status": E("pending", "collected", "cancelled", "all"), "customer": S(), "due_within_days": N("e.g. 7"), "limit": LIMIT})
def preorders(ctx, args):
    db, biz = ctx.db, ctx.biz
    status, who = choice(args, "status", ("pending", "collected", "cancelled", "all"), "pending"), text_of(args, "customer")
    within = number_of(args.get("due_within_days"))
    sql, params = "", []
    if within is not None:
        status = "pending"
        sql += " AND COALESCE(po.collect_on, (SELECT COALESCE(p2.actual_ready_on, p2.ready_on) FROM plantings p2 WHERE p2.id = po.planting_id)) <= ?"
        params.append(H.add_days(ctx.today, max(0, min(int(within), 90))))
    if status != "all":
        sql += " AND po.status = ?"
        params.append(status)
    if who:
        sql += " AND LOWER(po.customer_name) LIKE ?"
        params.append(like(who))
    count = db.one(f"SELECT COUNT(*) AS n, COALESCE(SUM(po.qty), 0) AS qty FROM preorders po WHERE po.business_id=?{sql}", [biz] + params)
    rows = db.all(
        f"""SELECT po.customer_name, po.customer_phone, po.qty, po.unit_price, po.status, po.collect_on, po.created_at, po.note, p.name AS plant, p.batch_no, p.planted_on, po.planting_id
            FROM preorders po JOIN plantings p ON p.id = po.planting_id WHERE po.business_id=?{sql}
            ORDER BY CASE WHEN po.status = 'pending' THEN 0 ELSE 1 END, COALESCE(po.collect_on, '9999-12-31'), po.id DESC LIMIT ?""",
        [biz] + params + [whole(args, "limit", 12)],
    )
    ctx.source("Up Next & pre-orders", H.url_for("up_next"))
    return {
        "matching": count["n"], "seedlings": count["qty"], "preorders": [
            {"customer": clip(r["customer_name"], 60), "phone": r["customer_phone"] or "", "plant": f"{r['plant']} ({r['batch_no'] or 'planted ' + (r['planted_on'] or '')})", "qty": r["qty"],
             "price_each": r2(r["unit_price"]), "status": r["status"], "collect_on": r["collect_on"], "ordered": (r["created_at"] or "")[:10], "note": clip(r["note"], 80)}
            for r in rows
        ],
        **({"say": "These customers' orders are due soon: tell the person who to remind, and offer to draft a reminder message to each."} if within is not None else {}),
    }


@tool("The reminder report: customers who still owe (admins), seedling batches ready with seedlings left to sell, store stock with none on the POS, "
      "batches ready longer than they should be, and pre-orders due soon with the customers to remind.", {"limit": LIMIT})
def reminders(ctx, args):
    db, biz, user, limit = ctx.db, ctx.biz, ctx.user, whole(args, "limit", 8)
    r = H.reminder_report(db, biz, H.reminder_stale_days(user), admin=user["role"] == "admin")
    ctx.source("Reminders", H.url_for("reminders")) if user["role"] == "admin" else ctx.source("Up next", H.url_for("up_next"))
    out = {
        "ready_to_sell": [{"batch": f["label"], "left": f["available"], "ready_since": f["ready_since"]} for f in r["ready"][:limit]],
        "in_store_not_on_pos": [{"item": clip(x["name"], 60), "in_store": x["quantity"]} for x in r["not_on_pos"][:limit]],
        f"ready_over_{r['stale_days']}_days": [{"batch": f["label"], "days_ready": f["ready_days"], "left": f["available"]} for f in r["stale"][:limit]],
        f"preorders_due_within_{H.REMINDER_ORDER_DAYS}_days": [
            {"customer": clip(o["customer_name"], 60), "phone": o["customer_phone"] or "", "qty": o["qty"], "plant": o["label"], "due": o["due"], "overdue": o["overdue"]}
            for o in r["orders"][:limit]],
    }
    if "owing" in r:
        out["customers_owing"] = {"customers": len(r["owing"]), "total": r2(r["owing_total"]), "most": [
            {"customer": clip(o["customer_name"], 60), "phone": o["customer_phone"] or "", "owes": r2(o["owed"]), "invoices": o["invoices"], "since": o["since"]}
            for o in r["owing"][:limit]]}
    out = {k: v for k, v in out.items() if v not in ([], None) and not (isinstance(v, dict) and not v.get("customers"))}
    return out or {"say": "Nothing to remind about right now."}


@tool("Deliveries and collections still waiting, by day, with customers and items. Overdue first.", {"kind": E("deliveries", "collections", "both"), "day": S("YYYY-MM-DD"), "limit": LIMIT})
def deliveries_and_collections(ctx, args):
    db, biz = ctx.db, ctx.biz
    kind, day, limit = choice(args, "kind", ("deliveries", "collections", "both"), "both"), (H.day_or_none(str(args.get("day"))) if args.get("day") else None), whole(args, "limit", 12)
    out = {"today": ctx.today}
    for name, column, done in (("deliveries", "deliver_on", "delivered_at"), ("collections", "collect_on", "collected_at")):
        if kind not in (name, "both"):
            continue
        sql, params = "", []
        if day:
            sql, params = f" AND {column} = ?", [day]
        rows = db.all(
            f"""SELECT id, invoice_no, customer_name, customer_phone, total, balance_due, {column} AS due FROM invoices
                WHERE business_id=? AND kind='SALE' AND {column} IS NOT NULL AND {done} IS NULL AND COALESCE(payment_status, '') <> 'Cancelled'{sql}
                ORDER BY {column}, customer_name, id LIMIT ?""",
            [biz] + params + [limit],
        )
        items = {}
        if rows:
            for line in db.all(
                f"SELECT invoice_id, item_name, qty FROM invoice_items WHERE business_id=? AND invoice_id IN ({', '.join('?' for _ in rows)}) ORDER BY id", [biz] + [r["id"] for r in rows]
            ):
                items.setdefault(line["invoice_id"], []).append(f"{line['qty']} x {clip(line['item_name'], 40)}")
        out[name] = [
            {"day": r["due"], "overdue": r["due"] < ctx.today, "invoice_no": r["invoice_no"], "customer": clip(r["customer_name"], 60), "phone": r["customer_phone"] or "",
             "items": clip("; ".join(items.get(r["id"], [])), 160), "total": r2(r["total"]), "balance_due": r2(r["balance_due"])}
            for r in rows
        ]
    if kind in ("deliveries", "both"):
        ctx.source("Deliveries", H.url_for("deliveries"))
        if day:
            ctx.source(f"Delivery list for {day}", H.url_for("delivery_list", day=day))
    if kind in ("collections", "both"):
        ctx.source("To be collected", H.url_for("history", collect="waiting"))
    return out


@tool("Purchases of stock and other things: date, supplier, item, quantity, cost, paid, owed.",
      {"query": S("Item name, code or purchase invoice no"), "supplier": S(), "unpaid_only": B(), "period": PERIOD, "from_date": FROM, "to_date": TO, "limit": LIMIT})
def purchases(ctx, args):
    db, biz = ctx.db, ctx.biz
    q, supplier = text_of(args, "query"), text_of(args, "supplier")
    first, last, label = period(ctx, args, "all_time" if q or supplier or args.get("unpaid_only") else "this_month")
    sql, params = between_days("purchase_date", first, last)
    if q:
        sql += " AND (LOWER(name) LIKE ? OR UPPER(item_code) LIKE ? OR LOWER(COALESCE(invoice_no, '')) LIKE ?)"
        params += [like(q), like(q).upper(), like(q)]
    if supplier:
        sql += " AND LOWER(COALESCE(supplier_name, '')) LIKE ?"
        params.append(like(supplier))
    if args.get("unpaid_only"):
        sql += " AND total_cost - COALESCE(amount_paid, total_cost) > 0.005"
    totals = db.one(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(total_cost), 0) AS cost, COALESCE(SUM(COALESCE(amount_paid, total_cost)), 0) AS paid FROM procurements WHERE business_id=?{sql}", [biz] + params
    )
    rows = db.all(
        f"""SELECT purchase_date, supplier_name, invoice_no, item_code, name, quantity, unit_cost, total_cost, COALESCE(amount_paid, total_cost) AS paid, in_inventory
            FROM procurements WHERE business_id=?{sql} ORDER BY purchase_date DESC, id DESC LIMIT ?""",
        [biz] + params + [whole(args, "limit", 12)],
    )
    ctx.source("Procurements", H.url_for("procurements"))
    return {
        "period": label, "matching": totals["n"], "total_cost": r2(totals["cost"]), "paid": r2(totals["paid"]), "still_owed": r2(totals["cost"] - totals["paid"]),
        "purchases": [
            {"date": r["purchase_date"], "supplier": clip(r["supplier_name"], 60), "invoice_no": r["invoice_no"], "item": clip(r["name"], 70), "code": r["item_code"], "qty": r["quantity"],
             "unit_cost": r2(r["unit_cost"]), "total_cost": r2(r["total_cost"]), "paid": r2(r["paid"]), "owed": r2(r["total_cost"] - r["paid"]), "kept_as_stock": r["in_inventory"] != 0}
            for r in rows
        ],
    }


@tool("Work recorded in the plots: who did what, where and when.",
      {"period": PERIOD, "from_date": FROM, "to_date": TO, "worker": S(), "plot_or_work": S("Plot name or part of the work done"), "limit": LIMIT})
def daily_activities(ctx, args):
    db, biz = ctx.db, ctx.biz
    first, last, label = period(ctx, args, "today")
    sql, params = between_days("work_date", first, last)
    if text_of(args, "worker"):
        sql += " AND LOWER(worker_name) LIKE ?"
        params.append(like(text_of(args, "worker")))
    if text_of(args, "plot_or_work"):
        sql += " AND (LOWER(plot_name) LIKE ? OR LOWER(description) LIKE ?)"
        params += [like(text_of(args, "plot_or_work"))] * 2
    count = db.one(f"SELECT COUNT(*) AS n FROM activities WHERE business_id=?{sql}", [biz] + params)
    rows = db.all(
        f"""SELECT work_date, plot_name, department, worker_name, period, description, product, product_qty, product_unit FROM activities
            WHERE business_id=?{sql} ORDER BY work_date DESC, id DESC LIMIT ?""",
        [biz] + params + [whole(args, "limit", 12)],
    )
    ctx.source("Daily Activities", H.url_for("activities"))
    return {
        "period": label, "matching": count["n"], "activities": [
            {"date": r["work_date"], "plot": clip(r["plot_name"], 50), "department": r["department"], "worker": clip(r["worker_name"], 50), "when": r["period"], "what": clip(r["description"], 160),
             "product": clip(r["product"], 40) or None, "product_qty": r["product_qty"], "unit": r["product_unit"] or None}
            for r in rows
        ],
    }


@tool("This person's recent alerts, unread first.", {"limit": LIMIT})
def my_alerts(ctx, args):
    db, biz, user = ctx.db, ctx.biz, ctx.user
    mine = "(for_user_id = ? OR for_user_id IS NULL)" if user["role"] == "admin" else "for_user_id = ?"
    rows = db.all(
        f"SELECT kind, title, body, created_at, read_at FROM notifications WHERE business_id=? AND {mine} ORDER BY CASE WHEN read_at IS NULL THEN 0 ELSE 1 END, id DESC LIMIT ?",
        (biz, user["id"], whole(args, "limit", 12)),
    )
    return {"alerts": [{"when": (r["created_at"] or "")[:16], "kind": r["kind"], "title": clip(r["title"], 100), "text": clip(r["body"], 160), "unread": r["read_at"] is None} for r in rows]}


GUIDE = {
    "selling": "Sales & POS: tap Add on a product to put it in the cart, set the quantity, optionally a discount on a line (an amount like 5, or a percentage like 10%), the customer's name and phone (a "
               "name or number opens a customer account), the payment status and how it was paid (cash, bank transfer, Airtel Money, MTN Money...), and how the customer gets the goods: takes them now, "
               "collects on another day, or we deliver on a day. Press Complete Invoice. Only things listed on the POS can be sold.",
    "discounts": "On the POS each cart line has a Discount box: an amount (5) or a percentage (10%). The invoice, statements, delivery notes and Accounts all use the price after the discount.",
    "delivery": "Deliveries: choose 'We deliver' with a day on the POS. Each day's deliveries make that day's delivery list (Deliveries in the menu), everyone is alerted that morning, and it can be printed "
                "or downloaded as a PDF, as a note for each customer or one route sheet. Mark deliveries as delivered from the list or the invoice. Sales & Invoices has 'Generate delivery list': tick any "
                "invoices and make one list. A customer's statement can make one for just their invoices.",
    "collection": "Collections: choose 'Collected on another day' with a day on the POS. Everyone is reminded the day before. 'To be collected' on Sales & Invoices lists them; mark each collected from its invoice.",
    "preorders": "Pre-orders: Planting Records > Up Next & pre-orders. A pre-order holds seedlings from a batch for a customer (with an optional collection day). When the customer collects, press Generate "
                 "invoice: the POS opens with the pre-order in the cart at the agreed price and quantity.",
    "planting": "Planting Records: each batch of seeds planted in the greenhouse, with mortality, ready date and price. A batch goes on the POS a week before it is ready. Up Next shows batches still growing. "
                "Enter the trays planted and the holes in each tray (trays x holes gives the seeds) and who planted them.",
    "inventory": "Inventory lists stock items (with cost and selling price) by category; Procurements records purchases from suppliers (which add to stock unless marked as not stock) and what is paid; "
                 "Inventory Analysis shows what was bought and spent. Admins manage suppliers and their payments.",
    "customers": "Customers (admins): each customer's account with their invoices, what they owe and a statement (PDF). Names and phone numbers are matched to avoid duplicates; duplicates can be merged.",
    "who_bought": "Click any product name anywhere in the app to see who bought it: customers, phones, dates, quantities, prices and discounts, with a CSV. A planting record's Sold tile does the same.",
    "accounts": "Accounts (admins): every movement of money in and out, balances for cash, bank and mobile money (counted by hand, then kept up to date), who owes us and whom we owe, profit and the financial standing.",
    "reports": "Reports: sales, purchases and profit by department and period.",
    "reminders": "Reminders (admins): every 3 days (Settings > Alerts: off, daily, every 2 or 3 days, weekly) an alert gathers who still owes, batches ready "
                 "with seedlings left to sell, store stock with none on the POS, batches ready longer than 30 days (changeable), and pre-orders due within a "
                 "week. It opens the Reminders page, and the assistant mentions it the next time it is opened. Phones and computers can get every alert as a "
                 "notification even with the app closed: Settings > Alerts > Turn on notifications on this device.",
    "staff": "Staff (admins): employees, payroll, payslips with NAPSA and NHIMA, and statements. Daily Activities records the work done in each plot. "
             "A casual worker is paid for the trays they planted at their own rate (set when they are registered, or with Edit). Each employee has a pay "
             "schedule (monthly on a day, every two weeks, weekly, or a date set each time) and a next pay date. HR & Payroll's Pay due card shows what each "
             "is owed; on the pay date admins are alerted and the payslip is filled in (the period since the last one, unpaid trays or salary less days absent). "
             "Absences are recorded on the employee's page.",
    "alerts": "Alerts come in the app and optionally by WhatsApp (Settings): low stock, pre-orders and collections due, deliveries due, customer requests to follow up, "
              "and requests for admin permission.",
    "requests": "Customer Requests (Sell menu): log what a customer asked for that we do not have (never stocked, out of stock or not planted), with their phone, the quantity "
                "and the day to follow it up. The admins are alerted that day, and when the item is bought or planted. Open a request to follow it up again, add a note, or "
                "close it as fulfilled or dropped. Most asked for items count in the restock advice. The assistant can log one too.",
    "assistant": "The assistant (this chat) can look things up, explain the app, say what needs attention today (the sun button), what to restock and what to watch out for, and plan plantings. "
                 "It prepares sales, pre-orders, payments, delivery changes, purchases, plantings with trays, tray payslips, stock transfers, supplier payments and money entries for you to confirm; "
                 "nothing is done until you press Confirm. The camera button sends a photo or PDF: a supplier's receipt becomes a purchase, a tally of trays a planting. It writes customer "
                 "reminders for you to copy or open in WhatsApp. Tell it a preference and it remembers it (the menu lists what it remembers, to delete any). Every evening it "
                 "gives a brief of tomorrow and of what to restock. With the ear on, saying the wake phrase (set by an admin in Settings, like 'Hey Assistant') asks it a question out "
                 "loud; the stop phrase switches listening off. Hands-free mode is a spoken conversation: it answers, suggests and prepares things, and you say yes or no to confirm. "
                 "Each person picks the voice it speaks with from the menu.",
}


@tool("How to do things in the app. Leave the topic out to list them.", {"topic": E(*GUIDE)})
def app_guide(ctx, args):
    topic = args.get("topic")
    if topic not in GUIDE:
        return {"topics": list(GUIDE)}
    return {"topic": topic, "guide": GUIDE[topic]}


# ================= Read-only tools for admins =================
@tool("Customer accounts: invoices, total bought, paid, owed, last purchase. Search by name or phone, or list who owes most.", {"query": S("Name or phone"), "owing_only": B(), "limit": LIMIT}, admin=True)
def customers(ctx, args):
    db, biz = ctx.db, ctx.biz
    q, limit = text_of(args, "query"), whole(args, "limit", 10)
    sql, params = "", []
    if q:
        sql = " AND (LOWER(c.name) LIKE ? OR COALESCE(c.phone, '') LIKE ?)"
        params = [like(q), "%" + q.replace(" ", "") + "%"]
    having = " HAVING COALESCE(SUM(i.balance_due), 0) > 0.005" if args.get("owing_only") else ""
    rows = db.all(
        f"""SELECT c.id, c.name, c.phone, COUNT(i.id) AS invoices, COALESCE(SUM(i.total), 0) AS total, COALESCE(SUM(i.amount_paid), 0) AS paid,
                   COALESCE(SUM(i.balance_due), 0) AS owed, MAX(i.created_at) AS last_purchase
            FROM customers c LEFT JOIN invoices i ON i.customer_id = c.id AND i.business_id = c.business_id AND i.kind = 'SALE' AND i.payment_status <> 'Cancelled'
            WHERE c.business_id=?{sql} GROUP BY c.id, c.name, c.phone{having} ORDER BY owed DESC, total DESC LIMIT ?""",
        [biz] + params + [limit],
    )
    out = {"shown": len(rows), "customers": []}
    for r in rows:
        entry = {"name": clip(r["name"], 60), "phone": r["phone"] or "", "invoices": r["invoices"], "bought_in_all": r2(r["total"]), "paid": r2(r["paid"]), "owes": r2(r["owed"]),
                 "last_purchase": (r["last_purchase"] or "")[:10] or None}
        if q and len(rows) <= 3:
            recent = db.all(
                "SELECT invoice_no, created_at, total, balance_due, payment_status FROM invoices WHERE business_id=? AND customer_id=? AND kind='SALE' ORDER BY created_at DESC LIMIT 8", (biz, r["id"])
            )
            entry["recent_invoices"] = [{"invoice_no": x["invoice_no"], "date": (x["created_at"] or "")[:10], "total": r2(x["total"]), "balance": r2(x["balance_due"]), "status": x["payment_status"]} for x in recent]
        out["customers"].append(entry)
        if len(rows) <= 3 or len(out["customers"]) <= 2:
            ctx.source(clip(r["name"], 40), H.url_for("customer_statement", customer_id=r["id"]))
    ctx.source("Customers", H.url_for("customers"))
    return out


@tool("Supplier accounts: purchases, paid, owed, latest payments. Search by name or phone, or list whom we owe most.", {"query": S("Name or phone"), "owing_only": B(), "limit": LIMIT}, admin=True)
def suppliers(ctx, args):
    db, biz = ctx.db, ctx.biz
    q, limit = text_of(args, "query"), whole(args, "limit", 10)
    sql, params = "", []
    if q:
        sql = " AND (LOWER(s.name) LIKE ? OR COALESCE(s.phone, '') LIKE ?)"
        params = [like(q), "%" + q.replace(" ", "") + "%"]
    having = " HAVING COALESCE(SUM(p.total_cost), 0) - COALESCE(SUM(COALESCE(p.amount_paid, p.total_cost)), 0) > 0.005" if args.get("owing_only") else ""
    rows = db.all(
        f"""SELECT s.id, s.name, s.phone, COUNT(p.id) AS purchases, COALESCE(SUM(p.total_cost), 0) AS total, COALESCE(SUM(COALESCE(p.amount_paid, p.total_cost)), 0) AS paid,
                   MAX(p.purchase_date) AS last_purchase
            FROM suppliers s LEFT JOIN procurements p ON p.supplier_id = s.id AND p.business_id = s.business_id
            WHERE s.business_id=?{sql} GROUP BY s.id, s.name, s.phone{having} ORDER BY total DESC LIMIT ?""",
        [biz] + params + [limit],
    )
    out = {"shown": len(rows), "suppliers": []}
    for r in rows:
        entry = {"name": clip(r["name"], 60), "phone": r["phone"] or "", "purchases": r["purchases"], "bought_in_all": r2(r["total"]), "paid": r2(r["paid"]), "we_owe": r2(r["total"] - r["paid"]),
                 "last_purchase": r["last_purchase"]}
        if q and len(rows) <= 3:
            pays = db.all("SELECT amount, method, paid_on FROM supplier_payments WHERE business_id=? AND supplier_id=? ORDER BY id DESC LIMIT 5", (biz, r["id"]))
            entry["latest_payments"] = [{"date": p["paid_on"], "amount": r2(p["amount"]), "method": p["method"]} for p in pays]
        out["suppliers"].append(entry)
        if len(rows) <= 3 or len(out["suppliers"]) <= 2:
            ctx.source(clip(r["name"], 40), H.url_for("supplier_statement", supplier_id=r["id"]))
    ctx.source("Suppliers", H.url_for("suppliers"))
    return out


@tool("Money held, who owes us, whom we owe, stock at cost, worth; and for a period money in and out and profit.", {"period": PERIOD, "from_date": FROM, "to_date": TO}, admin=True)
def financial_overview(ctx, args):
    db, biz = ctx.db, ctx.biz
    first, last, label = period(ctx, args)
    standing = H.financial_standing(db, biz, first, last)
    summary = H.account_summary(db, biz, first, last)
    held = standing["held"]
    profit = standing["profit"]
    ctx.source("Accounts", H.url_for("accounts"))
    return {
        "period": label,
        "money_held": {"total": r2(held["amount"]), "counted_by_hand": bool(held["counted"]), "accounts": [{"account": a, "amount": r2(v)} for a, v in held["accounts"]]},
        "customers_owe_us": {"total": r2(standing["receivable"]), "most": [{"customer": clip(o["name"], 50), "owes": r2(o["owed"]), "invoices": o["invoices"]} for o in standing["owing"][:8]]},
        "we_owe_suppliers": {"total": r2(standing["payable"]), "most": [{"supplier": clip(o["name"], 50), "owed": r2(o["owed"])} for o in standing["owed"][:8]]},
        "stock_at_cost": r2(standing["stock"]["total"]), "loans_to_repay": r2(standing["loans"]), "worth": r2(standing["worth"]),
        "period_money": {"in": r2(summary["in"]), "out": r2(summary["out"]), "net": r2(summary["net"]),
                         "came_from": [{"source": s["label"], "amount": r2(s["total"])} for s in summary["sources"][:8]], "went_on": [{"purpose": s["label"], "amount": r2(s["total"])} for s in summary["purposes"][:8]]},
        "period_profit": {"earned": [{"what": w, "amount": r2(v)} for w, v in profit["earned"]], "spent": [{"what": w, "amount": r2(v)} for w, v in profit["spent"]],
                          "total_earned": r2(profit["total_earned"]), "total_spent": r2(profit["total_spent"]), "profit": r2(profit["profit"])},
    }


@tool("Money in and out entries in Accounts: date, amount, method, category, party, purpose. For 'what did we spend on...'.",
      {"query": S("Party, purpose or category"), "direction": E("in", "out"), "period": PERIOD, "from_date": FROM, "to_date": TO, "limit": LIMIT}, admin=True)
def cashbook_entries(ctx, args):
    db, biz = ctx.db, ctx.biz
    first, last, label = period(ctx, args)
    sql, params = between("occurred_at", first, last)
    if args.get("direction") in ("in", "out"):
        sql += " AND direction = ?"
        params.append(args["direction"])
    q = text_of(args, "query")
    if q:
        sql += " AND (LOWER(COALESCE(party, '')) LIKE ? OR LOWER(COALESCE(description, '')) LIKE ? OR LOWER(category) LIKE ?)"
        params += [like(q)] * 3
    totals = db.one(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN direction = 'in' THEN amount ELSE 0 END), 0) AS money_in,
                   COALESCE(SUM(CASE WHEN direction = 'out' THEN amount ELSE 0 END), 0) AS money_out FROM cashbook WHERE business_id=?{sql}""",
        [biz] + params,
    )
    rows = db.all(
        f"SELECT occurred_at, direction, amount, method, category, party, description FROM cashbook WHERE business_id=?{sql} ORDER BY occurred_at DESC, id DESC LIMIT ?",
        [biz] + params + [whole(args, "limit", 12)],
    )
    ctx.source("Accounts", H.url_for("accounts"))
    return {
        "period": label, "matching": totals["n"], "money_in": r2(totals["money_in"]), "money_out": r2(totals["money_out"]),
        "entries": [{"when": (r["occurred_at"] or "")[:16], "direction": r["direction"], "amount": r2(r["amount"]), "method": r["method"], "category": r["category"],
                     "party": clip(r["party"], 50), "what": clip(r["description"], 100)} for r in rows],
    }


@tool("Employees and payroll for a month: pay, NAPSA, NHIMA, net. Never shows ID numbers or addresses.", {"employee": S(), "month": S("YYYY-MM"), "limit": LIMIT}, admin=True)
def staff(ctx, args):
    db, biz = ctx.db, ctx.biz
    who = text_of(args, "employee")
    month = text_of(args, "month", 7)
    month = month if len(month) == 7 and month[4] == "-" else ctx.today[:7]
    sql, params = "", []
    if who:
        sql, params = " AND LOWER(full_name) LIKE ?", [like(who)]
    people = db.all(
        f"SELECT {H.EMPLOYEE_COLUMNS} FROM employees WHERE business_id=?{sql} ORDER BY full_name LIMIT ?", [biz] + params + [whole(args, "limit", 12)]
    )
    hr_set = H.hr_settings_for(db, biz)
    pay_sql, pay_params = "", []
    if who:
        pay_sql, pay_params = " AND LOWER(employee_name) LIKE ?", [like(who)]
    pay = db.all(
        f"""SELECT employee_name, calculated_basic, napsa_amt, nhima_amt, net_payable, worked_days, expected_days, trays, tray_rate FROM payroll
            WHERE business_id=? AND pay_month=?{pay_sql} ORDER BY employee_name""",
        [biz, month] + pay_params,
    )
    # Casual workers are paid for the trays they planted: those not on a payslip yet
    owed = {r["employee_id"]: r["n"] for r in db.all(
        "SELECT employee_id, SUM(trays) AS n FROM planting_trays WHERE business_id=? AND paid_payslip_id IS NULL GROUP BY employee_id", (biz,)
    )}
    ctx.source("Payroll", H.url_for("hr"))
    return {
        "employees": [{"name": clip(p["full_name"], 60), "type": p["employment_type"], "salary_each_pay": r2(p["basic_salary"]) if p["employment_type"] != "Casual Worker" else None,
                       "pay_per_tray": r2(H.rate_for(p, hr_set)) if p["employment_type"] == "Casual Worker" else None, "paid": H.schedule_words(p),
                       "next_pay_date": p["next_pay_date"], "owed_at_next_pay": r2(H.pay_preview(db, biz, p, hr_set, p["next_pay_date"] or ctx.today)["amount"]),
                       "started": p["start_date"] or None, "days_a_week": p["expected_days_week"],
                       "unpaid_trays": r2(owed.get(p["id"])) if p["employment_type"] == "Casual Worker" else None} for p in people],
        "payroll_month": month, "pay_per_tray": r2(hr_set["tray_rate"]) or None,
        "paid_this_month": [{"name": clip(p["employee_name"], 60), "basic_for_days_worked": r2(p["calculated_basic"]) if p["trays"] is None else None,
                             "worked_days": p["worked_days"] if p["trays"] is None else None, "of_days": p["expected_days"] if p["trays"] is None else None,
                             "basic_for_trays": r2(p["calculated_basic"]) if p["trays"] is not None else None, "trays_planted": p["trays"], "pay_per_tray": p["tray_rate"] if p["trays"] is not None else None,
                             "napsa": r2(p["napsa_amt"]), "nhima": r2(p["nhima_amt"]), "net_pay": r2(p["net_payable"])} for p in pay],
        "total_net_pay": r2(sum(p["net_payable"] for p in pay)),
    }


@tool("Stock adjustments, deleted invoices or operators' permission requests.",
      {"kind": E("stock_adjustments", "deleted_invoices", "permission_requests"), "period": PERIOD, "from_date": FROM, "to_date": TO, "limit": LIMIT}, required=["kind"], admin=True)
def audit_log(ctx, args):
    db, biz = ctx.db, ctx.biz
    kind = choice(args, "kind", ("stock_adjustments", "deleted_invoices", "permission_requests"), "")
    if not kind:
        raise ToolError("Which record: stock_adjustments, deleted_invoices or permission_requests?")
    first, last, label = period(ctx, args, "this_month")
    limit = whole(args, "limit", 12)
    if kind == "stock_adjustments":
        sql, params = between("created_at", first, last)
        rows = db.all(f"SELECT created_at, kind, item_name, item_code, quantity_before, quantity_after, delta, reason, note, created_by FROM stock_adjustments WHERE business_id=?{sql} ORDER BY id DESC LIMIT ?", [biz] + params + [limit])
        ctx.source("Stock Adjustments", H.url_for("stock_adjustments"))
        return {"period": label, "adjustments": [{"when": (r["created_at"] or "")[:16], "item": clip(r["item_name"], 60), "code": r["item_code"], "from": r["quantity_before"], "to": r["quantity_after"],
                                                  "change": r["delta"], "reason": clip(r["reason"], 80), "note": clip(r["note"], 80), "by": r["created_by"]} for r in rows]}
    if kind == "deleted_invoices":
        sql, params = between("deleted_at", first, last)
        rows = db.all(f"SELECT invoice_no, customer_name, total, reason, deleted_by, approved_by, deleted_at FROM deleted_invoices WHERE business_id=?{sql} ORDER BY id DESC LIMIT ?", [biz] + params + [limit])
        ctx.source("Deletion Records", H.url_for("deleted_invoices"))
        return {"period": label, "deleted": [{"invoice_no": r["invoice_no"], "customer": clip(r["customer_name"], 60), "total": r2(r["total"]), "reason": clip(r["reason"], 100),
                                              "deleted_by": r["deleted_by"], "approved_by": r["approved_by"] or None, "when": (r["deleted_at"] or "")[:16]} for r in rows]}
    sql, params = between("created_at", first, last)
    rows = db.all(f"SELECT username, action, item_name, reason, status, created_at, decided_by FROM permission_requests WHERE business_id=?{sql} ORDER BY id DESC LIMIT ?", [biz] + params + [limit])
    ctx.source("Requests", H.url_for("permission_requests"))
    return {"period": label, "requests": [{"when": (r["created_at"] or "")[:16], "operator": r["username"], "wants_to": r["action"], "item": clip(r["item_name"], 60), "reason": clip(r["reason"], 100),
                                           "status": r["status"], "decided_by": r["decided_by"] or None} for r in rows]}


# ================= Prepare tools: they write a card for the person to confirm, and do nothing else =================
def shown(card_title, rows):
    """What the model is told after preparing something."""
    return {"prepared": True, "waiting_for": "the person to press Confirm on the card shown to them", "what": card_title, "details": rows,
            "say": "Tell the person briefly what you prepared and that nothing happens until they press Confirm."}


def pos_product(ctx, term):
    """The POS listing a person means: its code, or its name when only one product fits. Otherwise a ToolError that lists what fits."""
    term = clip(term, 80)
    if not term:
        raise ToolError("Which product?")
    db, biz = ctx.db, ctx.biz
    exact = db.all("SELECT id, item_code, name, quantity, price FROM pos_products WHERE business_id=? AND (UPPER(item_code) = ? OR LOWER(name) = ?)", (biz, term.upper(), term.lower()))
    found = exact or db.all("SELECT id, item_code, name, quantity, price FROM pos_products WHERE business_id=? AND (LOWER(name) LIKE ? OR UPPER(item_code) LIKE ?) ORDER BY name LIMIT 6", (biz, like(term), like(term).upper()))
    if not found:
        raise ToolError(f"'{term}' is not listed on the POS, and only things listed there can be sold.")
    if len(found) > 1:
        raise ToolError("More than one product fits '" + term + "': " + "; ".join(f"{p['name']} (code {p['item_code']}, {p['quantity']} in stock, {ctx.money(p['price'])})" for p in found) + ". Ask the person which one.")
    return found[0]


def money_method(args, needed):
    """How a payment was made, as the app names it. A ToolError that lists the choices when it is needed and missing or unknown."""
    raw = text_of(args, "payment_method", 30)
    method = H.method_from_text(raw) if raw else None
    if needed and method is None:
        raise ToolError("Ask the person how it was paid: " + ", ".join(H.METHOD_NAMES) + ".")
    return method


@tool("Prepare a sale for the person to confirm (nothing is sold until they press Confirm). Items must be on the POS.",
      {"items": {"type": "array", "items": {"type": "object", "properties": {"product": S("Code or name"), "quantity": N(), "discount": S("Amount like 5 or percentage like 10%")}, "required": ["product", "quantity"]}},
       "customer_name": S(), "customer_phone": S(), "payment_status": E("Paid", "Unpaid", "Partial Payment"), "amount_paid": {"type": "number", "description": "For a partial payment: paid now"},
       "payment_method": S("Cash, Bank transfer, Airtel Money, MTN Money, Cheque, Card or Other; needed unless unpaid"), "payment_reference": S(),
       "handover": E("now", "later", "deliver"), "day": S("YYYY-MM-DD, for later or deliver")},
      required=["items"], perm="sell")
def prepare_sale(ctx, args):
    items = args.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        raise ToolError("Give the items to sell (1 to 12).")
    cart, rows, total, seen = [], [], 0.0, set()
    for it in items:
        it = it if isinstance(it, dict) else {}
        product = pos_product(ctx, it.get("product"))
        try:
            qty = int(it.get("quantity"))
        except (TypeError, ValueError):
            qty = 0
        if product["id"] in seen:
            raise ToolError(f"{product['name']} is listed twice: give it once with the total quantity.")
        seen.add(product["id"])
        if qty < 1 or qty > product["quantity"]:
            raise ToolError(f"{product['name']}: {product['quantity']} in stock, so {qty} cannot be sold.")
        gross = round(qty * product["price"], 2)
        said = text_of(it, "discount", 20)
        try:
            off = H.read_discount(said, gross)
        except ValueError:
            raise ToolError(f"The discount on {product['name']} must be an amount up to {ctx.money(gross)}, or a percentage like 10%.")
        line = round(gross - off, 2)
        total += line
        cart.append(dict({"id": product["id"], "qty": qty}, **({"discount": said} if said else {})))
        rows.append([f"{qty} × {product['name']}", ctx.money(line) + (f" (after {ctx.money(off)} off)" if off else "")])
    total = round(total, 2)
    status = choice(args, "payment_status", H.PAYMENT_STATUSES, "Paid")
    form = {"cart": json.dumps(cart), "customer_name": text_of(args, "customer_name", 100), "customer_phone": text_of(args, "customer_phone", 40), "payment_status": status}
    if status == "Partial Payment":
        try:
            paid = round(float(args.get("amount_paid")), 2)
        except (TypeError, ValueError):
            paid = 0.0
        if not 0 < paid < total:
            raise ToolError(f"For a partial payment, say how much was paid now: more than nothing and less than {ctx.money(total)}.")
        form["amount_paid"] = paid
    method = money_method(args, status != "Unpaid")
    if status != "Unpaid":
        form["payment_method"], form["payment_reference"] = method, text_of(args, "payment_reference", 60)
    handover = choice(args, "handover", ("now", "later", "deliver"), "now")
    form["collection"] = handover
    if handover != "now":
        day = H.day_or_none(str(args.get("day") or ""))
        earliest = H.add_days(ctx.today, 1) if handover == "later" else ctx.today
        if day is None or day < earliest:
            raise ToolError(f"Ask the person for the day: it must be {earliest} or later (as YYYY-MM-DD).")
        form["collect_on" if handover == "later" else "deliver_on"] = day
    head = [["Customer", form["customer_name"] or "Walk-in customer"]] + ([["Phone", form["customer_phone"]]] if form["customer_phone"] else [])
    tail = [["Total", ctx.money(total)], ["Payment", status + (f", {ctx.money(form['amount_paid'])} paid" if "amount_paid" in form else "") + (f" by {method}" if status != "Unpaid" else "")]]
    if handover != "now":
        tail.append(["Goods", ("To be collected on " if handover == "later" else "To be delivered on ") + day])
    title = "Make a sale for " + (form["customer_name"] or "a walk-in customer")
    ctx.propose("sale", "sell", title, head + rows + tail, "/checkout", form)
    return shown(title, head + rows + tail)


def planting_for(ctx, term):
    term = clip(term, 80)
    if not term:
        raise ToolError("Which batch?")
    rows = ctx.db.all(
        "SELECT * FROM plantings WHERE business_id=? AND still_in_greenhouse=1 AND (LOWER(name) LIKE ? OR LOWER(batch_no) LIKE ? OR UPPER(pos_code) = ?) ORDER BY ready_on, id LIMIT 8",
        (ctx.biz, like(term), like(term), term.upper()),
    )
    figures = H.with_figures(ctx.db, ctx.biz, rows)
    if not figures:
        raise ToolError(f"No batch in the greenhouse matches '{term}'.")
    if len(figures) > 1:
        raise ToolError("More than one batch fits '" + term + "': " + "; ".join(f"{f['label']} ({f['available']} free, {f['stage_label'].lower()})" for f in figures) + ". Ask the person which one.")
    return figures[0]


@tool("Prepare a seedling pre-order for the person to confirm.",
      {"batch": S("Plant, batch no or POS code"), "customer_name": S(), "customer_phone": S(), "quantity": N(), "collect_on": S("YYYY-MM-DD, not before the batch is ready"), "note": S()},
      required=["batch", "customer_name", "quantity"], perm="sell")
def prepare_preorder(ctx, args):
    f = planting_for(ctx, args.get("batch"))
    name = text_of(args, "customer_name", 100)
    try:
        qty = int(args.get("quantity"))
    except (TypeError, ValueError):
        qty = 0
    if not name:
        raise ToolError("Ask for the customer's name.")
    if not 1 <= qty <= f["available"]:
        raise ToolError(f"{f['label']} has {f['available']} seedlings free, so {qty} cannot be pre-ordered.")
    form = {"customer_name": name, "customer_phone": text_of(args, "customer_phone", 40), "qty": qty, "note": text_of(args, "note", 200), "back": "detail"}
    rows = [["Customer", name]] + ([["Phone", form["customer_phone"]]] if form["customer_phone"] else []) + [["Seedlings", f"{qty} × {f['label']}"], ["Price each", ctx.money(f["price"])]]
    if args.get("collect_on"):
        day = H.day_or_none(str(args["collect_on"]))
        ready = f["actual_ready_on"] or f["ready_on"] or ""
        if day is None or day < ctx.today or (ready and day < ready):
            raise ToolError(f"The collection day must be today or later, and not before the batch is ready on {ready or 'its ready day'}.")
        form["collect_on"] = day
        rows.append(["Collect on", day])
    title = f"Pre-order {qty} × {f['label']} for {name}"
    ctx.propose("preorder", "sell", title, rows, f"/planting/{f['id']}/preorder", form)
    return shown(title, rows)


# ================= Customer requests: things customers asked for that we do not have =================
@tool("Customer requests: items customers asked for that we do not have, with the follow-up day and what we have of it now.",
      {"status": E("due", "open", "closed", "all"), "item": S(), "customer": S(), "limit": LIMIT})
def customer_requests(ctx, args):
    db, biz = ctx.db, ctx.biz
    status, item, who = choice(args, "status", ("due", "open", "closed", "all"), "open"), text_of(args, "item", 60), text_of(args, "customer", 60)
    sql, params = "", []
    if status in ("due", "open"):
        sql += " AND status='open'"
    elif status == "closed":
        sql += " AND status<>'open'"
    if status == "due":
        sql += " AND follow_up_on <= ?"
        params.append(ctx.today)
    if who:
        sql += " AND (LOWER(customer_name) LIKE ? OR customer_phone LIKE ?)"
        params += [like(who), like(who)]
    rows = db.all(f"SELECT * FROM customer_requests WHERE business_id=?{sql} ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, follow_up_on, id DESC LIMIT 300",
                  [biz] + params)
    if item:
        rows = [r for r in rows if H.request_fits(item, r["item_name"]) or H.request_fits(r["item_name"], item)]
    rows = H.requests_with_stock(db, biz, rows)
    ctx.source("Customer requests", H.url_for("customer_requests", show="due" if status == "due" else "open"))
    return {
        "today": ctx.today, "matching": len(rows), "requests": [
            {"no": r["id"], "customer": clip(r["customer_name"], 60), "phone": r["customer_phone"] or "", "item": clip(r["item_name"], 60), "quantity": r["quantity"],
             "follow_up_on": r["follow_up_on"], "status": r["status"], "logged": (r["created_at"] or "")[:10], "note": clip(r["note"], 80),
             **({"in_stock": r["in_stock"]} if r["in_stock"] else {}), **({"seedlings_free": r["growing"], "ready": r["growing_ready"]} if r["growing"] else {})}
            for r in rows[:whole(args, "limit", 12)]
        ],
        "most_asked_for": [{"item": clip(t["item"], 60), "customers": t["customers"], "quantity": t["quantity"] or None}
                           for t in H.most_requested([r for r in rows if r["status"] == "open"])[:5]],
    }


@tool("Prepare a customer request (an item a customer wants that we do not have) for the person to confirm. Ask for the follow-up day if not given.",
      {"customer_name": S(), "customer_phone": S(), "item": S("What they want"), "quantity": N("Leave out if not said"), "follow_up_on": S("YYYY-MM-DD"), "note": S()},
      required=["customer_name", "item", "follow_up_on"], perm="sell")
def prepare_customer_request(ctx, args):
    name, wanted = text_of(args, "customer_name", 100), text_of(args, "item", 100)
    if not name:
        raise ToolError("Ask for the customer's name.")
    if not wanted:
        raise ToolError("Ask what the customer wants.")
    day = H.day_or_none(str(args.get("follow_up_on") or ""))
    if day is None or day < ctx.today:
        raise ToolError(f"Ask the person which day to follow it up: {ctx.today} or later (as YYYY-MM-DD).")
    item_name, code = H.known_item(ctx.db, ctx.biz, wanted)
    form = {"customer_name": name, "customer_phone": text_of(args, "customer_phone", 40), "item_name": item_name, "follow_up_on": day,
            "note": text_of(args, "note", 300), "source": "assistant"}
    rows = [["Customer", name]] + ([["Phone", form["customer_phone"]]] if form["customer_phone"] else []) + [["Wants", item_name]]
    if args.get("quantity") not in (None, ""):
        try:
            qty = int(args.get("quantity"))
        except (TypeError, ValueError):
            qty = 0
        if qty < 1:
            raise ToolError("The quantity must be a whole number above zero, or left out.")
        form["quantity"] = qty
        rows.append(["Quantity", f"{qty:,}"])
    rows.append(["Follow up on", day])
    if form["note"]:
        rows.append(["Note", form["note"]])
    have = H.requests_with_stock(ctx.db, ctx.biz, [{"item_name": item_name, "item_code": code}])[0]
    if have["have_now"]:
        rows.append(["We have now", ", ".join(x for x in (f"{have['in_stock']:,} in stock" if have["in_stock"] else "",
                                                           f"{have['growing']:,} seedlings free, ready {have['growing_ready']}" if have["growing"] else "") if x)])
    title = f"Log a request: {name} wants " + (f"{form['quantity']:,} x " if "quantity" in form else "") + item_name
    ctx.propose("customer_request", "sell", title, rows, "/customer-requests", form)
    return shown(title, rows)


@tool("Prepare a change to an open customer request for the person to confirm: follow it up on another day, or close it as fulfilled or dropped.",
      {"request_no": N("From customer_requests"), "change": E("follow_up", "fulfilled", "dropped"), "day": S("New follow-up day, YYYY-MM-DD"), "note": S()},
      required=["request_no", "change"], perm="sell")
def prepare_request_update(ctx, args):
    try:
        number = int(args.get("request_no"))
    except (TypeError, ValueError):
        number = 0
    r = ctx.db.one("SELECT * FROM customer_requests WHERE id=? AND business_id=?", (number, ctx.biz))
    if r is None:
        raise ToolError("There is no such request: look it up with customer_requests first.")
    if r["status"] != "open":
        raise ToolError(f"That request is already {r['status']}.")
    change = choice(args, "change", ("follow_up", "fulfilled", "dropped"), "")
    note = text_of(args, "note", 200)
    rows = [["Customer", clip(r["customer_name"], 60)], ["Wants", H.request_label(r)]]
    if change == "follow_up":
        day = H.day_or_none(str(args.get("day") or ""))
        if day is None or day < ctx.today:
            raise ToolError(f"Ask for the new follow-up day: {ctx.today} or later.")
        form, path = {"follow_up_on": day, "note": note}, f"/customer-requests/{r['id']}/follow-up"
        rows.append(["Follow up on", f"{day} (was {r['follow_up_on']})"])
        title = f"Follow up {r['customer_name']}'s request on {day}"
    elif change in ("fulfilled", "dropped"):
        form, path = {"outcome": change, "note": note}, f"/customer-requests/{r['id']}/close"
        rows.append(["Close as", "Fulfilled" if change == "fulfilled" else "Dropped"])
        title = f"Mark {r['customer_name']}'s request {change}"
    else:
        raise ToolError("Say the change: follow_up (with a day), fulfilled or dropped.")
    if note:
        rows.append(["Note", note])
    ctx.propose("request_update", "sell", title, rows, path, form)
    return shown(title, rows)


def sale_to_change(ctx, args):
    """The sale a delivery or payment change is about, with a ToolError when it is not in a state to change."""
    number = text_of(args, "invoice_no", 40).upper()
    inv = ctx.db.one(
        "SELECT id, invoice_no, customer_name, total, amount_paid, balance_due, payment_status, deliver_on, delivered_at, collect_on, collected_at FROM invoices WHERE business_id=? AND kind='SALE' AND UPPER(invoice_no)=?",
        (ctx.biz, number),
    )
    if inv is None:
        raise ToolError(f"There is no sales invoice numbered {number}.")
    if inv["payment_status"] == "Cancelled":
        raise ToolError(f"{inv['invoice_no']} is cancelled, so nothing can be changed on it.")
    return inv


@tool("Prepare a payment received on an invoice, for the person to confirm.",
      {"invoice_no": S(), "amount_received": {"type": "number"}, "payment_method": S("Cash, Bank transfer, Airtel Money, MTN Money, Cheque, Card or Other"), "payment_reference": S(), "note": S()},
      required=["invoice_no", "amount_received", "payment_method"], admin=True)
def prepare_payment(ctx, args):
    inv = sale_to_change(ctx, args)
    try:
        amount = round(float(args.get("amount_received")), 2)
    except (TypeError, ValueError):
        amount = 0.0
    if inv["balance_due"] < 0.005:
        raise ToolError(f"{inv['invoice_no']} is already fully paid.")
    if not 0 < amount <= inv["balance_due"] + 0.005:
        raise ToolError(f"The amount must be more than nothing and at most the {ctx.money(inv['balance_due'])} still due.")
    method = money_method(args, True)
    new_paid = round(min(inv["total"], inv["amount_paid"] + amount), 2)
    form = {"amount_paid": new_paid, "payment_method": method, "payment_reference": text_of(args, "payment_reference", 60), "note": text_of(args, "note", 120) or "Recorded with the assistant"}
    if new_paid >= inv["total"] - 0.005:
        form["mark_paid"] = "1"
    rows = [["Invoice", inv["invoice_no"]], ["Customer", clip(inv["customer_name"], 60)], ["Received now", f"{ctx.money(amount)} by {method}"],
            ["Still due after this", ctx.money(round(inv["total"] - new_paid, 2))]]
    title = f"Record {ctx.money(amount)} received on {inv['invoice_no']}"
    ctx.propose("payment", "admin", title, rows, f"/invoices/{inv['id']}/payment", form)
    return shown(title, rows)


@tool("Prepare a change to a sale's delivery or collection, for the person to confirm.",
      {"invoice_no": S(), "change": E("mark_delivered", "mark_collected", "set_delivery_day", "set_collection_day"), "day": S("New day, YYYY-MM-DD")}, required=["invoice_no", "change"], perm="sell")
def prepare_handover_change(ctx, args):
    inv = sale_to_change(ctx, args)
    change = choice(args, "change", ("mark_delivered", "mark_collected", "set_delivery_day", "set_collection_day"), "")
    delivery = change in ("mark_delivered", "set_delivery_day")
    due, done = ("deliver_on", "delivered_at") if delivery else ("collect_on", "collected_at")
    if not change:
        raise ToolError("Say what to change: mark_delivered, mark_collected, set_delivery_day or set_collection_day.")
    if not inv[due] or inv[done]:
        raise ToolError(f"{inv['invoice_no']} is not waiting to be {'delivered' if delivery else 'collected'}.")
    form, rows = {}, [["Invoice", inv["invoice_no"]], ["Customer", clip(inv["customer_name"], 60)]]
    if change.startswith("mark"):
        path, title = f"/invoices/{inv['id']}/{'delivered' if delivery else 'collected'}", f"Mark {inv['invoice_no']} as {'delivered' if delivery else 'collected'}"
    else:
        day = H.day_or_none(str(args.get("day") or ""))
        if day is None or day < ctx.today:
            raise ToolError("Ask the person for the new day: today or later, as YYYY-MM-DD.")
        path = f"/invoices/{inv['id']}/{'deliver-day' if delivery else 'collect-day'}"
        form[due] = day
        title = f"Move {'delivery' if delivery else 'collection'} of {inv['invoice_no']} to {day}"
        rows.append(["Was", inv[due]])
        rows.append(["New day", day])
    ctx.propose("handover", "sell", title, rows, path, form)
    return shown(title, rows)


# ================= More prepare tools: purchases from a receipt, plantings with trays, tray pay, stock moves, supplier payments, money =================
NUM = {"type": "number"}
METHOD = S("Cash, Bank transfer, Airtel Money, MTN Money, Cheque, Card or Other")


def number_of(value, kind=float):
    """A number the model gave, or None when it is not one."""
    try:
        out = kind(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def inventory_item(ctx, term):
    """The inventory item a person means (by code, or a name only one item fits), None when nothing fits, a ToolError when several do."""
    term = clip(term, 80)
    if not term:
        return None
    db, biz = ctx.db, ctx.biz
    cols = "id, item_code, name, category, department, quantity, unit_cost, price, seeds_per_pack, loose_seeds"
    found = db.all(f"SELECT {cols} FROM products WHERE business_id=? AND (UPPER(item_code) = ? OR LOWER(name) = ?)", (biz, term.upper(), term.lower()))
    found = found or db.all(f"SELECT {cols} FROM products WHERE business_id=? AND (LOWER(name) LIKE ? OR UPPER(item_code) LIKE ?) ORDER BY name LIMIT 6",
                            (biz, like(term), like(term).upper()))
    if len(found) > 1:
        raise ToolError("More than one inventory item fits '" + term + "': " + "; ".join(f"{p['name']} (code {p['item_code']})" for p in found) + ". Ask the person which one.")
    return found[0] if found else None


def new_item_code(ctx, name, taken):
    """A code for a new inventory item, made from its name, used by nothing else."""
    base = H.seed_code_for(name)[:16]
    code, n = base, 2
    while code in taken or ctx.db.one("SELECT 1 AS x FROM products WHERE business_id=? AND item_code=?", (ctx.biz, code)) \
            or ctx.db.one("SELECT 1 AS x FROM pos_products WHERE business_id=? AND item_code=?", (ctx.biz, code)):
        code, n = f"{base}-{n}", n + 1
    taken.add(code)
    return code


def employee_named(ctx, who, staff):
    """The employee a name means, only when it is exactly theirs. A name that is only close is never used without the person saying so."""
    how, found = H.match_person(staff, who)
    if how == "same":
        return found[0]
    if how == "close":
        raise ToolError(f"'{who}' is not exactly the name of an employee; it is close to " + " or ".join(e["full_name"] for e in found)
                        + ". Ask the person who it is, then prepare it again with the name as registered.")
    if how == "many":
        raise ToolError(f"More than one employee is called {who}. Ask the person to do this one on its page, where they can choose.")
    raise ToolError(f"No employee is called '{who}'. Ask who it is; someone new is registered under HR & Payroll first.")


PURCHASE_LINE = {"type": "object", "properties": {
    "product": S("Inventory item's code or name; leave out for a new item"), "name": S("New item's name"), "quantity": N(), "unit_cost": NUM,
    "selling_price": {"type": "number", "description": "New stock item"}, "category": S("New item"), "seeds_per_pack": N("New seed item"),
    "stock": B("False for things not kept in stock, like fuel or repairs")}, "required": ["quantity", "unit_cost"]}


@tool("Prepare a purchase (one supplier's receipt, 1-12 lines) for the person to confirm. Lines are matched to inventory items, or become new items.",
      {"items": {"type": "array", "items": PURCHASE_LINE}, "supplier": S(), "supplier_phone": S(), "receipt_no": S(), "date": S("YYYY-MM-DD"),
       "payment_status": E("Paid", "Unpaid", "Partial Payment"), "amount_paid": {"type": "number", "description": "Part payment, one-line receipts only"},
       "payment_method": METHOD, "payment_reference": S("Defaults to the receipt number")},
      required=["items"], perm="procure")
def prepare_purchase(ctx, args):
    items = args.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        raise ToolError("Give the receipt's lines (1 to 12).")
    status = choice(args, "payment_status", H.PAYMENT_STATUSES, "Paid")
    if status == "Partial Payment" and len(items) > 1:
        raise ToolError("A part payment can only be prepared for a one-line receipt. Ask whether it was paid in full or not at all.")
    supplier, phone, receipt = text_of(args, "supplier", 100), text_of(args, "supplier_phone", 40), text_of(args, "receipt_no", 40)
    day = H.day_or_none(str(args["date"])) if args.get("date") else ctx.today
    if day is None or day > ctx.today:
        raise ToolError("The receipt's date must be today or earlier, as YYYY-MM-DD.")
    method = money_method(args, status != "Unpaid")
    reference = text_of(args, "payment_reference", 60) or receipt
    if status != "Unpaid" and supplier and not reference:
        raise ToolError("Ask for the receipt number: a payment to a named supplier is recorded with it.")
    departments = H.department_names(ctx.db, ctx.biz)
    taken, steps, rows, total = set(), [], [], 0.0
    for it in items:
        it = it if isinstance(it, dict) else {}
        qty, cost = number_of(it.get("quantity"), int), number_of(it.get("unit_cost"))
        if not qty or qty < 1 or cost is None or cost < 0:
            raise ToolError("Every line needs a whole quantity above zero and a unit cost.")
        cost = round(cost, 2)
        form = {"quantity": qty, "unit_cost": cost, "supplier_name": supplier, "supplier_phone": phone, "invoice_no": receipt, "purchase_date": day,
                "payment_status": status, "origin": "procurements"}
        if status == "Partial Payment":
            paid = number_of(args.get("amount_paid"))
            if paid is None or not 0 < paid < qty * cost:
                raise ToolError(f"For a part payment say how much was paid: more than nothing and less than {ctx.money(qty * cost)}.")
            form["amount_paid"] = round(paid, 2)
        if status != "Unpaid":
            form["payment_method"], form["payment_reference"] = method, reference
        note = ""
        if it.get("stock") is False:
            name = text_of(it, "name", 100) or text_of(it, "product", 100)
            if not name:
                raise ToolError("A line that is not stock still needs a name, such as Diesel.")
            form.update(name=name, item_code="", in_inventory="0")
            note = " (not stock)"
        else:
            known = inventory_item(ctx, it.get("product")) if it.get("product") else None
            if known:
                form.update(item_code=known["item_code"], name=known["name"], in_inventory=["0", "1"])
                if number_of(it.get("selling_price")) is not None:
                    form["price"] = round(number_of(it.get("selling_price")), 2)
                name = known["name"]
            else:
                name = text_of(it, "name", 100) or text_of(it, "product", 100)
                category = text_of(it, "category", 60)
                price = number_of(it.get("selling_price"))
                if not name:
                    raise ToolError("A new item needs a name.")
                if not category or price is None or price < 0:
                    raise ToolError(f"'{name}' is not in inventory. Ask whether it is new; if it is, ask for its category and selling price.")
                form.update(item_code=new_item_code(ctx, name, taken), name=name, category=category, price=round(price, 2), in_inventory=["0", "1"],
                            department="Greenhouse" if H.is_seed_category(category) and "Greenhouse" in departments else "General")
                if H.is_seed_category(category):
                    spp = number_of(it.get("seeds_per_pack"), int)
                    if not spp or spp < 1:
                        raise ToolError(f"'{name}' is a new seed item: ask how many seeds are in one pack.")
                    form["seeds_per_pack"] = spp
                note = f" (new item {form['item_code']}, {category}, sells at {ctx.money(price)})"
        line = round(qty * cost, 2)
        total += line
        steps.append(("/inventory/purchase", form))
        rows.append([f"{qty} × {name}{note}", f"{ctx.money(cost)} each = {ctx.money(line)}"])
    head = [["Supplier", supplier or "Not named"]] + ([["Receipt no", receipt]] if receipt else []) + [["Date", day]]
    paid_text = {"Paid": f"Paid by {method}", "Unpaid": "Not paid yet", "Partial Payment": f"{ctx.money(steps[0][1].get('amount_paid', 0))} paid by {method}"}[status]
    tail = [["Total", ctx.money(total)], ["Payment", paid_text]]
    if len(steps) > 1:
        tail.append(["Note", "Each line is saved as its own purchase; if one is refused, the lines before it stay saved."])
    title = "Record a purchase" + (f" from {supplier}" if supplier else "") + (f" (receipt {receipt})" if receipt else "")
    ctx.propose("purchase", "procure", title, head + rows + tail, None, None, steps=steps)
    return shown(title, head + rows + tail)


PLANTER = {"type": "object", "properties": {"name": S("Employee, as registered"), "trays": NUM}, "required": ["name", "trays"]}


@tool("Prepare a planting for the person to confirm: trays, holes per tray, who planted them (registered employees), dates, mortality.",
      {"name": S("e.g. Tomato seedlings"), "seed_item": S("Inventory seed used, code or name; leave out if none"), "planters": {"type": "array", "items": PLANTER},
       "trays": {"type": "number", "description": "Total, when nobody is named"}, "holes_per_tray": N("Leave out for the usual number"),
       "seeds": N("Only when counted instead of trays x holes"), "planted_on": S("YYYY-MM-DD, default today"), "ready_on": S("YYYY-MM-DD"),
       "mortality_pct": NUM, "batch_no": S(), "price": {"type": "number", "description": "Per seedling"}},
      required=["name", "ready_on", "mortality_pct"], perm="plant")
def prepare_planting(ctx, args):
    db, biz = ctx.db, ctx.biz
    name = text_of(args, "name", 100)
    if not name:
        raise ToolError("Ask what the seedlings are called, such as Tomato seedlings.")
    seed = None
    if args.get("seed_item"):
        seed = inventory_item(ctx, args["seed_item"])
        if seed is None or not H.is_seed_category(seed["category"]):
            raise ToolError(f"'{clip(args['seed_item'], 60)}' is not a seed item in inventory. Leave it out if the seed was not taken from inventory.")
    staff = H.planting_staff(db, biz)
    given = {}
    for p in args.get("planters") or []:
        p = p if isinstance(p, dict) else {}
        who = text_of(p, "name", 100)
        try:
            trays = H.tray_count(p.get("trays"))
        except (TypeError, ValueError):
            raise ToolError(f"Give the trays {who or 'each person'} planted, as a number above zero.")
        e = employee_named(ctx, who, staff)
        given[e["id"]] = [e, round((given[e["id"]][1] if e["id"] in given else 0) + trays, 2)]
    if given:
        total = round(sum(t for _, t in given.values()), 2)
    elif args.get("trays") is not None:
        try:
            total = H.tray_count(args.get("trays"))
        except (TypeError, ValueError):
            raise ToolError("The trays planted must be a number above zero.")
    else:
        total = None
    holes = number_of(args.get("holes_per_tray"), int) or (H.hr_settings_for(db, biz)["holes_per_tray"] or None)
    if holes is not None and not 1 <= holes <= H.MAX_TRAY_HOLES:
        raise ToolError(f"The holes in a tray must be a whole number from 1 to {H.MAX_TRAY_HOLES:,}.")
    typed = number_of(args.get("seeds"), int)
    seeds = typed or (int(total * holes + 0.5 + 1e-9) if total and holes else None)
    if not seeds or seeds < 1:
        raise ToolError("Give the trays and the holes in each tray (or the seeds planted). Ask how many holes the trays have if it is not known.")
    planted = H.day_or_none(str(args["planted_on"])) if args.get("planted_on") else ctx.today
    ready = H.day_or_none(str(args.get("ready_on") or ""))
    if planted is None or ready is None or ready < planted:
        raise ToolError("Ask for the planting date and the expected ready date (YYYY-MM-DD); ready cannot be before planted.")
    mortality = number_of(args.get("mortality_pct"))
    if mortality is None or not 0 <= mortality < 100:
        raise ToolError("Ask for the expected mortality: a percentage from 0 up to (not including) 100.")
    if seed:
        have = (seed["quantity"] or 0) * (seed["seeds_per_pack"] or 0) + (seed["loose_seeds"] or 0)
        if seed["seeds_per_pack"] and seeds > have:
            raise ToolError(f"Only {have:,} seeds of {seed['name']} are in inventory, not the {seeds:,} this needs.")
    departments = H.department_names(db, biz)
    form = {"item_id": seed["id"] if seed else "", "name": name, "department": "Greenhouse" if "Greenhouse" in departments else "General",
            "planted_on": planted, "ready_on": ready, "mortality_pct": mortality, "trays_planted": total if total else "", "holes_per_tray": holes or "",
            "seeds_planted": typed or "", "planter_id": [str(e["id"]) for e, _ in given.values()], "planter_trays": [str(t) for _, t in given.values()],
            "batch_no": text_of(args, "batch_no", 40), "still_in_greenhouse": ["0", "1"]}
    if number_of(args.get("price")) is not None:
        form["price"] = round(number_of(args.get("price")), 2)
    rows = [["Seedlings", name], ["Seed", f"{seed['name']} from inventory" if seed else "Not taken from inventory"]]
    if total:
        rows.append(["Trays", f"{total:g} × {holes} holes" if holes else f"{total:g}"])
    rows.append(["Seeds", f"{seeds:,}" + (" (counted)" if typed and total else "")])
    for e, t in given.values():
        rows.append([e["full_name"], f"{t:g} trays" + ("" if e["employment_type"] == "Casual Worker" else " (salaried, not paid by the tray)")])
    rows += [["Planted", planted], ["Ready", ready], ["Mortality", f"{mortality:g}%"], ["Seedlings expected", f"about {H.seedlings_from(seeds, mortality):,}"]]
    title = f"Record a planting of {name}"
    ctx.propose("planting", "plant", title, rows, "/planting", form)
    return shown(title, rows)


@tool("Prepare a casual worker's payslip for the trays they planted (every unpaid tray up to the month's end), for the person to confirm.",
      {"employee": S(), "month": S("YYYY-MM, default this month"), "payment_method": METHOD, "bonus": NUM, "bonus_name": S(), "deduction": NUM, "deduction_name": S()},
      required=["employee", "payment_method"], admin=True)
def prepare_tray_payslip(ctx, args):
    db, biz = ctx.db, ctx.biz
    staff = db.all(f"SELECT {H.EMPLOYEE_COLUMNS} FROM employees WHERE business_id=?", (biz,))
    e = employee_named(ctx, text_of(args, "employee", 100), staff)
    if e["employment_type"] != "Casual Worker":
        raise ToolError(f"{e['full_name']} is permanent staff: they are paid their salary, not by the tray.")
    month = text_of(args, "month", 7) or ctx.today[:7]
    if not (len(month) == 7 and month[4] == "-" and H.day_or_none(month + "-01")):
        raise ToolError("The pay month must be YYYY-MM.")
    rows_, trays = H.unpaid_trays(db, biz, e["id"], month)
    pay, kinds = H.price_trays(db, biz, e, H.hr_settings_for(db, biz), rows_)
    bonus, deduction = number_of(args.get("bonus")) or 0.0, number_of(args.get("deduction")) or 0.0
    if bonus < 0 or deduction < 0:
        raise ToolError("A bonus or deduction cannot be negative.")
    if not rows_ and not bonus:
        raise ToolError(f"{e['full_name']} has no unpaid trays planted up to {month}.")
    if any(not r["rate"] for r in rows_):
        raise ToolError(f"Some of {e['full_name']}'s trays have no pay set. An admin sets the pay for each kind of tray under HR Settings, or the worker's own rate on their page.")
    method = money_method(args, True)
    form = {"employee_id": e["id"], "pay_month": month, "pay_basis": "trays", "payment_method": method,
            "bonus_name": text_of(args, "bonus_name", 40) or ("Bonus" if bonus else ""), "bonus_amount": f"{bonus:.2f}" if bonus else "",
            "deduction_name": text_of(args, "deduction_name", 40) or ("Deduction" if deduction else ""), "deduction_amount": f"{deduction:.2f}" if deduction else ""}
    worked = " + ".join(f"{k['trays']:g}{' ' + k['name'] if k['name'] else ''} × {ctx.money(k['rate'])}" for k in kinds)
    rows = [["Employee", e["full_name"]], ["Pay month", month], ["Trays", f"{trays:g} unpaid, from {len(rows_)} planting(s)"], ["Pay for the trays", f"{worked} = {ctx.money(pay)}"]]
    if bonus:
        rows.append([form["bonus_name"], "+ " + ctx.money(bonus)])
    if deduction:
        rows.append([form["deduction_name"], "− " + ctx.money(deduction)])
    rows += [["NAPSA and NHIMA", "Worked out by the app from HR Settings"], ["Paid by", method]]
    title = f"Pay {e['full_name']} for {trays:g} trays"
    ctx.propose("payslip", "admin", title, rows, "/hr/payslips/new", form)
    return shown(title, rows)


@tool("Prepare moving inventory stock to the POS (to sell) or to farm use, for the person to confirm.",
      {"item": S("Code or name"), "quantity": N(), "to": E("pos", "farm"), "recipient": S("Farm use: who took it")}, required=["item", "quantity", "to"], perm="transfer")
def prepare_transfer(ctx, args):
    item = inventory_item(ctx, args.get("item"))
    if item is None:
        raise ToolError(f"No inventory item fits '{text_of(args, 'item', 60)}'.")
    to = choice(args, "to", ("pos", "farm"), "")
    qty = number_of(args.get("quantity"), int) or 0
    if not to:
        raise ToolError("Say where it goes: pos or farm.")
    if to == "pos" and H.is_seed_category(item["category"]):
        raise ToolError("Seed packs are sown, not sold as they are: prepare a planting instead, or sow them from the item's page.")
    if not 1 <= qty <= item["quantity"]:
        raise ToolError(f"{item['name']}: {item['quantity']} in inventory, so {qty} cannot be moved.")
    form = {"destination": to, "quantity": qty, "recipient": text_of(args, "recipient", 80) if to == "farm" else ""}
    rows = [["Item", f"{item['name']} ({item['item_code']})"], ["Quantity", f"{qty} of {item['quantity']} in inventory"],
            ["To", "Sales & POS, at " + ctx.money(item["price"]) if to == "pos" else "Farm use" + (f", taken by {form['recipient']}" if form["recipient"] else "")]]
    title = f"Move {qty} × {item['name']} to " + ("the POS" if to == "pos" else "farm use")
    ctx.propose("transfer", "transfer", title, rows, f"/inventory/{item['id']}/transfer", form)
    return shown(title, rows)


@tool("Prepare a payment to a supplier (spread over what is owed, oldest first), for the person to confirm.",
      {"supplier": S("Name or phone"), "amount": NUM, "payment_method": METHOD, "payment_reference": S("Needed; for cash the receipt number"), "paid_on": S("YYYY-MM-DD"), "note": S()},
      required=["supplier", "amount", "payment_method"], admin=True)
def prepare_supplier_payment(ctx, args):
    db, biz = ctx.db, ctx.biz
    term = text_of(args, "supplier", 80)
    found = db.all("SELECT id, name, phone FROM suppliers WHERE business_id=? AND (LOWER(name) = ? OR phone = ?)", (biz, term.lower(), term))
    found = found or db.all("SELECT id, name, phone FROM suppliers WHERE business_id=? AND (LOWER(name) LIKE ? OR phone LIKE ?) LIMIT 6", (biz, like(term), like(term)))
    if not found:
        raise ToolError(f"No supplier fits '{term}'.")
    if len(found) > 1:
        raise ToolError("More than one supplier fits: " + "; ".join(f"{s['name']} ({s['phone'] or 'no phone'})" for s in found) + ". Ask which one.")
    s = found[0]
    owed = db.one(
        "SELECT COALESCE(SUM(total_cost - COALESCE(amount_paid, total_cost)), 0) AS n FROM procurements WHERE business_id=? AND supplier_id=? AND total_cost - COALESCE(amount_paid, total_cost) > 0.005",
        (biz, s["id"]),
    )["n"]
    amount = number_of(args.get("amount"))
    if owed < 0.005:
        raise ToolError(f"Nothing is owed to {s['name']}.")
    if amount is None or not 0 < amount <= owed + 0.005:
        raise ToolError(f"The amount must be more than nothing and at most the {ctx.money(owed)} owed to {s['name']}.")
    method = money_method(args, True)
    reference = text_of(args, "payment_reference", 60)
    if not reference:
        raise ToolError("Ask for the payment's reference" + (" (for cash, the receipt number)." if method == "Cash" else "."))
    paid_on = H.day_or_none(str(args["paid_on"])) if args.get("paid_on") else ctx.today
    if paid_on is None or paid_on > ctx.today:
        raise ToolError("The payment date must be today or earlier, as YYYY-MM-DD.")
    form = {"amount": f"{amount:.2f}", "payment_method": method, "payment_reference": reference, "paid_on": paid_on, "note": text_of(args, "note", 150)}
    rows = [["Supplier", s["name"]], ["Amount", f"{ctx.money(amount)} by {method}"], ["Reference", reference], ["Date", paid_on],
            ["Still owed after this", ctx.money(round(owed - amount, 2))]]
    title = f"Pay {ctx.money(amount)} to {s['name']}"
    ctx.propose("supplier_payment", "admin", title, rows, f"/suppliers/{s['id']}/payment", form)
    return shown(title, rows)


@tool("Prepare money in or out that no other page records (rent, fuel, transport, owner's money, a loan...), for the person to confirm.",
      {"direction": E("in", "out"), "category": S("One of the categories the tool lists"), "amount": NUM, "payment_method": METHOD, "date": S("YYYY-MM-DD"),
       "party": S("Paid to or received from"), "description": S(), "reference": S()},
      required=["direction", "category", "amount", "payment_method"], admin=True)
def prepare_money_entry(ctx, args):
    direction = choice(args, "direction", ("in", "out"), "")
    allowed = H.MANUAL_IN if direction == "in" else H.MANUAL_OUT if direction == "out" else ()
    raw = text_of(args, "category", 40).lower().replace(" ", "_")
    by_label = {H.CATEGORY_LABELS[c].lower().replace(" ", "_"): c for c in allowed}
    category = raw if raw in allowed else by_label.get(raw)
    if not direction or category is None:
        raise ToolError("Choose in or out, and a category: " + "; ".join(f"{d}: " + ", ".join(H.CATEGORY_LABELS[c] for c in cats) for d, cats in (("out", H.MANUAL_OUT), ("in", H.MANUAL_IN))) + ".")
    amount = number_of(args.get("amount"))
    if amount is None or amount <= 0:
        raise ToolError("The amount must be above zero.")
    method = money_method(args, True)
    day = H.day_or_none(str(args["date"])) if args.get("date") else ctx.today
    if day is None or day > ctx.today:
        raise ToolError("The date must be today or earlier, as YYYY-MM-DD.")
    form = {"direction": direction, "category": category, "amount": f"{amount:.2f}", "payment_method": method, "occurred_on": day,
            "party": text_of(args, "party", 100), "description": text_of(args, "description", 200), "payment_reference": text_of(args, "reference", 60)}
    rows = [["Money", "In" if direction == "in" else "Out"], ["For", H.CATEGORY_LABELS[category]], ["Amount", f"{ctx.money(amount)} by {method}"], ["Date", day]]
    rows += [[label, form[key]] for key, label in (("party", "To or from"), ("description", "What for"), ("payment_reference", "Reference")) if form[key]]
    title = f"Record {ctx.money(amount)} {'in' if direction == 'in' else 'out'}: {H.CATEGORY_LABELS[category]}"
    ctx.propose("money", "admin", title, rows, "/accounts/record", form)
    return shown(title, rows)


# ================= The day, the planting planner, and messages to customers =================
@tool("What needs attention today (or by tomorrow): deliveries and collections due, pre-orders to collect, batches ready, low stock, and for admins money owed and trays to pay.",
      {"day": E("today", "tomorrow")})
def attention_today(ctx, args):
    db, biz, user = ctx.db, ctx.biz, ctx.user
    today = H.add_days(ctx.today, 1) if args.get("day") == "tomorrow" else ctx.today   # everything due by then, and anything overdue
    soon = H.add_days(today, 2)

    def handovers(column, done):
        rows = db.all(
            f"""SELECT invoice_no, customer_name, {column} AS day FROM invoices WHERE business_id=? AND kind='SALE' AND {column} IS NOT NULL AND {column} <= ?
                AND {done} IS NULL AND COALESCE(payment_status, '') <> 'Cancelled' ORDER BY {column}, id LIMIT 8""",
            (biz, today),
        )
        return [{"invoice": r["invoice_no"], "customer": clip(r["customer_name"], 50), "on": r["day"], "overdue": r["day"] < ctx.today} for r in rows]

    collect = db.all(
        """SELECT po.customer_name, po.qty, po.collect_on, p.name AS plant FROM preorders po JOIN plantings p ON p.id = po.planting_id
           WHERE po.business_id=? AND po.status='pending' AND po.collect_on IS NOT NULL AND po.collect_on <= ? ORDER BY po.collect_on LIMIT 8""",
        (biz, soon),
    )
    growing = H.with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? AND still_in_greenhouse=1", (biz,)))
    ready = [f for f in growing if f["stage"] in ("ready", "due") and f["available"] + f["pending"] > 0]
    coming = [f for f in growing if f["stage"] == "growing" and f["days"] <= H.RELEASE_DAYS]
    threshold = user["low_stock_threshold"]
    low = db.all("SELECT name, quantity FROM pos_products WHERE business_id=? AND quantity < ? ORDER BY quantity, name LIMIT 8", (biz, threshold))
    follow = db.all("SELECT id, customer_name, item_name, quantity, follow_up_on FROM customer_requests WHERE business_id=? AND status='open' AND follow_up_on <= ? "
                    "ORDER BY follow_up_on, id LIMIT 8", (biz, today))
    out = {
        "due_by": today, "deliveries_due": handovers("deliver_on", "delivered_at"), "collections_due": handovers("collect_on", "collected_at"),
        "preorders_to_collect_by": {"until": soon, "list": [{"customer": clip(r["customer_name"], 50), "qty": r["qty"], "plant": clip(r["plant"], 50), "on": r["collect_on"]} for r in collect]},
        "batches_ready": [{"batch": f["label"], "free": f["available"], "preordered": f["pending"]} for f in ready[:8]],
        "batches_ready_within_a_week": [{"batch": f["label"], "in_days": f["days"]} for f in coming[:8]],
        "pos_low_stock": {"under": threshold, "items": [{"name": clip(r["name"], 50), "left": r["quantity"]} for r in low]},
        "customer_requests_to_follow_up": [{"no": r["id"], "customer": clip(r["customer_name"], 50), "item": H.request_label(r), "on": r["follow_up_on"],
                                            "overdue": r["follow_up_on"] < ctx.today} for r in follow],
    }
    if user["role"] == "admin":
        owed = db.one(f"SELECT COUNT(*) AS n, COALESCE(SUM(i.balance_due), 0) AS owed FROM invoices i WHERE i.business_id=? AND {SALE} AND i.balance_due > 0.005", (biz,))
        top = db.all(f"SELECT customer_name, SUM(balance_due) AS owed FROM invoices i WHERE i.business_id=? AND {SALE} AND i.balance_due > 0.005 GROUP BY customer_name ORDER BY owed DESC LIMIT 3", (biz,))
        we_owe = db.one("SELECT COALESCE(SUM(total_cost - COALESCE(amount_paid, total_cost)), 0) AS n FROM procurements WHERE business_id=?", (biz,))["n"]
        hr_set = H.hr_settings_for(db, biz)
        # Unpaid trays by worker, each paid at the rate of its kind of tray (or the worker's own rate when it has no kind)
        by_worker = {}
        for r in db.all(
            """SELECT t.employee_id, t.employee_name, t.trays, t.tray_type_id, t.tray_type, t.type_rate, e.tray_rate
               FROM planting_trays t JOIN employees e ON e.id = t.employee_id AND e.business_id = t.business_id
               WHERE t.business_id=? AND t.paid_payslip_id IS NULL AND e.employment_type='Casual Worker'""",
            (biz,),
        ):
            by_worker.setdefault(r["employee_id"], []).append(r)
        trays = []
        for rows_of in by_worker.values():
            pay, _ = H.price_trays(db, biz, rows_of[0], hr_set, rows_of)
            trays.append({"employee_name": rows_of[0]["employee_name"], "trays": sum(r["trays"] for r in rows_of), "pay": pay})
        trays = sorted(trays, key=lambda r: -r["trays"])[:6]
        paydays = db.all(f"SELECT {H.EMPLOYEE_COLUMNS} FROM employees WHERE business_id=? AND next_pay_date IS NOT NULL AND next_pay_date <= ? ORDER BY next_pay_date LIMIT 8",
                         (biz, today))
        asks = db.one("SELECT COUNT(*) AS n FROM permission_requests WHERE business_id=? AND status='pending'", (biz,))["n"]
        out.update({
            "customers_owe": {"invoices": owed["n"], "amount": r2(owed["owed"]), "most": [{"customer": clip(r["customer_name"], 50), "owes": r2(r["owed"])} for r in top]},
            "we_owe_suppliers": r2(we_owe),
            "trays_to_pay": [{"worker": clip(r["employee_name"], 50), "trays": r2(r["trays"]), "pay": r2(r["pay"]) or None} for r in trays],
            "pay_days": [{"employee": clip(p["full_name"], 50), "on": p["next_pay_date"], "overdue": p["next_pay_date"] < ctx.today,
                          "owed": r2(H.pay_preview(db, biz, p, hr_set, p["next_pay_date"])["amount"])} for p in paydays],
            "requests_waiting_for_approval": asks,
        })
    ctx.source("Up next", H.url_for("up_next"))
    return {k: v for k, v in out.items() if v not in ([], None)}


@tool("Plan a planting: seeds and trays to plant, and when, to have enough seedlings by a day. Uses past batches' mortality and growing time, and seed in stock.",
      {"plant": S("e.g. Tomato"), "seedlings": N("Wanted; leave out to cover pre-orders not yet covered"), "ready_by": S("YYYY-MM-DD"), "mortality_pct": NUM, "holes_per_tray": N()},
      required=["plant"])
def plan_planting(ctx, args):
    db, biz = ctx.db, ctx.biz
    plant = text_of(args, "plant", 60)
    if not plant:
        raise ToolError("Which plant?")
    past = db.all("SELECT * FROM plantings WHERE business_id=? AND (LOWER(name) LIKE ? OR LOWER(seed_name) LIKE ?) ORDER BY planted_on DESC, id DESC LIMIT 20", (biz, like(plant), like(plant)))
    figures = H.with_figures(db, biz, past)
    mortality = number_of(args.get("mortality_pct"))
    basis = {}
    if mortality is None:
        if past:
            mortality = round(sum(p["mortality_pct"] for p in past) / len(past), 1)
            basis["mortality"] = f"average of {len(past)} past batch(es)"
        else:
            mortality, basis["mortality"] = 15.0, "no past batches: 15% assumed, ask the person"
    if not 0 <= mortality < 100:
        raise ToolError("Mortality must be from 0 up to (not including) 100.")
    spans = []
    for p in past:
        a, b = H.day_or_none(p["planted_on"] or ""), H.day_or_none(p["ready_on"] or "")
        if a and b:
            spans.append((datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days)
    grow_days = round(sum(spans) / len(spans)) if spans else None
    wanted = number_of(args.get("seedlings"), int)
    if wanted is None:
        pending = sum(f["pending"] for f in figures if f["still_in_greenhouse"])
        free = sum(f["available"] for f in figures if f["still_in_greenhouse"])
        wanted = max(0, pending - free)
        basis["seedlings"] = f"pre-orders waiting ({pending}) less seedlings still free in batches growing ({free})"
    if wanted <= 0:
        return {"plant": plant, "seedlings_needed": 0, "basis": basis, "say": "The pre-orders are already covered by the batches growing; ask how many seedlings they want if they want more."}
    holes = number_of(args.get("holes_per_tray"), int) or (H.hr_settings_for(db, biz)["holes_per_tray"] or None)
    if holes is None:
        used = [p["holes_per_tray"] for p in past if p["holes_per_tray"]]
        holes = max(set(used), key=used.count) if used else None
    seeds = math.ceil(wanted / (1 - mortality / 100) - 1e-9)
    out = {"plant": plant, "seedlings_needed": wanted, "mortality_pct": mortality, "seeds_to_plant": seeds, "basis": basis}
    if holes:
        out.update(holes_per_tray=holes, trays_to_plant=math.ceil(seeds / holes - 1e-9))
    if grow_days is not None:
        out["growing_days_usually"] = grow_days
        ready_by = H.day_or_none(str(args.get("ready_by") or ""))
        if ready_by:
            plant_by = H.add_days(ready_by, -grow_days)
            out.update(ready_by=ready_by, plant_by=plant_by, late=plant_by < ctx.today)
    seed_items = [r for r in db.all("SELECT name, quantity, seeds_per_pack, loose_seeds, category FROM products WHERE business_id=? AND LOWER(name) LIKE ?", (biz, like(plant))) if H.is_seed_category(r["category"])]
    if seed_items:
        have = sum((r["quantity"] or 0) * (r["seeds_per_pack"] or 0) + (r["loose_seeds"] or 0) for r in seed_items)
        out["seed_in_stock"] = {"items": [clip(r["name"], 50) for r in seed_items][:4], "seeds": have, "enough": have >= seeds}
    ctx.source("Planting Records", H.url_for("planting_list"))
    return out


@tool("Write a message for the person to send a customer themselves (a payment or collection reminder...). It is shown with Copy and Open in WhatsApp; nothing is sent.",
      {"customer": S("Name or phone"), "message": S("The whole message, in the person's language")}, required=["customer", "message"], perm="sell")
def draft_message(ctx, args):
    term, text = text_of(args, "customer", 80), " ".join(str(args.get("message") or "").split())[:900]
    if not text:
        raise ToolError("Write the message first.")
    db, biz = ctx.db, ctx.biz
    found = db.all("SELECT name, phone FROM customers WHERE business_id=? AND (LOWER(name) = ? OR phone_key = ?)", (biz, term.lower(), H.phone_key(term)))
    found = found or db.all("SELECT name, phone FROM customers WHERE business_id=? AND LOWER(name) LIKE ? LIMIT 6", (biz, like(term)))
    if len(found) > 1 and len({f["phone"] for f in found}) > 1:
        raise ToolError("More than one customer fits '" + term + "': " + "; ".join(f"{f['name']} ({f['phone'] or 'no phone'})" for f in found) + ". Ask which one.")
    name, phone = (found[0]["name"], found[0]["phone"] or "") if found else (term, term if len(H.phone_key(term)) >= 9 else "")
    ctx.draft(f"Message to {clip(name, 60)}", text, phone)
    return {"drafted": True, "to": name, "phone": phone or None,
            "say": "Say the message is ready below to copy or open in WhatsApp" + ("" if phone else " (no phone number is recorded, so it can only be copied)") + ". Nothing was sent."}


# ================= Restock advice and what to watch out for =================
# Worked out here, not by the model, so the numbers are the same every time and match the records. The model explains them.
WATCH_DAYS = 90          # the sales and use looked at to see how fast something goes
MIN_HISTORY = 14         # days of records needed before that speed is trusted
DEFAULT_COVER = 30       # days of stock to keep, unless the business or the item says otherwise
SPARE_DAYS = 7           # bought on top of those days: the time it takes to buy again
SHELF_DAYS = 7           # a POS shelf with less than this left is filled from the store (to last twice as long)
SEEDLING_MARK = "-SDL"   # in the POS code of seedlings (a planting's, or seed sown straight from the inventory)


def days_between(first, last):
    return (datetime.date.fromisoformat(last[:10]) - datetime.date.fromisoformat(first[:10])).days


def per_day(used90, used30, history):
    """Units a day. With a month of records or more, the average of the last 30 days and the whole period, so a change shows soon
    but one busy week does not decide it."""
    if history >= 30:
        return (used30 / 30 + used90 / min(history, WATCH_DAYS)) / 2
    return used90 / max(history, 1)


def stock_preferences_of(ctx):
    return {r["item_key"]: r for r in ctx.db.all("SELECT * FROM stock_preferences WHERE business_id=?", (ctx.biz,))}


def bought_line(last):
    return {"from": clip(last["supplier_name"], 50), "on": last["purchase_date"], "unit_cost": r2(last["unit_cost"])} if last else None


def stock_report(ctx, focus="all", only="", limit=6):
    """The restock advice and the warnings, as stock_watch gives them (the evening brief uses it too)."""
    db, biz, today = ctx.db, ctx.biz, ctx.today
    since90, since60, since30 = (H.add_days(today, -n) for n in (WATCH_DAYS, 60, 30))
    prefs = stock_preferences_of(ctx)
    keep_days = ctx.user["stock_cover_days"] or DEFAULT_COVER
    wanted = (only or "").lower()

    def chosen(name, code=""):
        return not wanted or wanted in (name or "").lower() or wanted.upper() == (code or "")

    # What went out: sales and farm use, by item code (seedlings' codes too: their plants are worked out below)
    used = {r["code"]: r for r in db.all(
        """SELECT ii.item_code AS code, MIN(i.created_at) AS first, MAX(i.created_at) AS last,
                  SUM(CASE WHEN i.created_at >= ? THEN ii.qty ELSE 0 END) AS used90, SUM(CASE WHEN i.created_at >= ? THEN ii.qty ELSE 0 END) AS used30,
                  SUM(CASE WHEN i.created_at >= ? AND i.kind = 'SALE' THEN ii.qty ELSE 0 END) AS sold30
           FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
           WHERE ii.business_id=? AND i.kind IN ('SALE', 'FARM_USE') AND COALESCE(i.payment_status, '') <> 'Cancelled' GROUP BY ii.item_code""",
        (since90, since30, since30, biz))}
    # Seed is used by planting it: counted in seeds, from plantings that took it from the inventory and from seed sown straight to the POS
    sown = {}
    for r in db.all("SELECT seed_item_id AS id, planted_on AS day, seeds_planted AS seeds FROM plantings WHERE business_id=? AND seed_item_id IS NOT NULL AND source='inventory'", (biz,)) \
            + db.all("SELECT seed_item_id AS id, created_at AS day, seeds_sown AS seeds FROM seedling_batches WHERE business_id=? AND seed_item_id IS NOT NULL", (biz,)):
        if not r["day"]:
            continue
        s = sown.setdefault(r["id"], {"first": r["day"], "last": r["day"], "used90": 0, "used30": 0})
        s["first"], s["last"] = min(s["first"], r["day"]), max(s["last"], r["day"])
        s["used90"] += r["seeds"] if r["day"] >= since90 else 0
        s["used30"] += r["seeds"] if r["day"] >= since30 else 0
    bought = {}
    for r in db.all("SELECT item_code, supplier_name, purchase_date, unit_cost FROM procurements WHERE business_id=? AND item_code IS NOT NULL ORDER BY purchase_date, id", (biz,)):
        bought.setdefault(r["item_code"], []).append(r)
    store = {r["item_code"]: r for r in db.all(
        "SELECT id, item_code, name, category, quantity, unit_cost, price, purchase_date, expiry_date, seeds_per_pack, loose_seeds, unit FROM products WHERE business_id=?", (biz,))}
    shelf = {r["item_code"]: r for r in db.all("SELECT item_code, name, category, quantity, price FROM pos_products WHERE business_id=?", (biz,))
             if SEEDLING_MARK not in (r["item_code"] or "") and (r["category"] or "") != "Seedlings"}

    buy, move, plant, watch, unsure, skipped = [], [], [], [], [], 0
    for code in sorted(set(store) | set(shelf)):
        p, s = store.get(code), shelf.get(code)
        name = (p or s)["name"]
        if not chosen(name, code):
            continue
        pref = prefs.get("code:" + code)
        seed = bool(p) and H.is_seed_category(p["category"]) and bool(p["seeds_per_pack"])
        u, sw = used.get(code), (sown.get(p["id"]) if p else None)
        in_store, on_shelf = (p["quantity"] if p else 0) or 0, (s["quantity"] if s else 0) or 0
        buys = bought.get(code, [])
        last = buys[-1] if buys else None
        cost = (last["unit_cost"] if last else 0) or (p["unit_cost"] if p else 0) or 0
        if seed:   # worked out in seeds, shown in packs
            size = p["seeds_per_pack"]
            total = in_store * size + (p["loose_seeds"] or 0)
            used90 = (sw or {}).get("used90", 0) + ((u["used90"] or 0) if u else 0) * size
            used30 = (sw or {}).get("used30", 0) + ((u["used30"] or 0) if u else 0) * size
        else:
            size, total = 1, in_store + on_shelf
            used90, used30 = ((u["used90"] or 0) if u else 0), ((u["used30"] or 0) if u else 0)
        firsts = [x for x in ((u or {}).get("first"), (sw or {}).get("first"), buys[0]["purchase_date"] if buys else None, p["purchase_date"] if p else None) if x]
        history = min(WATCH_DAYS, days_between(min(firsts), today)) if firsts else 0
        lasts = [x for x in ((u or {}).get("last"), (sw or {}).get("last")) if x]
        rate = per_day(used90, used30, history) if used90 else 0
        unit = "packs" if seed else ((p["unit"] if p else "") or "units")
        never = bool(pref and pref["never_suggest"])
        skipped += never

        # ----- running low: what to buy -----
        if rate > 0 and not never and focus != "watch":
            if history < MIN_HISTORY:
                if total / rate < keep_days:
                    unsure.append({"item": clip(name, 60), "why": f"only {history} days of records, too soon to say how fast it goes; ask how much they want to keep"})
            else:
                target = pref["cover_days"] if pref and pref["cover_days"] else keep_days
                days_left = total / rate
                if days_left < target:
                    need = math.ceil((rate * (target + SPARE_DAYS) - total) / size - 1e-9)
                    if need > 0:
                        buy.append({
                            "item": clip(name, 60), "code": code, "have": round(total / size, 1) if seed else total, "unit": unit,
                            "in_store": in_store if s else None, "on_pos": on_shelf if s else None,
                            "use_per_day": round(rate / size, 2), "days_left": int(days_left), "runs_out_about": H.add_days(today, int(days_left)),
                            "buy": need, "for_days": target, "about": r2(need * cost) if cost else None, "last_bought": bought_line(last),
                            "buy_from": (pref["supplier"] or None) if pref else None,
                            "urgency": "now" if days_left < 7 else ("soon" if days_left < 14 else "this month"),
                        })
            # ----- a POS shelf running low while the store has some: move it, do not buy it -----
            if s and p and in_store > 0 and not seed and u and history >= MIN_HISTORY:
                pos_rate = (u["sold30"] or 0) / min(30, history)
                if pos_rate > 0 and on_shelf / pos_rate < SHELF_DAYS:
                    qty = min(in_store, math.ceil(pos_rate * SHELF_DAYS * 2) - on_shelf)
                    if qty > 0:
                        move.append({"item": clip(name, 60), "code": code, "on_pos": on_shelf, "in_store": in_store, "pos_days_left": int(on_shelf / pos_rate), "move": qty})
        if focus == "restock":
            continue

        # ----- watch out -----
        expires = p["expiry_date"] if p else None
        if expires and H.day_or_none(expires) and total > 0:
            left_days = days_between(today, expires)
            if left_days < 0:
                watch.append({"type": "expired", "item": clip(name, 60), "expired_on": expires, "in_stock": round(total / size, 1) if seed else total})
            elif left_days <= 30:
                likely = math.ceil((total - rate * left_days) / size - 1e-9)
                if likely > 0:
                    watch.append({"type": "expiring", "item": clip(name, 60), "expires": expires, "in_stock": round(total / size, 1) if seed else total,
                                  "likely_left_then": likely, "unit": unit})
        if total > 0 and not never and firsts and min(firsts) < since60 and (not lasts or max(lasts) < since60):
            watch.append({"type": "not_selling", "item": clip(name, 60), "in_stock": round(total / size, 1) if seed else total, "unit": unit,
                          "last_sold_or_used": max(lasts)[:10] if lasts else None, "tied_up": r2(total / size * cost) if cost else None})
        if len(buys) >= 2 and buys[-1]["purchase_date"] and buys[-1]["purchase_date"] >= since90:
            before = [b["unit_cost"] for b in buys[-4:-1] if b["unit_cost"]]
            was = sum(before) / len(before) if before else 0
            if was > 0 and buys[-1]["unit_cost"] >= was * 1.10:
                watch.append({"type": "price_up", "item": clip(name, 60), "now": r2(buys[-1]["unit_cost"]), "before": r2(was),
                              "up_pct": round((buys[-1]["unit_cost"] - was) / was * 100), "supplier": clip(buys[-1]["supplier_name"], 50), "on": buys[-1]["purchase_date"]})
        price = (s["price"] if s else 0) or (p["price"] if p else 0) or 0
        if not seed and cost > 0 and price > 0 and (total > 0 or rate > 0):
            if price <= cost:
                watch.append({"type": "below_cost", "item": clip(name, 60), "price": r2(price), "cost": r2(cost)})
            elif (price - cost) / price < 0.10:
                watch.append({"type": "thin_margin", "item": clip(name, 60), "price": r2(price), "cost": r2(cost), "margin_pct": round((price - cost) / price * 100, 1)})

    # ----- seedlings: planted, not bought, so the time they take to grow counts -----
    batches = H.with_figures(db, biz, db.all("SELECT * FROM plantings WHERE business_id=? ORDER BY planted_on, id", (biz,)))
    plants = {}
    for b in batches:
        plants.setdefault(" ".join((b["name"] or "").lower().split()), []).append(b)
    holes_usual = H.hr_settings_for(db, biz)["holes_per_tray"] or None
    for key, group in sorted(plants.items()):
        name = group[-1]["name"]
        if not key or not chosen(name):
            continue
        live = [b for b in group if b["still_in_greenhouse"]]
        if focus != "restock":
            for b in live:
                over = b["pending"] + b["sold"] - b["seedlings"]
                if over > 0:
                    watch.append({"type": "overbooked", "item": b["label"], "seedlings": b["seedlings"], "sold": b["sold"], "preordered": b["pending"], "short_by": over})
        if focus == "watch":
            continue
        pref = prefs.get("plant:" + key)
        if pref and pref["never_suggest"]:
            skipped += 1
            continue
        sales = [used[b["pos_code"]] for b in group if b["pos_code"] in used]
        used90 = sum(r["used90"] or 0 for r in sales)
        if not used90:
            continue
        used30 = sum(r["used30"] or 0 for r in sales)
        # Counted from the first sale: a batch cannot sell before it is ready, so the growing weeks are not days of no demand
        history = min(WATCH_DAYS, days_between(min(r["first"] for r in sales), today))
        if history < MIN_HISTORY:
            unsure.append({"item": clip(name, 60) + " seedlings", "why": f"only {history} days of records; ask how many they sell"})
            continue
        rate = per_day(used90, used30, history)
        free = sum(b["available"] for b in live)
        recent = group[-10:]
        spans = [days_between(b["planted_on"], b["ready_on"]) for b in recent if H.day_or_none(b["planted_on"] or "") and H.day_or_none(b["ready_on"] or "")]
        grow = round(sum(spans) / len(spans)) if spans else None
        target = pref["cover_days"] if pref and pref["cover_days"] else keep_days
        need = rate * ((grow or 0) + target) - free
        if need < 1:
            continue
        mortality = sum(b["mortality_pct"] or 0 for b in recent) / len(recent)
        seedlings = math.ceil(need - 1e-9)
        seeds = math.ceil(seedlings / (1 - min(mortality, 95) / 100) - 1e-9)
        lasts_days = free / rate
        holes = holes_usual or next((b["holes_per_tray"] for b in reversed(group) if b["holes_per_tray"]), None)
        entry = {
            "plant": clip(name, 60), "sell_per_day": round(rate, 1), "free_seedlings_now": free, "free_last_days": int(lasts_days),
            "grow_days": grow, "plant_by": H.add_days(today, max(0, int(lasts_days - (grow or 0)))), "late": bool(grow) and lasts_days < grow,
            "plant_seedlings": seedlings, "for_days": target, "mortality_pct": round(mortality, 1), "seeds": seeds,
            "trays": math.ceil(seeds / holes - 1e-9) if holes else None, "holes_per_tray": holes,
        }
        if grow is None:
            entry["note"] = "no growing time recorded yet: ask how long it takes"
        seed_items = {b["seed_item_id"] for b in group if b["seed_item_id"]}
        stock = [r for r in store.values() if r["id"] in seed_items and r["seeds_per_pack"]]
        if stock:
            have = sum((r["quantity"] or 0) * r["seeds_per_pack"] + (r["loose_seeds"] or 0) for r in stock)
            entry["seed_in_stock"] = have
            if have < seeds:
                r = stock[0]
                packs = math.ceil((seeds - have) / r["seeds_per_pack"] - 1e-9)
                entry["seed_short_packs"] = packs
                same = next((x for x in buy if x["code"] == r["item_code"]), None)
                if same:
                    same["buy"] = max(same["buy"], packs)
                    same["for_planting"] = clip(name, 60)
                elif not (prefs.get("code:" + r["item_code"]) or {}).get("never_suggest"):
                    last = (bought.get(r["item_code"]) or [None])[-1]
                    cost = (last["unit_cost"] if last else 0) or r["unit_cost"] or 0
                    buy.append({"item": clip(r["name"], 60), "code": r["item_code"], "have": round(have / r["seeds_per_pack"], 1), "unit": "packs",
                                "buy": packs, "for_planting": clip(name, 60), "about": r2(packs * cost) if cost else None, "last_bought": bought_line(last),
                                "urgency": "now" if entry["late"] or entry["plant_by"] <= H.add_days(today, 7) else "soon", "days_left": 0})
        plant.append(entry)

    # ----- losses well above usual (disease, damage, mortality...) -----
    if focus != "restock":
        for r in db.all(
            """SELECT item_name, SUM(CASE WHEN created_at >= ? THEN -delta ELSE 0 END) AS recent, SUM(CASE WHEN created_at < ? THEN -delta ELSE 0 END) AS earlier,
                      MAX(reason) AS reason FROM stock_adjustments WHERE business_id=? AND delta < 0 AND created_at >= ? GROUP BY item_name""",
            (since30, since30, biz, since90),
        ):
            recent, usual = r["recent"] or 0, (r["earlier"] or 0) / 2
            if chosen(r["item_name"]) and recent >= 5 and recent > 2 * usual:
                watch.append({"type": "losses", "item": clip(r["item_name"], 60), "lost_last_30_days": recent, "usual_a_month": round(usual, 1), "reason": clip(r["reason"], 60)})

    order = {"now": 0, "soon": 1, "this month": 2}
    buy.sort(key=lambda x: (order[x["urgency"]], x.get("days_left", 0)))
    plant.sort(key=lambda x: x["plant_by"])
    move.sort(key=lambda x: x["pos_days_left"])
    kinds = ("expired", "overbooked", "below_cost", "expiring", "losses", "price_up", "thin_margin", "not_selling")
    watch.sort(key=lambda x: (kinds.index(x["type"]), -(x.get("tied_up") or 0)))
    # What customers asked for that we do not have (Customer requests): a reason to buy or plant it, shown with the advice
    asking = H.requests_with_stock(db, biz, db.all("SELECT * FROM customer_requests WHERE business_id=? AND status='open'", (biz,)))
    for b in buy + plant:
        named = b.get("item") or b.get("plant") or ""
        who = {H.customer_group_key(r["customer_name"], r["customer_phone"]) for r in asking if H.request_fits(r["item_name"], named)}
        if who:
            b["customers_asked"] = len(who)
    asked = [a for a in H.most_requested(asking) if chosen(a["item"])]
    ctx.source("Inventory", H.url_for("inventory"))
    out = {"basis": f"speed from sales and farm use of the last {WATCH_DAYS} days; enough is bought for {keep_days} days (unless an item says otherwise) plus {SPARE_DAYS} spare"}
    if focus != "watch":
        out.update(buy={"count": len(buy), "items": buy[:limit]}, move_to_pos={"count": len(move), "items": move[:limit]}, plant={"count": len(plant), "items": plant[:limit]})
        if asked:
            out["customers_asked_for"] = {"count": len(asked), "items": [{"item": clip(a["item"], 60), "customers": a["customers"], "quantity": a["quantity"] or None,
                                                                         "have_now": a["have_now"]} for a in asked[:limit]]}
    if focus != "restock":
        out["watch"] = {"count": len(watch), "items": watch[:limit + 2]}
    if unsure:
        out["ask"] = unsure[:3]
    if skipped:
        out["left_out_by_preference"] = skipped
    out["say"] = ("The app worked these numbers out: do not recalculate them. Most urgent first. Offer to prepare the purchase, transfer or planting. "
                  "customers_asked_for is what customers requested that we do not have: suggest getting what several asked for. "
                  "If 'ask' has something, end with one short question about it and offer to remember the answer.")
    return out


@tool("What to restock (how much, from whom, cost), POS shelves to fill from the store, seedlings to plant in time, and warnings: expiring, not selling, prices up, low margins, losses, "
      "over-promised batches.",
      {"focus": E("all", "restock", "watch"), "item": S("Only this item or plant"), "limit": LIMIT})
def stock_watch(ctx, args):
    return stock_report(ctx, choice(args, "focus", ("all", "restock", "watch"), "all"), text_of(args, "item", 60), whole(args, "limit", 6, 1, 15))


# ================= What the person asks to be remembered =================
# The only things the assistant writes itself: its own memory, which the person sees in the chat as it is saved and can delete in the panel or Settings.
MAX_NOTES_PERSON = 20
MAX_NOTES_BUSINESS = 40
NOTE_CHARS = 200


@tool("Remember a lasting preference the person told you (never one from a record). for_everyone (admins): for the whole business. Restock settings: stock_preference. "
      "A customer wanting an item we do not have is a customer request (prepare_customer_request), not this.",
      {"note": S(), "for_everyone": B()}, required=["note"])
def remember_this(ctx, args):
    note = " ".join(str(args.get("note") or "").split())[:NOTE_CHARS]
    if len(note) < 3:
        raise ToolError("What should be remembered?")
    everyone = bool(args.get("for_everyone"))
    if everyone and ctx.user["role"] != "admin":
        raise ToolError("Only an admin can set something for the whole business. It can be remembered for this person only.")
    db, biz = ctx.db, ctx.biz
    if everyone:
        mine = db.all("SELECT note FROM assistant_memory WHERE business_id=? AND user_id IS NULL", (biz,))
    else:
        mine = db.all("SELECT note FROM assistant_memory WHERE business_id=? AND user_id=?", (biz, ctx.user["id"]))
    if any(m["note"].lower() == note.lower() for m in mine):
        return {"remembered": True, "already": True}
    if len(mine) >= (MAX_NOTES_BUSINESS if everyone else MAX_NOTES_PERSON):
        raise ToolError("The memory is full. Ask the person which saved preference to forget first (they are listed in your instructions).")
    db.insert("INSERT INTO assistant_memory (business_id, user_id, note, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
              (biz, None if everyone else ctx.user["id"], note, ctx.user["username"], H.now().strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()
    ctx.notes.append(("Remembered for everyone: " if everyone else "Remembered: ") + note)
    return {"remembered": True, "for": "the whole business" if everyone else "this person"}


@tool("Forget a saved preference by its number.", {"number": N()}, required=["number"])
def forget_this(ctx, args):
    number = number_of(args.get("number"), int)
    row = ctx.db.one("SELECT * FROM assistant_memory WHERE id=? AND business_id=?", (number, ctx.biz)) if number else None
    if row is None or (row["user_id"] is not None and row["user_id"] != ctx.user["id"]):
        raise ToolError("No saved preference has that number.")
    if row["user_id"] is None and ctx.user["role"] != "admin":
        raise ToolError("That one is for the whole business: only an admin can remove it.")
    ctx.db.execute("DELETE FROM assistant_memory WHERE id=?", (row["id"],))
    ctx.db.commit()
    ctx.notes.append("Forgotten: " + row["note"])
    return {"forgotten": row["note"]}


def preference_item(ctx, term):
    """(item_key, name) for what a restock preference is about: an inventory or POS item, or a plant's seedlings."""
    db, biz, key = ctx.db, ctx.biz, " ".join(term.lower().split())

    def fits(exact):
        """Everything the term names, exactly or (exact=False) as part of a name: inventory, POS (not seedlings), plants."""
        if exact:
            where, params = "(UPPER(item_code) = ? OR LOWER(name) = ?)", (term.upper(), key)
        else:
            where, params = "(UPPER(item_code) LIKE ? OR LOWER(name) LIKE ?)", (like(term).upper(), like(term))
        out = {}
        for table in ("products", "pos_products"):
            for r in db.all(f"SELECT item_code, name FROM {table} WHERE business_id=? AND item_code NOT LIKE ? AND {where} LIMIT 8", (biz, "%" + SEEDLING_MARK + "%") + params):
                out.setdefault("code:" + r["item_code"], r["name"])
        for r in db.all("SELECT DISTINCT name FROM plantings WHERE business_id=? AND " + ("LOWER(name) = ?" if exact else "LOWER(name) LIKE ?") + " LIMIT 8",
                        (biz, key if exact else like(term))):
            out.setdefault("plant:" + " ".join(r["name"].lower().split()), r["name"] + " seedlings")
        return out

    found = fits(True) or fits(False)
    if len(found) == 1:
        return next(iter(found.items()))
    if found:
        raise ToolError("More than one fits '" + term + "': " + "; ".join(found.values()) + ". Ask which one.")
    raise ToolError(f"Nothing in the inventory, on the POS or in the plantings is called '{term}'.")


def preference_words(p):
    parts = []
    if p.get("never_suggest"):
        parts.append("never suggest it")
    if p.get("cover_days"):
        parts.append(f"keep about {p['cover_days']} days")
    if p.get("supplier"):
        parts.append("buy from " + p["supplier"])
    return ", ".join(parts) or "no setting"


@tool("Save a restock setting the person gave for an item or a plant's seedlings: days of stock to keep, never suggest, or supplier. No item: days for everything. clear removes it.",
      {"item": S(), "keep_days": N(), "never_suggest": B(), "supplier": S(), "clear": B()}, admin=True)
def stock_preference(ctx, args):
    db, biz, who, stamp = ctx.db, ctx.biz, ctx.user["username"], H.now().strftime("%Y-%m-%d %H:%M:%S")
    days = number_of(args.get("keep_days"), int)
    if days is not None and not 1 <= days <= 365:
        raise ToolError("Days of stock must be from 1 to 365.")
    term = text_of(args, "item", 80)
    if not term:
        if days is None:
            raise ToolError("Which item, or how many days of stock for everything?")
        db.execute("UPDATE businesses SET stock_cover_days=? WHERE id=?", (days, biz))
        db.commit()
        ctx.notes.append(f"Remembered for everyone: keep about {days} days of stock")
        return {"saved": True, "keep_days_for_everything": days}
    key, name = preference_item(ctx, term)
    row = db.one("SELECT * FROM stock_preferences WHERE business_id=? AND item_key=?", (biz, key))
    if args.get("clear"):
        if row:
            db.execute("DELETE FROM stock_preferences WHERE id=?", (row["id"],))
            db.commit()
            ctx.notes.append(f"Forgotten: the restock setting for {name}")
        return {"cleared": name}
    supplier, never = text_of(args, "supplier", 60), args.get("never_suggest")
    if days is None and never is None and not supplier:
        raise ToolError("What should be saved: days of stock to keep, never suggest it, or a supplier?")
    new = {
        "cover_days": days if days is not None else (row["cover_days"] if row else None),
        "never_suggest": (1 if never else 0) if never is not None else (row["never_suggest"] if row else 0),
        "supplier": supplier or (row["supplier"] if row else ""),
    }
    if row:
        db.execute("UPDATE stock_preferences SET item_name=?, cover_days=?, never_suggest=?, supplier=?, set_by=?, set_at=? WHERE id=?",
                   (name, new["cover_days"], new["never_suggest"], new["supplier"], who, stamp, row["id"]))
    else:
        db.insert("""INSERT INTO stock_preferences (business_id, item_key, item_name, cover_days, never_suggest, supplier, set_by, set_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (biz, key, name, new["cover_days"], new["never_suggest"], new["supplier"], who, stamp))
    db.commit()
    ctx.notes.append(f"Remembered for everyone: {name}: " + preference_words(new))
    return {"saved": True, "item": name, **new}



# ================= Which tools a question is offered =================
# Every call to Gemini carries the description of every tool it is offered, and those descriptions are most of what is sent. So a question
# is offered only the areas its words call for (assistant.areas_for), plus the few tools every question gets; when that is not enough, the
# model asks for more with more_tools. Each area's prepare tools (the largest descriptions) are a second group, "<area>+", offered only
# when the person asks for something to be done rather than asks a question. A tool may sit in more than one group.
GROUPS = {
    "sales": ("sales_report", "find_invoices", "invoice_details", "product_buyers", "customers", "deliveries_and_collections"),
    "sales+": ("prepare_sale", "prepare_payment", "prepare_handover_change", "draft_message"),
    "stock": ("stock_watch", "purchases", "suppliers"),
    "stock+": ("prepare_purchase", "prepare_transfer", "prepare_supplier_payment"),
    "planting": ("plantings", "preorders", "plan_planting", "daily_activities"),
    "planting+": ("prepare_preorder", "prepare_planting", "prepare_tray_payslip"),
    "money": ("financial_overview", "cashbook_entries", "customers", "suppliers"),
    "money+": ("prepare_money_entry", "prepare_supplier_payment"),
    "staff": ("staff", "daily_activities"),
    "staff+": ("prepare_tray_payslip",),
    "records": ("audit_log", "my_alerts", "reminders"),
    "memory": ("remember_this", "forget_this", "stock_preference"),
    "today": ("attention_today", "deliveries_and_collections", "preorders", "customer_requests", "reminders"),
    "requests": ("customer_requests", "stock_watch"),
    "requests+": ("prepare_customer_request", "prepare_request_update"),
}
AREAS = tuple(a for a in GROUPS if not a.endswith("+"))
# A question (rather than something to do) gets only the areas' look-ups
ASKING = re.compile(r"^\s*(how|what|which|who|whom|when|where|why|did|do|does|is|are|was|were|has|have|show|list|tell me|give me|can you tell|any)\b")
DOING = re.compile(r"\b(prepare|record|make|create|add|enter|log|do it|go ahead|yes|yeah|ok|okay|confirm)\b")
AREA_WORDS = {area: re.compile(r"\b(" + words + r")\b") for area, words in {
    "sales": r"sales?|sell\w*|sold|invoices?|inv-\d+|revenue|takings|customers?|clients?|buyers?|bought|paid|pay|paying|payments?|owe[sd]?|owing|debts?|"
             r"deliver\w*|collect\w*|remind\w*|message|whatsapp|discounts?|refund\w*",
    "stock": r"stock\w*|inventor\w*|restock\w*|buy|buying|purchas\w*|receipts?|suppliers?|move|moving|transfer\w*|low|items?|products?|expir\w*|prices?|"
             r"costs?|margins?|re-?order\w*|order more|run(ning)? out|watch out|warnings?|shelf|pos|store",
    "planting": r"plant\w*|seeds?|seedlings?|trays?|batch\w*|greenhouse|pre-?orders?|mortality|germin\w*|grow\w*|plots?|activit\w*|nursery",
    "money": r"money|cash|bank|profits?|loss|losses|expenses?|spend|spent|rent|fuel|transport|accounts?|balances?|income|worth|airtel|mtn|loans?|financ\w*|"
             r"owe[sd]?|owing|debts?",
    "staff": r"staff|employees?|payroll|payslips?|salar\w*|napsa|nhima|wages?|workers?|casuals?",
    "records": r"delet\w*|adjust\w*|removed|requests?|permissions?|alerts?|notifications?|audit|who changed",
    "memory": r"remember\w*|forget|forgot|prefer\w*|always|never|from now on|keep (about |around )?\d+",
    "today": r"today|tomorrow|tonight|attention|due|urgent|to do|brief|this morning|this week|remind\w*|owing|owe[sd]?|ready|orders?|pre-?orders?",
    "requests": r"requests?|requested|wants?|wanted|needs?|asked for|asking for|looking for|enquir\w*|inquir\w*|follow(ing)?[- ]?ups?|out of stock|"
                r"do not have|don't have|dont have|fulfill?(ed)?|dropped",
}.items()}


@tool("Get more tools (look-ups and prepare tools) when none offered fits. Areas: sales (invoices, customers, deliveries, payments, messages), stock (restock, "
      "purchases, suppliers, transfers), planting (batches, pre-orders, trays, plots), money (accounts, expenses), staff (payroll), records (alerts, "
      "adjustments, deleted), memory (preferences), today, requests (items customers asked for that we do not have).", {"areas": {"type": "array", "items": E(*AREAS)}}, required=["areas"])
def more_tools(ctx, args):
    wanted = {a for a in (args.get("areas") or []) if a in AREAS} if isinstance(args.get("areas"), list) else set()
    if not wanted:
        raise ToolError("Name one or more areas: " + ", ".join(AREAS) + ".")
    ctx.areas |= wanted | {a + "+" for a in wanted if a + "+" in GROUPS}
    return {"added": sorted(wanted), "say": "Those tools are offered now: use them."}


for _area, _names in GROUPS.items():
    for _name in _names:
        assistant.TOOLS[_name].groups.add(_area)


# ================= Simple questions the app answers itself, without Gemini =================
# Matched on their whole wording, so only a question that plainly is one of these is answered here; anything else goes to Gemini.
def plain_words(message):
    t = " ".join(re.sub(r"[?!.,;:“”\"]+", " ", message.lower().replace("’", "'")).split())
    return re.sub(r"\s+(please|for me|now|right now)$", "", re.sub(r"^((please|hey|hi|ok|okay|so)\s+)+", "", t))


QUICK_TODAY = re.compile(r"^(what|whats|what's|what is|anything that) (needs|need|requires|wants) (my |our )?attention( today)?$|^what (should|do|must) (i|we) do today$"
                         r"|^what('s| is) (on |due )?(for )?today$|^(today|my day|today's brief)$")
QUICK_RESTOCK = re.compile(r"^(what|which (items|things|products|stock)) (should|do|must) (i|we) (re-?stock|buy|order)( now| this month| this week| today)?$"
                           r"|^(what|anything) to (re-?stock|buy|order)$|^(re-?stock|restocking)( advice| suggestions)?$|^what needs re-?stocking$")
QUICK_LOW = re.compile(r"^(what|which (items|products|things))( is|'s| are)? (low|running low|running out|low on stock|low in stock|almost finished|finishing)$"
                       r"|^(show |list )?(me )?(the )?low stock( items)?$|^what('s| is) low( on stock| in stock)?$|^anything low( on stock)?$")
QUICK_COUNT = re.compile(r"^how many (different )?(items|products) (do we have|are there|are in stock|in stock|do we stock)$")
QUICK_LOOKUP = [re.compile(p) for p in (
    r"^how (many|much) (?P<item>.+?) (do we have( left)?|have we got( left)?|are (there|left|in stock)|is (there|left|in stock)|in stock|left)$",
    r"^(do we have|have we got|is there|are there) (any )?(?P<item>.+?)( in stock| left)?$",
    r"^(stock|how much stock|stock level|quantity) (of|for) (?P<item>.+)$",
    r"^is (the )?(?P<item>.+?) in stock$",
    r"^(find|search for|search|look up|lookup|show me|where is|where are) (the |a |an |some )?(?P<item>.+)$",
    r"^(what is the price of|price of|how much is|how much are|how much does|how much do) (the |a |an )?(?P<item>.+?)( cost| sell for)?$",
)]
QUICK_MOVE = re.compile(r"^(move|transfer|put|send|take) (?P<n>\d+) (?P<item>.+?) (to|onto|on|into) (the )?(?P<to>pos|shop|till|point of sale|counter|farm|farm use)$")


def answered(ctx, reply):
    return {"reply": reply, "sources": [{"label": label, "url": url} for label, url in ctx.sources], "actions": ctx.actions, "drafts": [], "notes": []}


def spellings(term):
    """A name as said, and without a plural ending (hoes: hoe; boxes: box)."""
    term = re.sub(r"^(the|a|an|any|some|our) ", "", term.strip())
    return [term] + ([term[:-2]] if term.endswith("es") else []) + ([term[:-1]] if term.endswith("s") else [])


def stock_matches(ctx, term):
    """The stock a term names, tried as it is and without a plural ending: (the items by code, whether more fit than were read), or ({}, False)."""
    for t in spellings(term):
        if len(t) < 2:
            continue
        found = find_stock(ctx, {"query": t, "limit": 6})
        items = {}
        for r in found["pos"]["items"]:
            items.setdefault(r["code"], {"name": r["name"], "price": r["price"]})["pos"] = r["in_stock"]
        for r in found["inventory"]["items"]:
            entry = items.setdefault(r["code"], {"name": r["name"], "price": r["price"]})
            entry["store"] = r["quantity"]
        if items:
            return items, max(found["pos"]["matching"], found["inventory"]["matching"]) > 6
    return {}, False


def quick_answer(ctx, message):
    """The answer the app gives itself to a simple question, in the same form as the assistant's, or None to ask Gemini."""
    t = plain_words(message)
    name = assistant.name_of(ctx.user).lower()
    if t.startswith(name + " "):
        t = t[len(name) + 1:]
    if not t or len(t) > 120:
        return None
    if QUICK_TODAY.match(t):
        a = attention_today(ctx, {})
        lines = assistant.due_lines(a, "today")
        low = (a.get("pos_low_stock") or {}).get("items") or []
        if low:
            lines.append("Low on the POS: " + ", ".join(f"{r['name']} ({r['left']} left)" for r in low[:5]) + (f" and {len(low) - 5} more" if len(low) > 5 else ""))
        coming = a.get("batches_ready_within_a_week") or []
        if coming:
            lines.append("Ready within a week: " + ", ".join(f"{b['batch']} (in {b['in_days']} days)" for b in coming[:3]))
        lines += assistant.money_lines(ctx, a)
        if a.get("requests_waiting_for_approval"):
            lines.append(assistant.plural(a["requests_waiting_for_approval"], "request is", "requests are") + " waiting for your approval")
        return answered(ctx, ("Today:\n" + "\n".join("- " + line for line in lines)) if lines else "Nothing needs attention today.")
    if QUICK_RESTOCK.match(t):
        s = stock_report(ctx, "all", "", 5)
        lines = assistant.stock_lines(ctx, s)
        if not lines:
            return answered(ctx, "Nothing needs restocking for the next " + f"{ctx.user['stock_cover_days'] or DEFAULT_COVER} days, and there is nothing to watch out for.")
        text = "\n".join("- " + line for line in lines)
        if s.get("ask"):
            text += "\nI cannot tell yet for " + ", ".join(a["item"] for a in s["ask"]) + ": there are too few records. How much do you want to keep?"
        return answered(ctx, "What to restock:\n" + text + "\nSay which to prepare and I will put it on a card for you to confirm.")
    if QUICK_LOW.match(t):
        found = find_stock(ctx, {"low_only": True, "limit": 10})
        rows = [f"{r['name']}: {r['in_stock']} on the POS" for r in found["pos"]["items"]] + [f"{r['name']}: {r['quantity']} in the store" for r in found["inventory"]["items"]]
        if not rows:
            return answered(ctx, f"Nothing is low: every item has at least {found['low_stock_means_under']}.")
        more = found["pos"]["matching"] + found["inventory"]["matching"] - len(rows)
        return answered(ctx, f"Low (under {found['low_stock_means_under']}):\n" + "\n".join("- " + r for r in rows) + (f"\n- and {more} more" if more > 0 else ""))
    if QUICK_COUNT.match(t):
        found = find_stock(ctx, {"limit": 1})
        n, listed = found["inventory"]["matching"], found["pos"]["matching"]
        return answered(ctx, f"There {'is' if n == 1 else 'are'} **{n}** {'item' if n == 1 else 'items'} in the inventory ({found['inventory']['units']:,} units in all) and "
                             f"{listed} listed on the POS ({found['pos']['units']:,} units).")
    move = QUICK_MOVE.match(t)
    if move and assistant.may(ctx.user, "transfer"):
        destination = "farm" if move.group("to").startswith("farm") else "pos"
        for name in spellings(move.group("item")):
            try:
                prepare_transfer(ctx, {"item": name, "quantity": int(move.group("n")), "to": destination})
                break
            except ToolError:
                continue
        else:
            return None   # nothing fits, several do, or there is not enough: the assistant asks about it
        card = ctx.actions[-1]
        return answered(ctx, f"I prepared it below: **{card['title']}**. Nothing moves until you press Confirm.")
    for pattern in QUICK_LOOKUP:
        m = pattern.match(t)
        if not m:
            continue
        items, more = stock_matches(ctx, m.group("item"))
        if not items:
            return None   # not stock: perhaps a customer or an invoice, for the assistant
        lines = []
        for code, it in list(items.items())[:5]:
            where = []
            if "pos" in it:
                where.append(f"**{it['pos']}** on the POS")
            if "store" in it:
                where.append(f"**{it['store']}** in the store")
            lines.append(f"{it['name']} ({code}): " + " and ".join(where) + (f", sells at {ctx.money(it['price'])}" if it.get("price") else ""))
        text = "\n".join("- " + line for line in lines) if len(lines) > 1 else lines[0]
        return answered(ctx, text + ("\n- and more: say more of the name to narrow it" if more or len(items) > 5 else ""))
    return None
