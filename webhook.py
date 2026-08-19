import os
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-secret-key")

# Secrets loaded from Render's Environment Variables
ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

# Supabase Storage (for menu item images) — project URL and service role key
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")          # e.g. https://mxqcsdrrjflhzbeedxkx.supabase.co
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "menu-images")

# Supabase Postgres connection settings (Session Pooler - IPv4 compatible)
DB_HOST = os.environ.get("DB_HOST")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "postgres")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")


def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode="require",
        connect_timeout=10
    )


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS submissions (
        id SERIAL PRIMARY KEY,
        sender TEXT,
        message TEXT,
        timestamp TIMESTAMP
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS menu_items (
        id SERIAL PRIMARY KEY,
        item_key TEXT UNIQUE NOT NULL,
        parent_key TEXT NOT NULL DEFAULT 'main',
        title TEXT NOT NULL,
        body_text TEXT,
        image_url TEXT,
        sort_order INTEGER DEFAULT 0,
        active BOOLEAN DEFAULT TRUE
    )
    """)
    conn.commit()
    cursor.execute("ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS image_url TEXT")
    conn.commit()
    cursor.execute("ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS collects_registration BOOLEAN DEFAULT FALSE")
    conn.commit()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id SERIAL PRIMARY KEY,
        phone TEXT UNIQUE NOT NULL,
        name TEXT,
        address TEXT,
        email TEXT,
        stage TEXT DEFAULT 'new',
        created_at TIMESTAMP DEFAULT NOW(),
        completed_at TIMESTAMP
    )
    """)
    conn.commit()
    # In case the table already existed from before this update, add the column if missing
    cursor.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS email TEXT")
    conn.commit()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS training_flow_state (
        phone TEXT PRIMARY KEY,
        training_key TEXT NOT NULL,
        step TEXT NOT NULL,
        name TEXT,
        phone_number TEXT,
        address TEXT,
        email TEXT,
        updated_at TIMESTAMP DEFAULT NOW()
    )
    """)
    conn.commit()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS training_registrations (
        id SERIAL PRIMARY KEY,
        training_key TEXT NOT NULL,
        training_title TEXT,
        customer_phone TEXT NOT NULL,
        name TEXT,
        phone_number TEXT,
        address TEXT,
        email TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    )
    """)
    conn.commit()

    cursor.execute("SELECT COUNT(*) FROM menu_items")
    count = cursor.fetchone()[0]
    if count == 0:
        seed_rows = [
            ("products",  "main",     "Products",  None, 1),
            ("farms",     "main",     "Farms",     "Visit our Mushroom Farms for hands-on cultivation experience.\n\nType 'menu' to return.", 2),
            ("trainings", "main",     "Trainings", "Join our Mushroom Trainings to become a certified cultivator.\n\nType 'menu' to return.", 3),
            ("support",   "main",     "Support",   "Contact Support: +91-9876543210\n\nType 'menu' to return.", 4),
            ("thanks",    "main",     "Finish",    "Thank you! All your info has been submitted.", 5),
            ("oyster",    "products", "Oyster",    "Oyster Mushroom: Rich in protein, easy to cultivate, popular in gourmet dishes.\n\nType 'menu' to return.", 1),
            ("button",    "products", "Button",    "Button Mushroom: Commonly used in curries and pizzas, widely cultivated worldwide.\n\nType 'menu' to return.", 2),
            ("shiitake",  "products", "Shiitake",  "Shiitake Mushroom: Known for medicinal properties and strong umami flavor.\n\nType 'menu' to return.", 3),
        ]
        cursor.executemany(
            "INSERT INTO menu_items (item_key, parent_key, title, body_text, sort_order) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (item_key) DO NOTHING",
            seed_rows
        )
        conn.commit()

    cursor.close()
    conn.close()


def get_customer(phone):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM customers WHERE phone = %s", (phone,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def create_customer(phone):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO customers (phone, stage) VALUES (%s, 'awaiting_name') ON CONFLICT (phone) DO NOTHING",
        (phone,)
    )
    conn.commit()
    cursor.close()
    conn.close()


