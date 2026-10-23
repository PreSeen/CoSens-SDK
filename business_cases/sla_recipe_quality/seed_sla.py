"""客服 SLA 初版：正常工作日上午可用，边界工单被报告异常。"""
from datetime import datetime, timedelta


def _working(day, calendar):
    return day.weekday() < 5 and day.isoformat() not in calendar.get('holidays', [])


def add_work_minutes(start_iso, minutes, calendar):
    current = datetime.fromisoformat(start_iso)
    remaining = int(minutes)
    while remaining > 0:
        if not _working(current.date(), calendar):
            current = (current + timedelta(days=1)).replace(hour=9, minute=0, second=0)
            continue
        if current.hour < 9:
            current = current.replace(hour=9, minute=0, second=0)
        if current.hour >= 18:
            current = (current + timedelta(days=1)).replace(hour=9, minute=0, second=0)
            continue
        current += timedelta(minutes=1)
        remaining -= 1
    return current.isoformat()


def work_minutes_between(start_iso, end_iso, calendar):
    current = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    total = 0
    while current < end:
        if _working(current.date(), calendar) and 9 <= current.hour < 18:
            total += 1
        current += timedelta(minutes=1)
    return total
