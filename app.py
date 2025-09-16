import os
import re
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from flask import Flask, request, jsonify
import dateparser

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request as GRequest
from google_auth_oauthlib.flow import Flow

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "Europe/Moscow")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
REDIRECT_URI = f"{BASE_URL}/auth/callback"
STORE_PATH = "store.json"
LOCK = threading.Lock()
OAUTH_STATE = {}

def load_store():
    if not os.path.exists(STORE_PATH):
        return {"users": {}}
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}

def save_store(data):
    with LOCK:
        with open(STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def get_user(store, chat_id):
    chat_id = str(chat_id)
    if chat_id not in store["users"]:
        store["users"][chat_id] = {"tz": DEFAULT_TZ, "creds": None, "reminders": {}}
    return store["users"][chat_id]

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        print("send_message error:", e)

def set_webhook():
    if not BASE_URL:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    data = {
        "url": f"{BASE_URL}/webhook",
        "drop_pending_updates": True,
        "allowed_updates": ["message"]
    }
    try:
        r = requests.post(url, json=data, timeout=10)
        print("setWebhook:", r.status_code, r.text)
    except Exception as e:
        print("setWebhook error:", e)

def build_service_for_chat(chat_id):
    store = load_store()
    user = get_user(store, chat_id)
    creds_json = user.get("creds")
    if not creds_json:
        return None
    try:
        creds = Credentials.from_authorized_user_info(creds_json, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GRequest())
            user["creds"] = json.loads(creds.to_json())
            save_store(store)
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        print("Google service build error:", e)
        return None

def parse_event_text(text, tz_str):
    if not text.strip():
        return None, None, None, "Пустой текст события"
    
    text = re.sub(r'(?i)\bв(?=\d)', 'в ', text.strip())
    text = re.sub(r'(?i)(?<=\bв\s)(\d{1,2})\.(\d{2})\b', r'\1:\2', text)
    
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        return None, None, None, "Неверный часовой пояс"
    
    dur_minutes = None
    has_half = re.search(r"\bполчаса\b", text, re.IGNORECASE)
    m = re.search(r"\bна\s*(\d+)\s*(?:минут(?:ы)?|мин\.?|мин|m)\b", text, re.IGNORECASE)
    h = re.search(r"\bна\s*(\d+)\s*(?:час(?:а|ов)?|ч\.?|ч|h)\b", text, re.IGNORECASE)
    
    if has_half:
        dur_minutes = 30
    if m:
        dur_minutes = int(m.group(1))
    elif h:
        dur_minutes = int(h.group(1)) * 60
    
    until = re.search(r"\bдо\s+(\d{1,2}(?::|\.)\d{2})\b", text, re.IGNORECASE)
    text_for_parse = text
    
    text_for_parse = re.sub(
        r"\bна\s*(полчаса|\d+\s*(?:минут(?:ы)?|мин\.?|мин|m|час(?:а|ов)?|ч\.?|ч|h))\b",
        "", text_for_parse, flags=re.IGNORECASE
    )
    text_for_parse = re.sub(
        r"\bдо\s+\d{1,2}(?::|\.)\d{2}\b",
        "", text_for_parse, flags=re.IGNORECASE
    )
    text_for_parse = re.sub(r"\s{2,}", " ", text_for_parse).strip()

    now = datetime.now(ZoneInfo(tz_str))
    dt = dateparser.parse(
        text_for_parse,
        languages=['ru'],
        settings={
            'PREFER_DATES_FROM': 'future',
            'RELATIVE_BASE': now,
            'TIMEZONE': tz_str,
            'TO_TIMEZONE': tz_str,
            'RETURN_AS_TIMEZONE_AWARE': True,
        }
    )
    
    if not dt:
        base = None
        if re.search(r"\bсегодня\b", text_for_parse, re.IGNORECASE):
            base = now.replace(second=0, microsecond=0)
        elif re.search(r"\bзавтра\b", text_for_parse, re.IGNORECASE):
            base = (now + timedelta(days=1)).replace(second=0, microsecond=0)
        elif re.search(r"\bпослезавтра\b", text_for_parse, re.IGNORECASE):
            base = (now + timedelta(days=2)).replace(second=0, microsecond=0)
        
        hhmm = re.search(r"\bв\s*(\d{1,2})(?::|\.)?(\d{2})?\b", text, re.IGNORECASE)
        if base and hhmm:
            h = int(hhmm.group(1))
            m = int(hhmm.group(2) or 0)
            try:
                dt = base.replace(hour=h, minute=m)
            except ValueError:
                return None, None, None, "Некорректное время"
        else:
            return None, None, None, "Не понял дату/время. Пример: 'завтра в 14:30 встреча на 30 мин'"

    start = dt.astimezone(tz)
    
    if until:
        time_parts = re.split(r"[:.]", until.group(1))
        if len(time_parts) < 2:
            return None, None, None, "Некорректное время окончания"
        hh = int(time_parts[0])
        mm = int(time_parts[1])
        end = start.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if end <= start:
            end += timedelta(days=1)
    else:
        dur_minutes = dur_minutes or 60
        end = start + timedelta(minutes=dur_minutes)
    
    summary = text
    summary = re.sub(r"\b(сегодня|завтра|послезавтра)\b", "", summary, flags=re.I)
    summary = re.sub(r"\bв\s+\d{1,2}(?::|\.)?\d{0,2}\b", "", summary, flags=re.I)
    summary = re.sub(
        r"\bна\s*(полчаса|\d+\s*(?:минут(?:ы)?|мин\.?|мин|m|час(?:а|ов)?|ч\.?|ч|h))\b",
        "", summary, flags=re.I
    )
    summary = re.sub(r"\bдо\s+\d{1,2}(?::|\.)\d{2}\b", "", summary, flags=re.I)
    summary = re.sub(r"\s{2,}", " ", summary).strip(" ,.-")
    
    if not summary:
        summary = "Событие"
    
    return summary, start, end, None

def handle_voice(chat_id, voice):
    pass

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        upd = request.get_json(force=True, silent=True) or {}
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        
        if not chat_id or (ALLOWED_CHAT_ID and chat_id != str(ALLOWED_CHAT_ID)):
            return jsonify(ok=True)
        
        if "text" in msg:
            handle_text(chat_id, msg["text"])
        else:
            send_message(chat_id, "Пришлите текстовое сообщение.")
        
        return jsonify(ok=True)
    except Exception as e:
        print("Webhook error:", e)
        return jsonify(ok=False), 500

def handle_text(chat_id, text):
    try:
        if text.startswith("/start"):
            send_message(chat_id, "Привет! Я добавляю события в Google Календарь. Пример: 'завтра в 11:00 встреча на 30 минут'")
            return
            
        if text.startswith("/tz"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                send_message(chat_id, "Укажите часовой пояс, например: /tz Europe/Moscow")
                return
            tz = parts[1].strip()
            try:
                ZoneInfo(tz)
                store = load_store()
                user = get_user(store, chat_id)
                user["tz"] = tz
                save_store(store)
                send_message(chat_id, f"Часовой пояс установлен: {tz}")
            except Exception:
                send_message(chat_id, "Неизвестный часовой пояс. Пример: Europe/Moscow")
            return
            
        if text.startswith("/connect"):
            if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and BASE_URL):
                send_message(chat_id, "Google OAuth не настроен")
                return
            client_config = {
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [REDIRECT_URI]
                }
            }
            flow = Flow.from_client_config(client_config, SCOPES, redirect_uri=REDIRECT_URI)
            auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
            OAUTH_STATE[state] = {
                "chat_id": str(chat_id),
                "code_verifier": getattr(flow, "code_verifier", None),
                "ts": time.time()
            }
            send_message(chat_id, f"Авторизация Google: {auth_url}")
            return
            
        if text.startswith("/add"):
            event_text = text[4:].strip()
        else:
            event_text = text
        
        store = load_store()
        user = get_user(store, chat_id)
        tz = user.get("tz", DEFAULT_TZ)
        summary, start, end, err = parse_event_text(event_text, tz)
        
        if err:
            send_message(chat_id, err)
            return
            
        service = build_service_for_chat(chat_id)
        if not service:
            send_message(chat_id, "Сначала выполните: /connect")
            return
            
        try:
            ev_id, link = add_event(service, summary, start, end, tz)
            send_message(chat_id, f"✅ Добавлено: {summary}\n🕒 {start.strftime('%d.%m %H:%M')}")
        except Exception as e:
            print("Ошибка добавления:", e)
            send_message(chat_id, "Ошибка при добавлении события")
    except Exception as e:
        print("Handle text error:", e)
        send_message(chat_id, "Произошла внутренняя ошибка")