def update_customer(phone, **fields):
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields.keys())
    values = list(fields.values()) + [phone]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE customers SET {set_clause} WHERE phone = %s", values)
    conn.commit()
    cursor.close()
    conn.close()


def get_training_flow(phone):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM training_flow_state WHERE phone = %s", (phone,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def start_training_flow(phone, training_key):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO training_flow_state (phone, training_key, step)
        VALUES (%s, %s, 'awaiting_name')
        ON CONFLICT (phone) DO UPDATE SET
            training_key = EXCLUDED.training_key, step = 'awaiting_name',
            name = NULL, phone_number = NULL, address = NULL, email = NULL, updated_at = NOW()
    """, (phone, training_key))
    conn.commit()
    cursor.close()
    conn.close()


def update_training_flow(phone, **fields):
    if not fields:
        return
    fields["updated_at"] = datetime.now()
    set_clause = ", ".join(f"{k} = %s" for k in fields.keys())
    values = list(fields.values()) + [phone]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE training_flow_state SET {set_clause} WHERE phone = %s", values)
    conn.commit()
    cursor.close()
    conn.close()


def clear_training_flow(phone):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM training_flow_state WHERE phone = %s", (phone,))
    conn.commit()
    cursor.close()
    conn.close()


def save_training_registration(flow):
    item = get_item(flow["training_key"])
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO training_registrations
            (training_key, training_title, customer_phone, name, phone_number, address, email)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    """, (
        flow["training_key"],
        item["title"] if item else flow["training_key"],
        flow["phone"], flow["name"], flow["phone_number"], flow["address"], flow["email"]
    ))
    conn.commit()
    cursor.close()
    conn.close()


def get_children(parent_key):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute(
        "SELECT * FROM menu_items WHERE parent_key = %s AND active = TRUE ORDER BY sort_order ASC, id ASC",
        (parent_key,)
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def get_item(item_key):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM menu_items WHERE item_key = %s", (item_key,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row


def log_submission(sender, message):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO submissions (sender, message, timestamp) VALUES (%s, %s, %s)",
        (sender, message, datetime.now())
    )
    conn.commit()
    cursor.close()
    conn.close()


def upload_image_to_supabase(file_storage):
    """Uploads a Flask FileStorage object to Supabase Storage and returns its public URL, or None on failure."""
    if not file_storage or not file_storage.filename:
        return None
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("DEBUG: Supabase Storage not configured (missing SUPABASE_URL/SUPABASE_SERVICE_KEY)", flush=True)
        return None

    import time
    safe_name = "".join(c for c in file_storage.filename if c.isalnum() or c in "._-")
    path = f"{int(time.time())}_{safe_name}"

    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{path}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "apikey": SUPABASE_SERVICE_KEY,
        "Content-Type": file_storage.mimetype or "application/octet-stream",
    }
    resp = requests.put(upload_url, headers=headers, data=file_storage.read())
    print("DEBUG image upload status:", resp.status_code, resp.text[:300], flush=True)
    if resp.status_code in (200, 201):
        return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{path}"
    return None


def send_image(to, image_url, caption=None):
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    image_obj = {"link": image_url}
    if caption:
        image_obj["caption"] = caption
    data = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": image_obj}
    resp = requests.post(url, headers=headers, json=data)
    print("DEBUG send_image status:", resp.status_code, resp.text, flush=True)
    return resp


def send_message(to, body):
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    resp = requests.post(url, headers=headers, json=data)
    print("DEBUG send_message status:", resp.status_code, resp.text, flush=True)
    return resp


def send_template(to, template_name="hello_world", language_code="en_US"):
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code}
        }
    }
    resp = requests.post(url, headers=headers, json=data)
    print("DEBUG send_template status:", to, resp.status_code, resp.text, flush=True)
    return resp.status_code, resp.text


