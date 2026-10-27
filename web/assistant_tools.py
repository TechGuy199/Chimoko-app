"""What the assistant (assistant.py) may do, as tools Claude can choose from.

Every tool is read-only, except the prepare_* ones, which only write a card for the person to confirm. A tool never sees more than the signed-in
person could open in the app: each query is scoped to their business (ctx.biz), and a tool for an admin-only part of the app (admin=True) is
not even offered to an operator. Lists are capped, text from the records is shortened, and links are made here, never by Claude.
"""
import datetime
import json

from assistant import H, ToolError, clip, tool

# ---------- Descriptions of what a tool takes ----------
def S(description):
    return {"type": "string", "description": description}


def N(description):
    return {"type": "integer", "description": description}


def B(description):
    return {"type": "boolean", "description": description}


def E(description, *values):
    return {"type": "string", "enum": list(values), "description": description}


PERIOD_NAMES = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_year", "all_time")
PERIOD = E("A named period.", *PERIOD_NAMES)
FROM = S("First day, as YYYY-MM-DD. Use with to_date instead of period.")
TO = S("Last day (included), as YYYY-MM-DD.")
LIMIT = N("How many rows at most (default 20, up to 40).")


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
@tool("A quick picture of the business today: sales today and this month, what customers still owe, POS stock and low stock, pre-orders waiting, "
      "and deliveries and collections due. Use it first for general questions such as 'how are we doing?'.")
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


@tool("Sales totals for a period, optionally grouped: by day, month, item, department, customer, payment_status or seller. "
      "Use it for 'how much did we sell...', 'best sellers', 'sales by department'. Amounts are after discounts.",
      {"period": PERIOD, "from_date": FROM, "to_date": TO, "group_by": E("What to group the sales by (leave out for just the totals).", "day", "month", "item", "department", "customer", "payment_status", "seller"), "limit": LIMIT})
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
    group, limit = args.get("group_by"), whole(args, "limit", 15)
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


