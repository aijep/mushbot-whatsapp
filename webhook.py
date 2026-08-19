import os
from flask import Flask, request, jsonify
import requests
import sqlite3
from datetime import datetime

app = Flask(__name__)

# ✅ Secrets loaded from Render's Environment Variables (set in Render dashboard)
ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN")

DB_PATH = "mushbot.db"


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT,
        message TEXT,
        timestamp TEXT
    )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def log_submission(sender, message):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO submissions (sender, message, timestamp) VALUES (?, ?, ?)",
        (sender, message, datetime.now().isoformat())
    )
    conn.commit()
    cursor.close()
    conn.close()


def send_message(to, body):
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    resp = requests.post(url, headers=headers, json=data)
    print("DEBUG send_message status:", resp.status_code, resp.text)
    return resp


def send_buttons(to, text, buttons):
    # WhatsApp allows max 3 buttons for this message type
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {"type": "button", "body": {"text": text}, "action": {"buttons": buttons}}
    }
    resp = requests.post(url, headers=headers, json=data)
    print("DEBUG send_buttons status:", resp.status_code, resp.text)
    return resp


def send_list(to, text, button_text, rows):
    # Use this for menus with more than 3 options (up to 10 rows)
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
    print("DEBUG send_list status:", resp.status_code, resp.text)
    return resp


def send_main_menu(sender, header="🌿 Main Menu:"):
    send_list(sender, header, "Choose", [
        {"id": "products", "title": "🍄 Products"},
        {"id": "farms", "title": "🏡 Farms"},
        {"id": "trainings", "title": "🎓 Trainings"},
        {"id": "support", "title": "📞 Support"},
        {"id": "thanks", "title": "✅ Finish"}
    ])


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
            # Likely a status update (sent/delivered/read), not an incoming message
            return jsonify({"status": "ignored"}), 200

        message = value["messages"][0]
        sender = message["from"]

        print("DEBUG incoming message:", message)

        # ✅ Normalize text input (handles text + interactive buttons/lists)
        text = ""
        if "interactive" in message:
            interactive = message["interactive"]
            if "button_reply" in interactive:
                text = interactive["button_reply"]["id"].strip().lower()
            elif "list_reply" in interactive:
                text = interactive["list_reply"]["id"].strip().lower()
        elif "text" in message:
            text = message["text"]["body"].strip().lower()

        print("DEBUG normalized text:", text)

        # ✅ Welcome flow
        if text in ["hi", "hello", "start"]:
            send_main_menu(sender, "👋 Welcome to MUSHBOT!\nChoose an option below:")

        # ✅ Main menu
        elif text == "menu":
            send_main_menu(sender)

        # ✅ Submenu: Products
        elif text == "products":
            send_list(sender, "🍄 Mushroom Products:", "Choose", [
                {"id": "oyster", "title": "🌿 Oyster"},
                {"id": "button", "title": "🍄 Button"},
                {"id": "shiitake", "title": "🌿 Shiitake"},
                {"id": "menu", "title": "⬅️ Main Menu"}
            ])

        # ✅ Submenu details
        elif text == "oyster":
            send_message(sender, "🌿 Oyster Mushroom: Rich in protein, easy to cultivate, popular in gourmet dishes.\n\nType 'menu' to return.")
        elif text == "button":
            send_message(sender, "🍄 Button Mushroom: Commonly used in curries and pizzas, widely cultivated worldwide.\n\nType 'menu' to return.")
        elif text == "shiitake":
            send_message(sender, "🌿 Shiitake Mushroom: Known for medicinal properties and strong umami flavor.\n\nType 'menu' to return.")

        # ✅ Other options
        elif text == "farms":
            send_message(sender, "🏡 Visit our Mushroom Farms for hands-on cultivation experience.\n\nType 'menu' to return.")
        elif text == "trainings":
            send_message(sender, "🎓 Join our Mushroom Trainings to become a certified cultivator.\n\nType 'menu' to return.")
        elif text == "support":
            send_message(sender, "📞 Contact Support: +91-9876543210\n\nType 'menu' to return.")

        # ✅ Thanks flow
        elif text == "thanks":
            send_message(sender, "✅ Thank you! All your info has been submitted.")
            log_submission(sender, text)

        # ✅ Fallback: always show menu
        else:
            send_main_menu(sender)

        return jsonify({"status": "received"}), 200


@app.route('/')
def home():
    # Simple health check so visiting the root URL doesn't 404/500
    return "MUSHBOT webhook is running.", 200


# ✅ Initialize DB table on startup (safe to call every time)
try:
    init_db()
except Exception as e:
    print("DEBUG: DB init failed:", e)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