def send_list(to, text, button_text, rows):
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": text},
            "action": {
                "button": button_text,
                "sections": [{"title": "Menu", "rows": rows}]
            }
        }
    }
    resp = requests.post(url, headers=headers, json=data)
    print("DEBUG send_list status:", resp.status_code, resp.text, flush=True)
    return resp


def send_buttons(to, text, buttons):
    """buttons: list of {'id': ..., 'title': ...}, max 3"""
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in buttons
            ]}
        }
    }
    resp = requests.post(url, headers=headers, json=data)
    print("DEBUG send_buttons status:", resp.status_code, resp.text, flush=True)
    return resp


def send_menu(sender, parent_key, header):
    items = get_children(parent_key)
    if not items:
        send_message(sender, "No options available right now. Type 'menu' to return.")
        return
    rows = [{"id": item["item_key"], "title": item["title"][:24]} for item in items[:10]]
    send_list(sender, header, "Choose", rows)


def send_main_menu(sender, header="Main Menu:"):
    send_menu(sender, "main", header)


@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode == 'subscribe' and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    elif request.method == 'POST':
        data = request.get_json()

        try:
            value = data["entry"][0]["changes"][0]["value"]
        except (KeyError, IndexError, TypeError):
            return jsonify({"status": "ignored"}), 200

        if "messages" not in value:
            return jsonify({"status": "ignored"}), 200

        message = value["messages"][0]
        sender = message["from"]

        print("DEBUG incoming message:", message, flush=True)

        text = ""
        if "interactive" in message:
            interactive = message["interactive"]
            if "button_reply" in interactive:
                text = interactive["button_reply"]["id"].strip()
            elif "list_reply" in interactive:
                text = interactive["list_reply"]["id"].strip()
        elif "text" in message:
            text = message["text"]["body"].strip()

        text_lower = text.lower()
        print("DEBUG normalized text:", text_lower, flush=True)

        customer = get_customer(sender)
        if not customer:
            create_customer(sender)
            send_message(sender, "Welcome to MUSHBOT!\nBefore we start, please tell us your name:")
            return jsonify({"status": "received"}), 200

        stage = customer["stage"]

        if stage == "awaiting_name":
            update_customer(sender, name=text, stage="awaiting_address")
            send_message(sender, f"Thanks {text}! Now please share your delivery address:")
            return jsonify({"status": "received"}), 200

        if stage == "awaiting_address":
            update_customer(sender, address=text, stage="awaiting_email")
            send_message(sender, "Got it! Lastly, share your email address (optional) — or type 'skip' to continue without one:")
            return jsonify({"status": "received"}), 200

        if stage == "awaiting_email":
            email_value = None if text_lower in ["skip", "no", "none", "-"] else text
            update_customer(sender, email=email_value, stage="completed", completed_at=datetime.now())
            send_message(sender, "Thanks! Your details have been saved.")
            send_main_menu(sender, "Main Menu:")
            return jsonify({"status": "received"}), 200

        flow = get_training_flow(sender)
        if flow:
            if text_lower == "cancel":
                clear_training_flow(sender)
                send_message(sender, "Training registration cancelled.")
                send_main_menu(sender)
                return jsonify({"status": "received"}), 200

            fstep = flow["step"]
            if fstep == "awaiting_name":
                update_training_flow(sender, name=text, step="awaiting_phone")
                send_message(sender, "Thanks! Please share your contact phone number:")
                return jsonify({"status": "received"}), 200

            if fstep == "awaiting_phone":
                update_training_flow(sender, phone_number=text, step="awaiting_address")
                send_message(sender, "Got it. Please share your address:")
                return jsonify({"status": "received"}), 200

            if fstep == "awaiting_address":
                update_training_flow(sender, address=text, step="awaiting_email")
                send_message(sender, "Please share your email address, or type 'skip':")
                return jsonify({"status": "received"}), 200

            if fstep == "awaiting_email":
                email_value = None if text_lower in ["skip", "no", "none", "-"] else text
                update_training_flow(sender, email=email_value, step="awaiting_confirm")
                flow = get_training_flow(sender)
                titem = get_item(flow["training_key"])
                summary = (
                    f"Please confirm your registration for {titem['title'] if titem else flow['training_key']}:\n\n"
                    f"Name: {flow['name']}\nPhone: {flow['phone_number']}\n"
                    f"Address: {flow['address']}\nEmail: {flow['email'] or 'N/A'}"
                )
                send_buttons(sender, summary, [
                    {"id": "training_confirm", "title": "Join Training"},
                    {"id": "training_cancel", "title": "Cancel"}
                ])
                return jsonify({"status": "received"}), 200

            if fstep == "awaiting_confirm":
                if text_lower == "training_confirm":
                    save_training_registration(flow)
                    clear_training_flow(sender)
                    send_message(sender, "You're registered! We'll contact you with training details soon.")
                    send_main_menu(sender)
                elif text_lower == "training_cancel":
                    clear_training_flow(sender)
                    send_message(sender, "Registration cancelled.")
                    send_main_menu(sender)
                else:
                    send_message(sender, "Please tap 'Join Training' or 'Cancel' above.")
                return jsonify({"status": "received"}), 200

        if text_lower in ["hi", "hello", "start"]:
            name = customer["name"] or ""
            send_main_menu(sender, f"Welcome back, {name}!\nChoose an option below:")
        elif text_lower == "menu":
            send_main_menu(sender)
        else:
            item = get_item(text_lower)
            if item:
                children = get_children(item["item_key"])
                if children:
                    send_menu(sender, item["item_key"], item["title"] + ":")
                elif item.get("collects_registration"):
                    if item["body_text"]:
                        send_message(sender, item["body_text"])
                    start_training_flow(sender, item["item_key"])
                    send_message(sender, "Let's get you registered! Please share your full name:")
                else:
                    if item["body_text"]:
                        if item.get("image_url"):
                            send_image(sender, item["image_url"], caption=item["body_text"])
                        else:
                            send_message(sender, item["body_text"])
                    elif item.get("image_url"):
                        send_image(sender, item["image_url"])
                    if item["item_key"] == "thanks":
                        log_submission(sender, "thanks")
            else:
                send_main_menu(sender)

        return jsonify({"status": "received"}), 200


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