@tool("Find sales invoices by invoice number, customer name or phone, status, period, or what is unpaid / to be delivered / to be collected. "
      "Returns the matching invoices (newest first) with how many matched in all and their total.",
      {"query": S("Part of an invoice number, a customer's name or phone number."), "status": E("Payment status (cancelled ones are left out unless asked for).", "Paid", "Partial Payment", "Unpaid", "Cancelled"),
       "unpaid_only": B("Only invoices that still have a balance due."), "handover": E("Only sales still to be delivered, still to be collected, or already delivered.", "to_deliver", "to_collect", "delivered"),
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
        params + [whole(args, "limit", 20)],
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


@tool("Everything on one invoice: its items with quantities, prices and discounts, the totals, who sold it, and delivery or collection. For admins, the payments on it too.",
      {"invoice_no": S("The invoice number, for example INV-2026-0012.")}, required=["invoice_no"])
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


@tool("Who bought a product, and when: customers, phone numbers, quantities, prices and discounts, newest first, with totals. Search by product code or part of its name.",
      {"product": S("The product's code or part of its name."), "period": PERIOD, "from_date": FROM, "to_date": TO, "limit": LIMIT}, required=["product"])
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
        params + [whole(args, "limit", 20)],
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


@tool("Stock on hand: what is listed for sale on the POS (with selling price), and the inventory (with cost and supplier). Search by name or code; can show only low stock.",
      {"query": S("Part of a name or code."), "where": E("POS stock, inventory, or both (default both).", "pos", "inventory", "both"), "low_only": B("Only items running low (below the low-stock level)."),
       "department": S("Only this department."), "limit": LIMIT})
def find_stock(ctx, args):
    db, biz = ctx.db, ctx.biz
    q, dept, limit = text_of(args, "query"), text_of(args, "department", 40), whole(args, "limit", 20)
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


@tool("Greenhouse planting records (batches): what was planted, seedlings expected or counted, when they are ready, how many are pre-ordered, sold and still available.",
      {"query": S("Part of a plant, seed or batch name."), "stage": E("Where the batch stands.", "growing", "due", "ready", "record", "all"), "limit": LIMIT})
def plantings(ctx, args):
    db, biz = ctx.db, ctx.biz
    q, stage = text_of(args, "query"), choice(args, "stage", ("growing", "due", "ready", "record", "all"), "all")
    sql, params = "", []
    if q:
        sql = " AND (LOWER(name) LIKE ? OR LOWER(batch_no) LIKE ? OR LOWER(seed_name) LIKE ?)"
        params = [like(q)] * 3
    rows = db.all(f"SELECT * FROM plantings WHERE business_id=?{sql} ORDER BY ready_on DESC, id DESC LIMIT 200", [biz] + params)
    figures = [f for f in H.with_figures(db, biz, rows) if stage == "all" or f["stage"] == stage]
    limit = whole(args, "limit", 20)
    for f in figures[:2]:
        ctx.source(f["label"], H.url_for("planting_detail", planting_id=f["id"]))
    ctx.source("Planting Records", H.url_for("planting_list"))
    return {
        "matching": len(figures), "batches": [
            {"batch": f["label"], "planted_on": f["planted_on"], "seeds_planted": f["seeds_planted"], "seedlings": f["seedlings"], "ready_on": f["actual_ready_on"] or f["ready_on"],
             "stage": f["stage_label"], "days_to_ready": f["days"] if f["stage"] == "growing" else None, "pre_ordered_not_collected": f["pending"], "sold": f["sold"],
             "available": f["available"], "price": r2(f["price"]), "department": f["department"]}
            for f in figures[:limit]
        ],
    }


@tool("Customers' pre-orders of seedlings: who ordered what, how many, the collection day, and whether it is waiting, collected or cancelled.",
      {"status": E("Which pre-orders (default pending).", "pending", "collected", "cancelled", "all"), "customer": S("Part of the customer's name."), "limit": LIMIT})
def preorders(ctx, args):
    db, biz = ctx.db, ctx.biz
    status, who = choice(args, "status", ("pending", "collected", "cancelled", "all"), "pending"), text_of(args, "customer")
    sql, params = "", []
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
        [biz] + params + [whole(args, "limit", 20)],
    )
    ctx.source("Up Next & pre-orders", H.url_for("up_next"))
    return {
        "matching": count["n"], "seedlings": count["qty"], "preorders": [
            {"customer": clip(r["customer_name"], 60), "phone": r["customer_phone"] or "", "plant": f"{r['plant']} ({r['batch_no'] or 'planted ' + (r['planted_on'] or '')})", "qty": r["qty"],
             "price_each": r2(r["unit_price"]), "status": r["status"], "collect_on": r["collect_on"], "ordered": (r["created_at"] or "")[:10], "note": clip(r["note"], 80)}
            for r in rows
        ],
    }


@tool("Deliveries still to make and collections still waiting, by day, with the customers and what is on each sale. Overdue ones come first.",
      {"kind": E("Deliveries, collections, or both (default both).", "deliveries", "collections", "both"), "day": S("Only this day, as YYYY-MM-DD."), "limit": LIMIT})
def deliveries_and_collections(ctx, args):
    db, biz = ctx.db, ctx.biz
    kind, day, limit = choice(args, "kind", ("deliveries", "collections", "both"), "both"), (H.day_or_none(str(args.get("day"))) if args.get("day") else None), whole(args, "limit", 25)
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


@tool("Purchases of stock and other things bought (procurements): date, supplier, item, quantity, cost, how much is paid and what is still owed.",
      {"query": S("Part of an item name, code or the purchase invoice number."), "supplier": S("Part of the supplier's name."), "unpaid_only": B("Only purchases not fully paid."),
       "period": PERIOD, "from_date": FROM, "to_date": TO, "limit": LIMIT})
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
        [biz] + params + [whole(args, "limit", 20)],
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


@tool("Daily activities: the work recorded in the plots (greenhouse blocks, fields, benches): who did what, where and when.",
      {"period": PERIOD, "from_date": FROM, "to_date": TO, "worker": S("Part of a worker's name."), "plot_or_work": S("Part of a plot's name or of what was done."), "limit": LIMIT})
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
        [biz] + params + [whole(args, "limit", 25)],
    )
    ctx.source("Daily Activities", H.url_for("activities"))
    return {
        "period": label, "matching": count["n"], "activities": [
            {"date": r["work_date"], "plot": clip(r["plot_name"], 50), "department": r["department"], "worker": clip(r["worker_name"], 50), "when": r["period"], "what": clip(r["description"], 160),
             "product": clip(r["product"], 40) or None, "product_qty": r["product_qty"], "unit": r["product_unit"] or None}
            for r in rows
        ],
    }


