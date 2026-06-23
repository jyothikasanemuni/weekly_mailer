"""
FM Invoice Readiness Mailer — Flask Backend
============================================
Local:  python app.py  →  http://localhost:5050
Render: set env var FM_MAPPINGS=<json_string> in dashboard
        (app reads from env on boot, writes back on every save)
"""

import io, os, json, random, smtplib, logging, threading, calendar
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText

import pandas as pd
import requests as req_lib
from flask import Flask, request, jsonify, render_template

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# ════════════════════════════════════════════════════════════════════════════
#  MAPPINGS STORAGE
#  ─────────────────────────────────────────────────────────────────────────
#  Strategy (in priority order):
#
#  1. LOCAL  — fm_mappings.json next to app.py          (works on your PC)
#  2. RENDER — FM_MAPPINGS env var (JSON string)        (works on Render)
#
#  On Render, the file system is ephemeral so we can't rely on a file.
#  Instead, the JSON is stored as an environment variable on the Render
#  dashboard. On every save we write back to the in-memory dict (immediate)
#  AND try to write the file (succeeds locally, silently fails on Render).
#
#  To set up on Render:
#  1. Dashboard → your service → Environment → Add env var
#  2. Key:   FM_MAPPINGS
#  3. Value: (paste the contents of your fm_mappings.json file)
#  4. Save → Render will redeploy with the new env var baked in.
# ════════════════════════════════════════════════════════════════════════════

MAPPINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fm_mappings.json")

# In-memory cache — loaded once at startup, updated on every save
_MAPPINGS_CACHE: dict = {}

_DEFAULT_MAPPINGS = {
    "sales": {}, "branch": {}, "invoice": "",
    "config": {}, "customer_emails": {}, "contacts": {}
}


