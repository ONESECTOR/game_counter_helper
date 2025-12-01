#!/usr/bin/env python3
"""Скрипт для инициализации истории выходных дней."""
import json
from pathlib import Path
from datetime import date

VACATION_HISTORY_FILE = Path("vacation_history.json")

# Выходные дни в ноябре
VACATION_DATES = [
    date(2025, 11, 14),
    date(2025, 11, 16),
    date(2025, 11, 19),
    date(2025, 11, 20),
    date(2025, 11, 22),
    date(2025, 11, 24),
    date(2025, 11, 25),
    date(2025, 11, 26),
    date(2025, 11, 27),
    date(2025, 11, 28),
    date(2025, 11, 29),
    date(2025, 11, 30),
]

# Загружаем существующую историю (если есть)
history = {}
if VACATION_HISTORY_FILE.exists():
    try:
        with VACATION_HISTORY_FILE.open("r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception as e:
        print(f"Ошибка загрузки существующей истории: {e}")
        history = {}

# Добавляем/обновляем записи для выходных дней
for vacation_date in VACATION_DATES:
    date_key = vacation_date.isoformat()
    if date_key not in history:
        history[date_key] = {
            "date": date_key,
            "day_status": "vacation",
            "question_sent": False,
            "question_sent_at": None,
            "answered": True,
            "answered_at": "2025-11-30T23:59:59",
            "answer_source": "legacy",
            "message_sent": True,
            "message_sent_at": "2025-11-30T13:00:00"
        }
    else:
        # Обновляем только если нужно
        history[date_key]["day_status"] = "vacation"
        if not history[date_key].get("answered"):
            history[date_key]["answered"] = True
            history[date_key]["answer_source"] = "legacy"

# Добавляем рабочие дни (остальные дни ноября с 11 по 30)
for day in range(11, 31):
    work_date = date(2025, 11, day)
    if work_date not in VACATION_DATES:
        date_key = work_date.isoformat()
        if date_key not in history:
            history[date_key] = {
                "date": date_key,
                "day_status": "work",
                "question_sent": False,
                "question_sent_at": None,
                "answered": True,
                "answered_at": "2025-11-30T23:59:59",
                "answer_source": "legacy",
                "message_sent": True,
                "message_sent_at": "2025-11-30T13:00:00"
            }

# Сохраняем историю
with VACATION_HISTORY_FILE.open("w", encoding="utf-8") as f:
    json.dump(history, f, ensure_ascii=False, indent=2)

print(f"История инициализирована: {len(history)} дней")
print(f"Выходных дней: {len([k for k, v in history.items() if v.get('day_status') == 'vacation'])}")