def add_event(service, summary, start, end, tz_str):
    body = {
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": tz_str},
        "end": {"dateTime": end.isoformat(), "timeZone": tz_str},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 10}
            ]
        }
    }
    try:
        ev = service.events().insert(calendarId="primary", body=body).execute()
        return ev.get("id"), ev.get("htmlLink")
    except HttpError as e:
        content = e.content.decode('utf-8') if isinstance(e.content, bytes) else str(e)
        status_code = e.status_code if hasattr(e, 'status_code') else '?'
        if not status_code and hasattr(e, 'resp'):
            status_code = e.resp.status if e.resp else '?'
        print(f"Google API error ({status_code}): {content}")
        raise Exception("Google API error")
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        raise

@app.route("/", methods=["GET"])
def index():
    return "Service is running", 200

@app.route("/auth/callback", methods=["GET"])
def auth_callback():
    try:
        state = request.args.get("state")
        code = request.args.get("code")
        if not state or not code or state not in OAUTH_STATE:
            return "Invalid request", 400
        
        info = OAUTH_STATE.pop(state)
        chat_id = info["chat_id"]
        client_config = {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI]
            }
        }
        flow = Flow.from_client_config(client_config, SCOPES, redirect_uri=REDIRECT_URI)
        
        if hasattr(flow, "code_verifier") and info.get("code_verifier"):
            flow.code_verifier = info["code_verifier"]
        
        try:
            flow.fetch_token(authorization_response=request.url)
        except Exception:
            try:
                flow.fetch_token(code=code)
            except Exception:
                return "Authentication failed", 400
            
        creds = flow.credentials
        store = load_store()
        user = get_user(store, chat_id)
        user["creds"] = json.loads(creds.to_json())
        save_store(store)
        send_message(chat_id, "✅ Google Calendar подключен!")
        return "Authenticated successfully", 200
    except Exception as e:
        print("Auth callback error:", e)
        return "Authentication failed", 400