def _load_from_env() -> dict:
    """Read FM_MAPPINGS env var (JSON string). Returns {} if not set."""
    raw = os.environ.get("FM_MAPPINGS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        log.info("Mappings loaded from FM_MAPPINGS env var (%d bytes)", len(raw))
        return data
    except json.JSONDecodeError as e:
        log.error("FM_MAPPINGS env var is not valid JSON: %s", e)
        return {}


def _load_from_file() -> dict:
    """Read fm_mappings.json. Returns {} if file missing or unreadable."""
    try:
        with open(MAPPINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        log.info("Mappings loaded from file: %s", MAPPINGS_FILE)
        return data
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("Could not read mappings file: %s", e)
        return {}


def _init_mappings():
    """Called once at startup. File takes priority over env var locally."""
    global _MAPPINGS_CACHE
    file_data = _load_from_file()
    if file_data:
        _MAPPINGS_CACHE = file_data
        return
    env_data = _load_from_env()
    if env_data:
        _MAPPINGS_CACHE = env_data
        # Write to file so local dev works for subsequent runs
        _write_file(_MAPPINGS_CACHE)
        return
    _MAPPINGS_CACHE = dict(_DEFAULT_MAPPINGS)
    log.info("No saved mappings found — starting with empty defaults")


def _write_file(data: dict):
    """Try to write JSON file. Silently skips on read-only fs (Render)."""
    try:
        with open(MAPPINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning("Could not write mappings file (expected on Render): %s", e)


def load_mappings() -> dict:
    """Return current mappings from in-memory cache (always fast)."""
    m = dict(_DEFAULT_MAPPINGS)
    m.update(_MAPPINGS_CACHE)
    return m


def save_mappings(data: dict):
    """
    Save to in-memory cache immediately (works everywhere) and
    try to also write the local file (works on PC, silently fails on Render).
    On Render, the in-memory cache IS the source of truth for the current
    process lifetime. Set FM_MAPPINGS env var to survive restarts.
    """
    global _MAPPINGS_CACHE
    _MAPPINGS_CACHE = data
    _write_file(data)
    log.info("Mappings saved to cache (%d sales, %d branches)",
             len(data.get("sales", {})), len(data.get("branch", {})))


# Initialise mappings at import time
_init_mappings()

# ── global state ──────────────────────────────────────────────────────────────
STATE = {
    "all_orders":      [],
    "filtered_orders": [],
    "grouped":         {},
    "send_log":        [],
    "sending":         False,
    "last_result":     None,
    "sel_month":       5,
    "sel_year":        2026,
}

MONTHS_LONG  = ["January","February","March","April","May","June",
                 "July","August","September","October","November","December"]
MONTHS_SHORT = ["JAN","FEB","MAR","APR","MAY","JUN",
                 "JUL","AUG","SEP","OCT","NOV","DEC"]

# ── week → ready date ─────────────────────────────────────────────────────────
VALID_WEEKS = {"WEEK1", "WEEK2", "WEEK3", "WEEK4"}

def parse_week_num(val):
    if not val or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip().upper().replace(" ", "")
    return {"WEEK1": 1, "WEEK2": 2, "WEEK3": 3, "WEEK4": 4}.get(s)

def calc_ready_date(completion_week, planned_week, month: int, year: int):
    wn = parse_week_num(completion_week) or parse_week_num(planned_week)
    if not wn:
        return None
    days_in_month = calendar.monthrange(year, month)[1]
    bands = {1: (1, 7), 2: (8, 14), 3: (15, 21), 4: (22, days_in_month)}
    s, e = bands[wn]
    e = min(e, days_in_month)
    candidates = [d for d in range(s, e + 1) if date(year, month, d).weekday() < 5]
    if not candidates:
        candidates = list(range(s, e + 1))
    return date(year, month, random.choice(candidates))

def fmt_date_long(d):
    if not d:
        return "—"
    if isinstance(d, str):
        return d
    return f"{d.day} {MONTHS_LONG[d.month - 1]} {d.year}"

# ── Excel parsing ─────────────────────────────────────────────────────────────
COLUMN_MAP = {
    "order number":          "order_number",
    "order no":              "order_number",
    "oa number":             "order_number",
    "booked date":           "booked_date",
    "booking date":          "booked_date",
    "order date":            "booked_date",
    "branch":                "branch",
    "division":              "division",
    "customer name":         "customer_name",
    "customer_name":         "customer_name",
    "po no.":                "po_number",
    "po no":                 "po_number",
    "po number":             "po_number",
    "purchase order":        "po_number",
    "value":                 "value",
    "may inv month":         "inv_month",
    "inv month":             "inv_month",
    "invoice month":         "inv_month",
    "planned week":          "planned_week",
    "completion week":       "completion_week",
    "spcl cases":            "spcl_cases",
    "special cases":         "spcl_cases",
    "hold details":          "hold_details",
    "sales rep":             "sales_rep",
    "salesperson":           "sales_rep",
    "sales person":          "sales_rep",
    "sales representative":  "sales_rep",
    "itemcode":              "item_code",
    "item code":             "item_code",
    "shipping instructions": "shipping",
    "terms of payment":      "payment_terms",
    "request date":          "request_date",
    "ready date":            "excel_ready_date",
    "customer email":        "customer_email",
    "customer email id":     "customer_email",
    "customer emails":       "customer_email",
    "customer mail":         "customer_email",
    "customer mail id":      "customer_email",
    "client email":          "customer_email",
    "email id":              "customer_email",
}

def normalise_col(name):
    key = str(name).strip().lower()
    return COLUMN_MAP.get(key, key.replace(" ", "_"))

def parse_excel(file_bytes, filename: str):
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(io.BytesIO(file_bytes), dtype=str)
    else:
        df = pd.read_excel(io.BytesIO(file_bytes))

    df.columns = [normalise_col(c) for c in df.columns]

    for col in ["customer_name", "completion_week", "planned_week",
                "sales_rep", "branch", "value", "order_number",
                "po_number", "booked_date", "inv_month", "item_code",
                "customer_email", "excel_ready_date"]:
        if col not in df.columns:
            df[col] = ""

    records = []
    for _, row in df.iterrows():
        def g(col):
            v = row.get(col, "")
            if pd.isna(v):
                return ""
            return str(v).strip()

        cname = g("customer_name")
        if not cname:
            continue

        records.append({
            "order_number":     g("order_number"),
            "booked_date":      g("booked_date"),
            "branch":           g("branch"),
            "division":         g("division"),
            "customer_name":    cname,
            "customer_email":   g("customer_email"),
            "po_number":        g("po_number"),
            "value":            g("value"),
            "inv_month":        g("inv_month"),
            "planned_week":     g("planned_week"),
            "completion_week":  g("completion_week"),
            "spcl_cases":       g("spcl_cases"),
            "hold_details":     g("hold_details"),
            "sales_rep":        g("sales_rep"),
            "item_code":        g("item_code"),
            "shipping":         g("shipping"),
            "payment_terms":    g("payment_terms"),
            "request_date":     g("request_date"),
            "excel_ready_date": g("excel_ready_date"),
            "ready_date":       None,
            "ready_date_long":  "—",
            "order_status":     "pending",
        })
    return records

# ── apply filter ──────────────────────────────────────────────────────────────
def apply_filter():
    month = STATE["sel_month"]
    year  = STATE["sel_year"]
    mabbr = MONTHS_SHORT[month - 1]

    filtered = []
    for o in STATE["all_orders"]:
        cw_raw = str(o.get("completion_week", "")).strip()
        pw_raw = str(o.get("planned_week",    "")).strip()
        cw = cw_raw.upper().replace(" ", "")
        pw = pw_raw.upper().replace(" ", "")

        order_status = "pending"
        active_week  = None
        rd           = None
        rd_long      = "—"

        if cw in VALID_WEEKS:
            active_week = cw
        elif cw == "R":
            order_status = "ready"
            excel_rd = str(o.get("excel_ready_date", "")).strip()
            rd_long  = excel_rd if excel_rd else "Ready"
        elif "READYBY" in cw or "READY" in cw:
            order_status = "ready"
            rd           = date(year, month, 1)
            rd_long      = fmt_date_long(rd)
        elif cw == "" or cw_raw == "":
            if pw in VALID_WEEKS:
                active_week = pw
            else:
                continue
        else:
            continue

        inv = str(o.get("inv_month", "")).strip().upper()
        if inv and inv != mabbr:
            continue
        if not inv and active_week not in VALID_WEEKS and order_status == "pending":
            continue

        if active_week in VALID_WEEKS:
            rd      = calc_ready_date(active_week, "", month, year)
            rd_long = fmt_date_long(rd)

        o2 = dict(o)
        o2["ready_date"]      = rd.isoformat() if rd else None
        o2["ready_date_long"] = rd_long
        o2["order_status"]    = order_status
        filtered.append(o2)

    STATE["filtered_orders"] = filtered
    grouped = {}
    for o in filtered:
        k = o["customer_name"]
        if k not in grouped:
            grouped[k] = {"customer_name": k, "orders": []}
        grouped[k]["orders"].append(o)
    STATE["grouped"] = grouped

def fmt_value(val_str):
    try:
        v = float(str(val_str).replace(",", ""))
        return "₹{:,.0f}".format(v)
    except Exception:
        return val_str or "—"

# ── email HTML builder ────────────────────────────────────────────────────────
def build_email_html(customer_name, orders, company, month_name, year):
    TH = (
        "background:#f1f5f9;font-size:10px;font-weight:700;color:#475569;"
        "text-transform:uppercase;letter-spacing:.05em;text-align:left;"
        "border-bottom:2px solid #cbd5e1;border-right:1px solid #e2e8f0;"
        "padding:9px 12px;white-space:nowrap;position:sticky;top:0;z-index:1;"
    )
    TH_LAST = TH.replace("border-right:1px solid #e2e8f0;", "border-right:none;")
    TD = (
        "padding:8px 12px;border-bottom:1px solid #e2e8f0;"
        "border-right:1px solid #e2e8f0;font-size:12px;color:#1e293b;"
        "vertical-align:middle;white-space:nowrap;"
    )
    TD_MONO  = TD + "font-family:'Courier New',monospace;font-size:11.5px;"
    TD_MUTED = TD + "color:#64748b;"
    TD_BOLD  = TD + "font-weight:600;"
    TD_GREEN_MID = TD + "font-weight:700;color:#15803d;"
    TR_EVEN  = "background:#f8fafc;"

    rows_html = ""
    for i, o in enumerate(orders):
        row_bg  = TR_EVEN if i % 2 == 0 else ""
        status  = o.get("order_status", "pending")
        rd_disp = o.get("ready_date_long", "—") or "—"
        rd_cell = (f"<td style='{TD_GREEN_MID}'>{rd_disp} ✓</td>"
                   if status == "ready"
                   else f"<td style='{TD_GREEN_MID}'>{rd_disp}</td>")
        rows_html += (
            f"<tr style='{row_bg}'>"
            f"<td style='{TD_MONO}'>{o.get('order_number','—') or '—'}</td>"
            f"<td style='{TD_MONO}'>{o.get('po_number','—') or '—'}</td>"
            f"<td style='{TD_MUTED}'>{o.get('booked_date','—') or '—'}</td>"
            + rd_cell +
            f"<td style='{TD_BOLD}border-right:none;'>{fmt_value(o.get('value',''))}</td>"
            "</tr>"
        )

    total_val = 0
    for o in orders:
        try:
            total_val += float(str(o.get("value","")).replace(",",""))
        except Exception:
            pass
    total_str = fmt_value(str(total_val)) if total_val else "—"

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#1e293b;">
  <div style="background:#fff;border-radius:10px;border:1px solid #dde3ec;box-shadow:0 2px 10px rgba(0,0,0,.07);">
    <div style="background:#1a3a5c;padding:18px 22px;border-radius:10px 10px 0 0;">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
        <h2 style="margin:0;color:#fff;font-size:16px;font-weight:700;">Order Readiness Update</h2>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          <span style="font-size:9px;font-weight:700;padding:3px 9px;border-radius:20px;background:#1e5fa5;color:#b5d4f4;white-space:nowrap;">GAUGES</span>
          <span style="font-size:9px;font-weight:700;padding:3px 9px;border-radius:20px;background:#15803d;color:#bbf7d0;white-space:nowrap;">{month_name.upper()} {year}</span>
        </div>
      </div>
      <p style="margin:6px 0 0;color:#7ab8e8;font-size:11px;">{company} &mdash; Automated notification</p>
    </div>
    <div style="padding:18px 22px;font-size:13px;color:#1e293b;line-height:1.75;">
     Dear <strong>{customer_name}</strong>,<br><br>

    Greetings from <strong>{company}</strong>!<br><br>

We are pleased to inform you that the materials for the orders listed below are progressing as scheduled and are expected to be ready on the respective dates shown for <strong>{month_name} {year}</strong>.<br><br>

As a valued customer, we would like to ensure a seamless process. We kindly request you to review the schedule and make the necessary arrangements in advance to avoid any delays.<br><br>

Thank you for your continued confidence in <strong>{company}</strong>. We greatly appreciate your business and remain committed to delivering quality products and dependable service. If you have any questions or require support, our team will be happy to assist you.<br><br>

<div style="display:block;width:100%;overflow-x:scroll;overflow-y:auto;
            max-height:420px;-webkit-overflow-scrolling:touch;
            border:1px solid #e2e8f0;border-radius:8px;">
  <table style="border-collapse:collapse;table-layout:auto;width:max-content;min-width:580px;">
    <thead>
      <tr>
            <th style="{TH}">Order No.</th>
            <th style="{TH}">PO No.</th>
            <th style="{TH}">Booked Date</th>
            <th style="{TH}">Ready Date</th>
            <th style="{TH_LAST}">Value (&#8377;)</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
      <br>
      For any queries or special requirements, please reach out to your dedicated
      sales representative or reply to this email.<br><br>
      Thank you for your continued business.<br><br>
      Warm regards,<br>
      <strong>Sales &amp; Dispatch Team</strong><br>
      {company}
    </div>
    <div style="background:#f8fafc;padding:12px 22px;border-top:1px solid #e2e8f0;border-radius:0 0 10px 10px;font-size:10.5px;color:#94a3b8;line-height:1.6;">
      Automated notification &middot; Forbes Marshall-HYD (Gauges) &middot; Please do not reply directly
    </div>
  </div>
</body></html>"""

# ── recipient resolution ──────────────────────────────────────────────────────
def get_recipients(order, override_customer_emails=None):
    mappings   = load_mappings()
    sales_map  = mappings.get("sales",  {})
    branch_map = mappings.get("branch", {})
    invoice_em = mappings.get("invoice", "")
    cust_email_map = mappings.get("customer_emails", {})

    to_list, cc_list = [], []
    cname = order.get("customer_name", "")

    if override_customer_emails:
        to_list += [e.strip() for e in override_customer_emails.split(",") if e.strip()]
    elif cust_email_map.get(cname):
        to_list += [e.strip() for e in cust_email_map[cname].split(",") if e.strip()]
    elif order.get("customer_email"):
        to_list += [e.strip() for e in order["customer_email"].split(",") if e.strip()]

    rep_emails_str = sales_map.get(order.get("sales_rep", ""), "")
    if rep_emails_str:
        cc_list += [e.strip() for e in rep_emails_str.split(",") if e.strip()]

    br_emails = branch_map.get(order.get("branch", ""), "")
    if br_emails:
        cc_list += [e.strip() for e in br_emails.split(",") if e.strip()]

    if invoice_em:
        cc_list += [e.strip() for e in invoice_em.split(",") if e.strip()]

    seen, cc_deduped = set(), []
    for e in cc_list:
        if e not in seen:
            seen.add(e)
            cc_deduped.append(e)

    return to_list, cc_deduped

# ── email send ────────────────────────────────────────────────────────────────
def send_via_smtp(gmail, password, to_list, cc_list, subject, html_body, company):
    clean_password = password.replace(" ", "")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{company} <{gmail}>"
    msg["To"]      = ", ".join(to_list) if to_list else gmail
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg.attach(MIMEText(html_body, "html"))
    all_rcpts = list(set(to_list + cc_list)) or [gmail]
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(gmail, clean_password)
        s.sendmail(gmail, all_rcpts, msg.as_string())

def send_via_n8n(webhook_url, to_list, cc_list, subject, html_body, customer_name):
    payload = {
        "to":        ", ".join(to_list),
        "cc":        ", ".join(cc_list) if cc_list else "",
        "subject":   subject,
        "html_body": html_body,
        "customer":  customer_name,
        "sent_at":   datetime.now().isoformat(),
    }
    resp = req_lib.post(webhook_url, json=payload,
                        headers={"Content-Type": "application/json"}, timeout=30)
    resp.raise_for_status()
    return resp

def send_one(cfg, to_list, cc_list, subject, html_body, company, customer_name=""):
    mode = cfg.get("send_mode", "smtp")
    if mode == "n8n":
        webhook = cfg.get("n8n_webhook", "").strip()
        if not webhook:
            raise ValueError("n8n webhook URL not configured.")
        send_via_n8n(webhook, to_list, cc_list, subject, html_body, customer_name)
    else:
        send_via_smtp(cfg.get("gmail",""), cfg.get("password",""),
                      to_list, cc_list, subject, html_body, company)

# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")

# ── NEW: health + mappings export endpoints for Render setup ──────────────────
@app.route("/api/health")
def api_health():
    """Health check + shows whether FM_MAPPINGS env var is set."""
    env_set  = bool(os.environ.get("FM_MAPPINGS", "").strip())
    cache_sz = len(json.dumps(_MAPPINGS_CACHE))
    file_ok  = os.path.exists(MAPPINGS_FILE)
    return jsonify({
        "status":           "ok",
        "storage_mode":     "env_var" if env_set else "file",
        "env_var_set":      env_set,
        "file_exists":      file_ok,
        "cache_size_bytes": cache_sz,
        "sales_count":      len(_MAPPINGS_CACHE.get("sales", {})),
        "branch_count":     len(_MAPPINGS_CACHE.get("branch", {})),
        "render_hint":      (
            "Set FM_MAPPINGS env var in Render dashboard to persist data across restarts."
            if not env_set else
            "FM_MAPPINGS env var is set — data will survive restarts."
        ),
    })

@app.route("/api/export_mappings")
def api_export_mappings():
    """
    Returns current mappings as a downloadable JSON string.
    Use this to copy-paste into Render's FM_MAPPINGS env var.
    """
    data = load_mappings()
    js   = json.dumps(data, ensure_ascii=False)
    return app.response_class(
        response=js,
        status=200,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=fm_mappings.json"}
    )

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    try:
        records = parse_excel(f.read(), f.filename)
        if not records:
            return jsonify({"error": "No valid rows found. Check column headers."}), 400
        STATE["all_orders"] = records
        apply_filter()
        log.info("Uploaded %s: %d total, %d filtered",
                 f.filename, len(records), len(STATE["filtered_orders"]))

        # Auto-load customer emails from Excel (don't overwrite manual saves)
        mappings = load_mappings()
        cust_email_map = mappings.get("customer_emails", {})
        changed = False
        for o in records:
            cname  = o.get("customer_name", "")
            cemail = o.get("customer_email", "")
            if cname and cemail and cname not in cust_email_map:
                cust_email_map[cname] = cemail
                changed = True
        if changed:
            mappings["customer_emails"] = cust_email_map
            save_mappings(mappings)

        return jsonify({
            "success":   True,
            "total":     len(records),
            "filtered":  len(STATE["filtered_orders"]),
            "customers": len(STATE["grouped"]),
            "orders":    STATE["filtered_orders"],
        })
    except Exception as e:
        log.exception("Upload error")
        return jsonify({"error": str(e)}), 500

@app.route("/api/set_month", methods=["POST"])
def api_set_month():
    data  = request.json or {}
    STATE["sel_month"] = int(data.get("month", 5))
    STATE["sel_year"]  = int(data.get("year",  2026))
    if STATE["all_orders"]:
        apply_filter()
    return jsonify({
        "success":   True,
        "filtered":  len(STATE["filtered_orders"]),
        "customers": len(STATE["grouped"]),
        "orders":    STATE["filtered_orders"],
    })

@app.route("/api/customers")
def api_customers():
    custs = []
    for k, v in STATE["grouped"].items():
        custs.append({
            "customer_name": k,
            "order_count":   len(v["orders"]),
            "has_date":      any(o.get("ready_date") or o.get("order_status") == "ready"
                                 for o in v["orders"]),
        })
    return jsonify(custs)

@app.route("/api/preview/<path:customer_name>")
def api_preview(customer_name):
    grp = STATE["grouped"].get(customer_name)
    if not grp:
        return jsonify({"error": "Customer not found"}), 404

    mappings   = load_mappings()
    company    = mappings.get("config", {}).get("company", "FORBES MARSHALL Pvt. Ltd (HYD).")
    month_name = MONTHS_LONG[STATE["sel_month"] - 1]
    gmail      = mappings.get("config", {}).get("gmail", "")
    cust_email_map  = mappings.get("customer_emails", {})
    override_emails = cust_email_map.get(customer_name, "")

    first_order = grp["orders"][0] if grp["orders"] else {}
    to_list, cc_list = get_recipients(first_order, override_emails or None)

    from_display = gmail or "(invoice sender — set in Mapping → Gmail Config)"
    to_display   = ", ".join(to_list) if to_list else "(no customer email — add in Mapping → Customer Emails)"
    cc_display   = ", ".join(cc_list) if cc_list else "(no CC emails mapped)"

    po   = next((o["po_number"] for o in grp["orders"] if o.get("po_number")), "")
    subj = f"Order Readiness Update – {company} | {customer_name}" + (f" | PO: {po}" if po else "")
    html = build_email_html(customer_name, grp["orders"], company, month_name, STATE["sel_year"])

    return jsonify({
        "html":            html,
        "subject":         subj,
        "from":            from_display,
        "to":              to_display,
        "cc":              cc_display,
        "customer_emails": override_emails,
    })

@app.route("/api/mappings", methods=["GET"])
def api_get_mappings():
    return jsonify(load_mappings())

@app.route("/api/mappings", methods=["POST"])
def api_save_mappings():
    data     = request.json or {}
    existing = load_mappings()
    for key in ["sales", "branch", "invoice", "config", "customer_emails", "contacts"]:
        if key in data:
            existing[key] = data[key]
    save_mappings(existing)
    log.info("Mappings saved via API")
    return jsonify({"success": True})

@app.route("/api/meta")
def api_meta():
    reps = sorted({o["sales_rep"] for o in STATE["filtered_orders"]
                   if o.get("sales_rep") and o["sales_rep"] != "No Sales Credit"})
    branches = sorted({o["branch"] for o in STATE["filtered_orders"] if o.get("branch")})
    cust_emails = {}
    for o in STATE["filtered_orders"]:
        cname  = o.get("customer_name", "")
        cemail = o.get("customer_email", "")
        if cname and cemail:
            existing = cust_emails.get(cname, set())
            for e in cemail.split(","):
                e = e.strip()
                if e:
                    existing.add(e)
            cust_emails[cname] = existing
    cust_emails_str = {k: ", ".join(sorted(v)) for k, v in cust_emails.items()}
    return jsonify({"reps": reps, "branches": branches, "customer_emails": cust_emails_str})

@app.route("/api/upload_contacts", methods=["POST"])
def api_upload_contacts():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    force = request.form.get("force", "false").lower() == "true"
    try:
        ext = os.path.splitext(f.filename)[1].lower()
        raw = f.read()
        if ext == ".csv":
            df = pd.read_csv(io.BytesIO(raw), dtype=str)
        else:
            df = pd.read_excel(io.BytesIO(raw))

        df.columns = [str(c).strip().lower() for c in df.columns]
        name_col  = next((c for c in df.columns if c in
                          ("name","member","member name","sales rep",
                           "sales representative","branch","branch name")), None)
        email_col = next((c for c in df.columns if "email" in c or "mail" in c), None)

        if not name_col or not email_col:
            return jsonify({"error": "Need a Name column and an Email column. Detected: "
                                      + ", ".join(df.columns)}), 400

        contacts = {}
        for _, row in df.iterrows():
            nm = str(row.get(name_col, "")).strip()
            em = str(row.get(email_col,"")).strip()
            if nm and em and nm.lower() != "nan" and em.lower() != "nan":
                contacts[nm] = em

        if not contacts:
            return jsonify({"error": "No valid Name/Email rows found."}), 400

        mappings   = load_mappings()
        sales_map  = mappings.get("sales", {})
        branch_map = mappings.get("branch", {})

        contacts_lower = {k.lower().strip(): v for k, v in contacts.items()}

        def find_match(target_name):
            tl = target_name.lower().strip()
            if tl in contacts_lower:
                return contacts_lower[tl]
            for cname, cemail in contacts_lower.items():
                if cname and (cname in tl or tl in cname):
                    return cemail
                target_parts  = set(p.strip() for p in tl.replace(","," ").split() if len(p.strip())>2)
                contact_parts = set(p.strip() for p in cname.replace(","," ").split() if len(p.strip())>2)
                if target_parts and contact_parts and target_parts & contact_parts:
                    return cemail
            return None

        sales_updated = 0
        for rep in list(sales_map.keys()):
            if force or not sales_map.get(rep):
                m = find_match(rep)
                if m: sales_map[rep] = m; sales_updated += 1

        branch_updated = 0
        for br in list(branch_map.keys()):
            if force or not branch_map.get(br):
                m = find_match(br)
                if m: branch_map[br] = m; branch_updated += 1

        existing_contacts = mappings.get("contacts", {})
        existing_contacts.update(contacts)
        mappings["sales"]    = sales_map
        mappings["branch"]   = branch_map
        mappings["contacts"] = existing_contacts
        save_mappings(mappings)

        return jsonify({
            "success": True, "total_contacts": len(contacts),
            "sales_updated": sales_updated, "branch_updated": branch_updated,
            "sales": sales_map, "branch": branch_map,
        })
    except Exception as e:
        log.exception("Contacts upload error")
        return jsonify({"error": str(e)}), 500

@app.route("/api/send", methods=["POST"])
def api_send():
    if not STATE["filtered_orders"]:
        return jsonify({"error": "No filtered orders. Upload a file and set month first."}), 400

    mappings = load_mappings()
    cfg      = mappings.get("config", {})
    company  = cfg.get("company", "FORBES MARSHALL Pvt. Ltd (HYD).")
    mode     = cfg.get("send_mode", "smtp")

    if mode == "n8n":
        if not cfg.get("n8n_webhook","").strip():
            return jsonify({"error": "n8n webhook URL not saved."}), 400
    else:
        if not cfg.get("gmail","").strip() or not cfg.get("password",""):
            return jsonify({"error": "Gmail address and App Password not saved."}), 400

    opts           = request.json or {}
    week_only      = opts.get("week_only",   False)
    skip_sent      = opts.get("skip_sent",   True)
    already_sent   = set(opts.get("sent_keys", []))
    filter_names   = opts.get("filter_names", None)
    email_overrides= opts.get("email_overrides", {})

    custs = list(STATE["grouped"].values())
    if filter_names:
        custs = [c for c in custs if c["customer_name"] in filter_names]
    elif week_only:
        custs = [c for c in custs if any(
            o.get("ready_date") or o.get("order_status")=="ready" for o in c["orders"])]
    if skip_sent and not filter_names:
        custs = [c for c in custs if c["customer_name"] not in already_sent]
    if not custs:
        return jsonify({"error": "No eligible customers after filters."}), 400

    STATE["send_log"]    = []
    STATE["sending"]     = True
    STATE["last_result"] = None
    month_name = MONTHS_LONG[STATE["sel_month"] - 1]
    year       = STATE["sel_year"]
    cust_email_map = mappings.get("customer_emails", {})

    def run():
        sent, failed = 0, 0
        results, errors = [], []
        for i, grp in enumerate(custs):
            cname  = grp["customer_name"]
            orders = grp["orders"]
            override = email_overrides.get(cname) or cust_email_map.get(cname,"")
            to_list, cc_list = get_recipients(orders[0] if orders else {}, override or None)
            po      = next((o["po_number"] for o in orders if o.get("po_number")),"")
            subject = (f"Order Readiness Update – Forbes Marshall-HYD (GAUGES) | {cname}"
                       + (f" | PO: {po}" if po else ""))
            html_body = build_email_html(cname, orders, company, month_name, year)
            idx    = f"[{i+1}/{len(custs)}]"
            to_str = ", ".join(to_list) if to_list else "(no email)"
            STATE["send_log"].append({"idx":i+1,"total":len(custs),"status":"info",
                                       "msg":f"{idx} Sending → {cname} | To: {to_str}"})
            if not to_list:
                failed += 1
                STATE["send_log"].append({"idx":i+1,"total":len(custs),"status":"skip",
                                           "msg":f"{idx} SKIPPED — no email for {cname}"})
                errors.append({"name":cname,"error":"No email mapped"})
                continue
            try:
                send_one(cfg, to_list, cc_list, subject, html_body, company, cname)
                sent += 1
                ts = datetime.now().strftime("%d %b %Y %H:%M")
                results.append({"time":ts,"customer":cname,"to":to_str,
                                  "orders":len(orders),"status":"Sent"})
                STATE["send_log"].append({"idx":i+1,"total":len(custs),"status":"ok",
                                           "msg":f"{idx} ✓ Sent → {cname} ({len(orders)} order(s))"})
            except smtplib.SMTPAuthenticationError:
                STATE["sending"] = False
                STATE["last_result"] = {"sent":sent,"failed":failed+(len(custs)-i-1),
                    "total":len(custs),"auth_error":True,
                    "message":"Gmail authentication failed. Use an App Password."}
                return
            except req_lib.exceptions.HTTPError as http_err:
                STATE["sending"] = False
                STATE["last_result"] = {"sent":sent,"failed":failed+(len(custs)-i-1),
                    "total":len(custs),"auth_error":True,
                    "message":f"Webhook error: {http_err}"}
                return
            except Exception as exc:
                failed += 1
                errors.append({"name":cname,"error":str(exc)})
                STATE["send_log"].append({"idx":i+1,"total":len(custs),"status":"err",
                                           "msg":f"{idx} ✗ FAILED → {cname}: {exc}"})

        STATE["last_result"] = {"sent":sent,"failed":failed,"total":len(custs),
                                  "results":results,"errors":errors}
        STATE["sending"] = False
        log.info("Send complete: %d sent, %d failed", sent, failed)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True, "total": len(custs)})

@app.route("/api/send_log")
def api_send_log():
    return jsonify({"log":STATE["send_log"],"sending":STATE["sending"],"result":STATE["last_result"]})

@app.route("/api/status")
def api_status():
    return jsonify({"total":len(STATE["all_orders"]),"filtered":len(STATE["filtered_orders"]),
                    "customers":len(STATE["grouped"]),"month":STATE["sel_month"],
                    "year":STATE["sel_year"],"sending":STATE["sending"]})

@app.route("/api/orders")
def api_orders():
    return jsonify(STATE["filtered_orders"])

if __name__ == "__main__":
    print("=" * 58)
    print("  FM Invoice Readiness Mailer")
    print("  Open browser →  http://localhost:5050")
    print("  Health check →  http://localhost:5050/api/health")
    print("=" * 58)
    app.run(debug=True, host="0.0.0.0", port=5050)