@tool("This person's recent alerts (low stock, pre-orders and deliveries due, requests...), unread first.", {"limit": LIMIT})
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
    "planting": "Planting Records: each batch of seeds planted in the greenhouse, with mortality, ready date and price. A batch goes on the POS a week before it is ready. Up Next shows batches still growing.",
    "inventory": "Inventory lists stock items (with cost and selling price) by category; Procurements records purchases from suppliers (which add to stock unless marked as not stock) and what is paid; "
                 "Inventory Analysis shows what was bought and spent. Admins manage suppliers and their payments.",
    "customers": "Customers (admins): each customer's account with their invoices, what they owe and a statement (PDF). Names and phone numbers are matched to avoid duplicates; duplicates can be merged.",
    "who_bought": "Click any product name anywhere in the app to see who bought it: customers, phones, dates, quantities, prices and discounts, with a CSV. A planting record's Sold tile does the same.",
    "accounts": "Accounts (admins): every movement of money in and out, balances for cash, bank and mobile money (counted by hand, then kept up to date), who owes us and whom we owe, profit and the financial standing.",
    "reports": "Reports: sales, purchases and profit by department and period.",
    "staff": "Staff (admins): employees, payroll, payslips with NAPSA and NHIMA, and statements. Daily Activities records the work done in each plot.",
    "alerts": "Alerts come in the app and optionally by WhatsApp (Settings): low stock, pre-orders and collections due, deliveries due, and requests for admin permission.",
    "assistant": "The assistant (this chat) can look things up, explain the app and prepare sales, pre-orders, payments and delivery changes for you to confirm. Nothing is done until you press Confirm.",
}


@tool("How to do things in the app. Give a topic: " + ", ".join(GUIDE) + ". Leave the topic out to list them.", {"topic": E("The topic.", *GUIDE)})
def app_guide(ctx, args):
    topic = args.get("topic")
    if topic not in GUIDE:
        return {"topics": list(GUIDE)}
    return {"topic": topic, "guide": GUIDE[topic]}


# ================= Read-only tools for admins =================
@tool("Customer accounts (admins): how many invoices, what they bought in all, what they paid, what they owe, and when they last bought. Search by name or phone, or list who owes most.",
      {"query": S("Part of a name or phone number."), "owing_only": B("Only customers who owe money."), "limit": LIMIT}, admin=True)
def customers(ctx, args):
    db, biz = ctx.db, ctx.biz
    q, limit = text_of(args, "query"), whole(args, "limit", 15)
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


@tool("Supplier accounts (admins): purchases, what was paid, what is still owed, and the latest payments. Search by name or phone, or list whom the business owes most.",
      {"query": S("Part of a name or phone number."), "owing_only": B("Only suppliers who are owed money."), "limit": LIMIT}, admin=True)
def suppliers(ctx, args):
    db, biz = ctx.db, ctx.biz
    q, limit = text_of(args, "query"), whole(args, "limit", 15)
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


@tool("The financial picture (admins): money held in each account, what customers owe, what is owed to suppliers, stock at cost, what the business is worth, and for a period the money in and out, "
      "where it came from and what it went on, and profit.", {"period": PERIOD, "from_date": FROM, "to_date": TO}, admin=True)
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


@tool("Money in and out entries in Accounts (admins): date, direction, amount, how it was paid, category, who with and what for. Search or filter to answer 'what did we spend on...'.",
      {"query": S("Part of the party's name, the description or the category."), "direction": E("Money in or out.", "in", "out"), "period": PERIOD, "from_date": FROM, "to_date": TO, "limit": LIMIT}, admin=True)
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
        [biz] + params + [whole(args, "limit", 20)],
    )
    ctx.source("Accounts", H.url_for("accounts"))
    return {
        "period": label, "matching": totals["n"], "money_in": r2(totals["money_in"]), "money_out": r2(totals["money_out"]),
        "entries": [{"when": (r["occurred_at"] or "")[:16], "direction": r["direction"], "amount": r2(r["amount"]), "method": r["method"], "category": r["category"],
                     "party": clip(r["party"], 50), "what": clip(r["description"], 100)} for r in rows],
    }


@tool("Staff and pay (admins): employees (type, basic salary, start date) and payroll for a month: pay, NAPSA and NHIMA, net pay. National ID numbers and addresses are never shown.",
      {"employee": S("Part of an employee's name."), "month": S("Pay month as YYYY-MM (default this month)."), "limit": LIMIT}, admin=True)