LOGIN_HTML = """
<!doctype html><html><head><title>Admin Login</title>
<style>body{font-family:sans-serif;max-width:400px;margin:80px auto;padding:20px}
input{width:100%;padding:10px;margin:8px 0;box-sizing:border-box}
button{padding:10px 20px;background:#2e7d32;color:#fff;border:none;cursor:pointer}
.error{color:red}</style></head><body>
<h2>MUSHBOT Admin Login</h2>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post">
<input type="password" name="password" placeholder="Admin password" required>
<button type="submit">Login</button>
</form></body></html>
"""

NAV_HTML = """<p><a href="{{ url_for('admin_panel') }}">Menu Items</a> | <a href="{{ url_for('admin_customers') }}">Customers</a> | <a href="{{ url_for('admin_trainings') }}">Trainings</a> | <a href="{{ url_for('admin_broadcast') }}">Broadcast</a> | <a href="{{ url_for('admin_logout') }}">Logout</a></p>"""

ADMIN_HTML = """
<!doctype html><html><head><title>Menu Admin</title>
<style>
body{font-family:sans-serif;max-width:1000px;margin:30px auto;padding:0 20px}
table{width:100%;border-collapse:collapse;margin-bottom:30px}
th,td{border:1px solid #ccc;padding:8px;text-align:left;font-size:14px}
th{background:#2e7d32;color:#fff}
form.inline{display:inline}
button{padding:6px 12px;cursor:pointer}
.del{background:#c62828;color:#fff;border:none}
.edit{background:#1565c0;color:#fff;border:none;text-decoration:none;padding:6px 12px;display:inline-block}
.addbox{background:#f5f5f5;padding:20px;border-radius:8px}
input,select,textarea{width:100%;padding:8px;margin:6px 0;box-sizing:border-box}
.save{background:#2e7d32;color:#fff;border:none;padding:10px 20px;cursor:pointer}
a{color:#1565c0}
</style></head><body>
<h2>MUSHBOT Menu Admin</h2>
{{ nav|safe }}

<table>
<tr><th>Image</th><th>Key</th><th>Parent</th><th>Title</th><th>Body Text</th><th>Order</th><th>Active</th><th>Actions</th></tr>
{% for item in items %}
<tr>
<td>{% if item.image_url %}<img src="{{ item.image_url }}" style="width:50px;height:50px;object-fit:cover;border-radius:4px">{% endif %}</td>
<td>{{ item.item_key }}</td>
<td>{{ item.parent_key }}</td>
<td>{{ item.title }}</td>
<td>{{ (item.body_text or '')[:60] }}</td>
<td>{{ item.sort_order }}</td>
<td>{{ 'Yes' if item.active else 'No' }}</td>
<td>
<a class="edit" href="{{ url_for('admin_edit', item_id=item.id) }}">Edit</a>
<form class="inline" method="post" action="{{ url_for('admin_delete', item_id=item.id) }}" onsubmit="return confirm('Delete this item?')">
<button class="del" type="submit">Delete</button>
</form>
</td>
</tr>
{% endfor %}
</table>

<div class="addbox">
<h3>Add New Menu Item</h3>
<form method="post" action="{{ url_for('admin_add') }}" enctype="multipart/form-data">
<label>Item Key (unique, lowercase, no spaces)</label>
<input type="text" name="item_key" required>
<label>Parent Key (use "main" for top-level, or another item's key for a submenu)</label>
<input type="text" name="parent_key" value="main" required>
<label>Title (button label shown in WhatsApp, keep short)</label>
<input type="text" name="title" required>
<label>Body Text (message sent when selected -- leave blank if this item has its own submenu)</label>
<textarea name="body_text" rows="3"></textarea>
<label>Picture (optional -- sent along with the body text when this item is selected)</label>
<input type="file" name="image" accept="image/*">
<label>Sort Order (number, lower shows first)</label>
<input type="number" name="sort_order" value="0">
<label><input type="checkbox" name="collects_registration" style="width:auto"> Collects training registration (starts a name/phone/address/email sign-up flow instead of just showing text)</label>
<button class="save" type="submit">Add Item</button>
</form>
</div>

</body></html>
"""