def reminder_loop():
    time.sleep(5)
    set_webhook()
    while True:
        try:
            store = load_store()
            for chat_id, user in store.get("users", {}).items():
                if not user.get("creds"):
                    continue
                
                service = build_service_for_chat(chat_id)
                if not service:
                    continue
                
                try:
                    tz = ZoneInfo(user.get("tz", DEFAULT_TZ))
                except Exception:
                    continue
                
                now = datetime.now(tz)
                time_min = (now - timedelta(minutes=1)).astimezone(timezone.utc).isoformat()
                time_max = (now + timedelta(minutes=70)).astimezone(timezone.utc).isoformat()
                
                try:
                    events = service.events().list(
                        calendarId="primary",
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
                        orderBy="startTime"
                    ).execute().get("items", [])
                except Exception:
                    continue
                
                reminders = user.setdefault("reminders", {})
                changed = False
                
                for event in events:
                    start_data = event.get("start", {})
                    if "dateTime" not in start_data:
                        continue
                    
                    try:
                        start_dt = datetime.fromisoformat(start_data["dateTime"].replace("Z", "+00:00")).astimezone(tz)
                    except Exception:
                        continue
                    
                    event_id = event["id"]
                    rem_data = reminders.setdefault(event_id, {
                        "sent60": False, 
                        "sent10": False,
                        "summary": event.get("summary", "Событие")
                    })
                    
                    start_str = start_dt.isoformat()
                    if "start_str" not in rem_data or rem_data["start_str"] != start_str:
                        rem_data["sent60"] = False
                        rem_data["sent10"] = False
                        rem_data["start_str"] = start_str
                        changed = True
                    
                    remaining_min = (start_dt - now).total_seconds() / 60
                    
                    if not rem_data["sent60"] and 59 <= remaining_min <= 61:
                        send_message(chat_id, f"⏰ Напоминание: {rem_data['summary']} через 1 час")
                        rem_data["sent60"] = True
                        changed = True
                    
                    if not rem_data["sent10"] and 9 <= remaining_min <= 11:
                        send_message(chat_id, f"⏰ Напоминание: {rem_data['summary']} через 10 минут")
                        rem_data["sent10"] = True
                        changed = True
                
                if changed:
                    save_store(store)
        except Exception as e:
            print("Reminder loop error:", e)
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=reminder_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)





