```python
import os
import logging
import datetime
import csv
from io import StringIO
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build


logging.basicConfig(level=logging.INFO)


SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")


creds = service_account.Credentials.from_service_account_file(
SERVICE_ACCOUNT_FILE, scopes=SCOPES
)
service = build("calendar", "v3", credentials=creds)


TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
"Привет! Я бот для Google Календаря. Доступные команды:\n"
"/add YYYY-MM-DD HH:MM Название — добавить событие\n"
"/today — события на сегодня\n"
"/week — события на неделю\n"
"/delete <ключевое слово или дата> — удалить события\n"
"/export — экспорт событий недели в CSV"
)


async def add_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
try:
date_str, time_str, *title_parts = context.args
title = " ".join(title_parts)
start_dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
end_dt = start_dt + datetime.timedelta(hours=1)


event = {
'summary': title,
'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'UTC'},
'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'UTC'},
}
service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
await update.message.reply_text(f"Добавлено: {title} {start_dt}")
except Exception as e:
await update.message.reply_text(f"Ошибка: {e}")


async def list_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
now = datetime.datetime.utcnow().isoformat() + 'Z'
end = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).isoformat() + 'Z'
events = service.events().list(calendarId=CALENDAR_ID, timeMin=now, timeMax=end,
singleEvents=True, orderBy='startTime').execute().get('items', [])
if not events:
await update.message.reply_text("Сегодня событий нет")
else:
text = "Сегодняшние события:\n"
for ev in events:
start = ev['start'].get('dateTime', ev['start'].get('date'))
text += f"- {ev['summary']} ({start})\n"
await update.message.reply_text(text)


async def list_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
now = datetime.datetime.utcnow().isoformat() + 'Z'
end = (datetime.datetime.utcnow() + datetime.timedelta(days=7)).isoformat() + 'Z'
events = service.events().list(calendarId=CALENDAR_ID, timeMin=now, timeMax=end,
singleEvents=True, orderBy='startTime').execute().get('items', [])
if not events:
await update.message.reply_text("На этой неделе событий нет")
else:
text = "События на неделю:\n"
for ev in events:
start = ev['start'].get('dateTime', ev['start'].get('date'))
text += f"- {ev['summary']} ({start})\n"
asyncio.run(main())