EDIT_HTML = """
<!doctype html><html><head><title>Edit Menu Item</title>
<style>
body{font-family:sans-serif;max-width:500px;margin:30px auto;padding:0 20px}
input,select,textarea{width:100%;padding:8px;margin:6px 0;box-sizing:border-box}
.save{background:#2e7d32;color:#fff;border:none;padding:10px 20px;cursor:pointer}
a{display:inline-block;margin-top:10px}
</style></head><body>
<h2>Edit: {{ item.item_key }}</h2>
{% if item.image_url %}<img src="{{ item.image_url }}" style="width:120px;height:120px;object-fit:cover;border-radius:6px;display:block;margin-bottom:10px">{% endif %}
<form method="post" enctype="multipart/form-data">
<label>Parent Key</label>
<input type="text" name="parent_key" value="{{ item.parent_key }}" required>
<label>Title</label>
<input type="text" name="title" value="{{ item.title }}" required>
<label>Body Text</label>
<textarea name="body_text" rows="4">{{ item.body_text or '' }}</textarea>
<label>Picture (upload a new one to replace the current image)</label>
<input type="file" name="image" accept="image/*">
<label>Sort Order</label>
<input type="number" name="sort_order" value="{{ item.sort_order }}">
<label><input type="checkbox" name="active" {{ 'checked' if item.active else '' }} style="width:auto"> Active</label>
<label><input type="checkbox" name="collects_registration" {{ 'checked' if item.collects_registration else '' }} style="width:auto"> Collects training registration</label>
<button class="save" type="submit">Save Changes</button>
</form>
<a href="{{ url_for('admin_panel') }}">&larr; Back to list</a>
</body></html>
"""