def staff(ctx, args):
    db, biz = ctx.db, ctx.biz
    who = text_of(args, "employee")
    month = text_of(args, "month", 7)
    month = month if len(month) == 7 and month[4] == "-" else ctx.today[:7]
    sql, params = "", []
    if who:
        sql, params = " AND LOWER(full_name) LIKE ?", [like(who)]
    people = db.all(
        f"SELECT id, full_name, employment_type, basic_salary, start_date, expected_days_week FROM employees WHERE business_id=?{sql} ORDER BY full_name LIMIT ?", [biz] + params + [whole(args, "limit", 30)]
    )
    pay_sql, pay_params = "", []
    if who:
        pay_sql, pay_params = " AND LOWER(employee_name) LIKE ?", [like(who)]
    pay = db.all(
        f"""SELECT employee_name, calculated_basic, napsa_amt, nhima_amt, net_payable, worked_days, expected_days FROM payroll
            WHERE business_id=? AND pay_month=?{pay_sql} ORDER BY employee_name""",
        [biz, month] + pay_params,
    )
    ctx.source("Payroll", H.url_for("hr"))
    return {
        "employees": [{"name": clip(p["full_name"], 60), "type": p["employment_type"], "basic_salary": r2(p["basic_salary"]), "started": p["start_date"] or None, "days_a_week": p["expected_days_week"]} for p in people],
        "payroll_month": month, "paid_this_month": [{"name": clip(p["employee_name"], 60), "basic_for_days_worked": r2(p["calculated_basic"]), "worked_days": p["worked_days"], "of_days": p["expected_days"],
                                                     "napsa": r2(p["napsa_amt"]), "nhima": r2(p["nhima_amt"]), "net_pay": r2(p["net_payable"])} for p in pay],
        "total_net_pay": r2(sum(p["net_payable"] for p in pay)),
    }


@tool("The records of care (admins): stock adjustments (items removed or corrected), deleted invoices with their reasons, or operators' permission requests.",
      {"kind": E("Which record.", "stock_adjustments", "deleted_invoices", "permission_requests"), "period": PERIOD, "from_date": FROM, "to_date": TO, "limit": LIMIT}, required=["kind"], admin=True)
def audit_log(ctx, args):
    db, biz = ctx.db, ctx.biz
    kind = choice(args, "kind", ("stock_adjustments", "deleted_invoices", "permission_requests"), "")
    if not kind:
        raise ToolError("Which record: stock_adjustments, deleted_invoices or permission_requests?")
    first, last, label = period(ctx, args, "this_month")
    limit = whole(args, "limit", 20)
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


@tool("Prepare a sale for the person to confirm (they press Confirm; nothing is sold until then). Items must be listed on the POS.",
      {"items": {"type": "array", "description": "What is sold.", "items": {"type": "object", "properties": {
          "product": S("The product's code or name."), "quantity": N("How many."), "discount": S("Money off this line: an amount like 5, or a percentage like 10%. Leave out for none.")}, "required": ["product", "quantity"]}},
       "customer_name": S("The customer's name (leave out for a walk-in)."), "customer_phone": S("Their phone number."),
       "payment_status": E("Default Paid.", "Paid", "Unpaid", "Partial Payment"), "amount_paid": {"type": "number", "description": "For a partial payment: how much was paid now."},
       "payment_method": S("How it was paid: Cash, Bank transfer, Airtel Money, MTN Money, Cheque, Card or Other. Needed unless unpaid."), "payment_reference": S("A receipt or transaction number, if given."),
       "handover": E("Customer takes the goods now (default), collects on a later day, or we deliver.", "now", "later", "deliver"), "day": S("For later or deliver: the day, as YYYY-MM-DD (a collection must be from tomorrow).")},
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


@tool("Prepare a pre-order of seedlings for a customer, for the person to confirm. The seedlings are held from the batch once confirmed.",
      {"batch": S("The plant or batch (its name, batch number or POS code)."), "customer_name": S("The customer's name."), "customer_phone": S("Their phone number."), "quantity": N("How many seedlings."),
       "collect_on": S("The day they will collect, as YYYY-MM-DD (not before the batch is ready). Leave out if not known."), "note": S("A note.")},
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


@tool("Prepare a payment received on an invoice (admins), for the person to confirm. Say how much was received now.",
      {"invoice_no": S("The invoice number."), "amount_received": {"type": "number", "description": "How much was received now."}, "payment_method": S("Cash, Bank transfer, Airtel Money, MTN Money, Cheque, Card or Other."),
       "payment_reference": S("A receipt or transaction number, if given."), "note": S("A note.")}, required=["invoice_no", "amount_received", "payment_method"], admin=True)
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


@tool("Prepare a change to a sale's delivery or collection, for the person to confirm: mark it delivered, mark it collected, or move its delivery or collection day.",
      {"invoice_no": S("The invoice number."), "change": E("What to do.", "mark_delivered", "mark_collected", "set_delivery_day", "set_collection_day"),
       "day": S("For moving a day: the new day, as YYYY-MM-DD (today or later).")}, required=["invoice_no", "change"], perm="sell")
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