BROADCAST_HTML = """
<!doctype html><html><head><title>Broadcast</title>
<style>
body{font-family:sans-serif;max-width:800px;margin:30px auto;padding:0 20px}
textarea,input{width:100%;padding:8px;margin:6px 0;box-sizing:border-box}
.save{background:#2e7d32;color:#fff;border:none;padding:10px 20px;cursor:pointer}
a{color:#1565c0}
.note{background:#fff3cd;padding:12px;border-radius:6px;margin-bottom:20px;font-size:14px}
.results{background:#f5f5f5;padding:12px;border-radius:6px;margin-top:20px;font-family:monospace;font-size:13px;white-space:pre-wrap}
</style></head><body>
<h2>MUSHBOT Broadcast</h2>
{{ nav|safe }}

<div class="note">
Broadcasting to people who have NOT messaged you recently requires an approved
WhatsApp <b>template</b>. "hello_world" is a free sample template every WhatsApp
Business account gets automatically — use it to test broadcasting right now.
For real marketing content, submit a custom template in Meta Business Manager
for approval, then enter its exact name below.
</div>

<form method="post">
<label>Phone numbers (comma-separated, with country code, no + or spaces — e.g. 919876543210,919123456780)</label>
<textarea name="numbers" rows="4" required></textarea>

<label>Template name</label>
<input type="text" name="template_name" value="hello_world" required>

<label>Template language code</label>
<input type="text" name="language_code" value="en_US" required>

<button class="save" type="submit">Send Broadcast</button>
</form>

{% if results %}
<div class="results">{{ results }}</div>
{% endif %}

</body></html>
"""

CUSTOMERS_HTML = """
<!doctype html><html><head><title>Customers</title>
<style>
body{font-family:sans-serif;max-width:1000px;margin:30px auto;padding:0 20px}
table{width:100%;border-collapse:collapse}
th,td{border:1px solid #ccc;padding:8px;text-align:left;font-size:14px}
th{background:#2e7d32;color:#fff}
a{color:#1565c0}
</style></head><body>
<h2>MUSHBOT Customers</h2>
{{ nav|safe }}
<table>
<tr><th>Phone</th><th>Name</th><th>Address</th><th>Email</th><th>Stage</th><th>Joined</th></tr>
{% for c in customers %}
<tr>
<td>{{ c.phone }}</td>
<td>{{ c.name or '' }}</td>
<td>{{ c.address or '' }}</td>
<td>{{ c.email or '' }}</td>
<td>{{ c.stage }}</td>
<td>{{ c.created_at }}</td>
</tr>
{% endfor %}
</table>
</body></html>
"""


TRAININGS_HTML = """
<!doctype html><html><head><title>Training Registrations</title>
<style>
body{font-family:sans-serif;max-width:1100px;margin:30px auto;padding:0 20px}
table{width:100%;border-collapse:collapse}
th,td{border:1px solid #ccc;padding:8px;text-align:left;font-size:14px}
th{background:#2e7d32;color:#fff}
a{color:#1565c0}
</style></head><body>
<h2>MUSHBOT Training Registrations</h2>
{{ nav|safe }}
<table>
<tr><th>Training</th><th>Name</th><th>Phone</th><th>Address</th><th>Email</th><th>Customer WA #</th><th>Registered</th></tr>
{% for r in regs %}
<tr>
<td>{{ r.training_title }}</td>
<td>{{ r.name or '' }}</td>
<td>{{ r.phone_number or '' }}</td>
<td>{{ r.address or '' }}</td>
<td>{{ r.email or '' }}</td>
<td>{{ r.customer_phone }}</td>
<td>{{ r.created_at }}</td>
</tr>
{% endfor %}
</table>
</body></html>
"""


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_panel'))
        error = "Incorrect password."
    return render_template_string(LOGIN_HTML, error=error)


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))


@app.route('/admin')
@login_required
def admin_panel():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM menu_items ORDER BY parent_key ASC, sort_order ASC, id ASC")
    items = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template_string(ADMIN_HTML, items=items, nav=render_template_string(NAV_HTML))


@app.route('/admin/broadcast', methods=['GET', 'POST'])
@login_required
def admin_broadcast():
    results = None
    if request.method == 'POST':
        raw_numbers = request.form.get('numbers', '')
        template_name = request.form.get('template_name', 'hello_world').strip()
        language_code = request.form.get('language_code', 'en_US').strip()

        numbers = [n.strip() for n in raw_numbers.split(',') if n.strip()]
        lines = []
        for number in numbers:
            status, body = send_template(number, template_name, language_code)
            lines.append(f"{number} -> HTTP {status}: {body[:200]}")
        results = "\n".join(lines)

    return render_template_string(BROADCAST_HTML, nav=render_template_string(NAV_HTML), results=results)


@app.route('/admin/customers')
@login_required
def admin_customers():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM customers ORDER BY created_at DESC")
    customers = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template_string(CUSTOMERS_HTML, customers=customers, nav=render_template_string(NAV_HTML))


@app.route('/admin/trainings')
@login_required
def admin_trainings():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM training_registrations ORDER BY created_at DESC")
    regs = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template_string(TRAININGS_HTML, regs=regs, nav=render_template_string(NAV_HTML))


@app.route('/admin/add', methods=['POST'])
@login_required
def admin_add():
    item_key = request.form.get('item_key', '').strip().lower()
    parent_key = request.form.get('parent_key', 'main').strip().lower()
    title = request.form.get('title', '').strip()
    body_text = request.form.get('body_text', '').strip() or None
    sort_order = int(request.form.get('sort_order', 0) or 0)
    collects_registration = True if request.form.get('collects_registration') else False
    image_url = upload_image_to_supabase(request.files.get('image'))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO menu_items (item_key, parent_key, title, body_text, image_url, sort_order, collects_registration) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (item_key, parent_key, title, body_text, image_url, sort_order, collects_registration)
    )
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for('admin_panel'))


@app.route('/admin/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
def admin_edit(item_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == 'POST':
        parent_key = request.form.get('parent_key', 'main').strip().lower()
        title = request.form.get('title', '').strip()
        body_text = request.form.get('body_text', '').strip() or None
        sort_order = int(request.form.get('sort_order', 0) or 0)
        active = True if request.form.get('active') else False
        collects_registration = True if request.form.get('collects_registration') else False

        new_image_url = upload_image_to_supabase(request.files.get('image'))
        if new_image_url:
            cursor.execute(
                "UPDATE menu_items SET parent_key=%s, title=%s, body_text=%s, sort_order=%s, active=%s, image_url=%s, collects_registration=%s WHERE id=%s",
                (parent_key, title, body_text, sort_order, active, new_image_url, collects_registration, item_id)
            )
        else:
            cursor.execute(
                "UPDATE menu_items SET parent_key=%s, title=%s, body_text=%s, sort_order=%s, active=%s, collects_registration=%s WHERE id=%s",
                (parent_key, title, body_text, sort_order, active, collects_registration, item_id)
            )
        conn.commit()
        cursor.close()
        conn.close()
        return redirect(url_for('admin_panel'))

    cursor.execute("SELECT * FROM menu_items WHERE id = %s", (item_id,))
    item = cursor.fetchone()
    cursor.close()
    conn.close()
    return render_template_string(EDIT_HTML, item=item)


@app.route('/admin/delete/<int:item_id>', methods=['POST'])
@login_required
def admin_delete(item_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM menu_items WHERE id = %s", (item_id,))
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for('admin_panel'))


@app.route('/')
def home():
    return "MUSHBOT webhook is running.", 200


@app.route('/init-db')
def init_db_route():
    try:
        init_db()
        return "DB init succeeded. Tables are ready.", 200
    except Exception as e:
        return f"DB init FAILED: {repr(e)}", 500


try:
    init_db()
except Exception as e:
    print("DEBUG: DB init failed:", e, flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
