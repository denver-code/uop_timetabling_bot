from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from icalendar import Calendar
import aiohttp
import asyncio
from datetime import datetime, timedelta
import pytz
import logging
import redis.asyncio as redis
from dotenv import load_dotenv
import aiohttp
import base64
from time import time
import json
import os
import re
import ssl

ssl_context = ssl.create_default_context()
ssl_context.options |= 0x4  # Enable legacy renegotiation

load_dotenv()


logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
redis_client = redis.from_url(REDIS_URL)

UMAMI_URL = os.getenv("UMAMI_URL")
UMAMI_WEBSITE_ID = os.getenv("UMAMI_WEBSITE_ID")
UMAMI_HOSTNAME = "telegram-bot"


async def send_umami_event(user_id: int, event_type: str, event_data: dict = None):
    """Send event data to Umami analytics."""
    payload = {
        "payload": {
            "website": UMAMI_WEBSITE_ID,
            "hostname": UMAMI_HOSTNAME,
            "screen": "telegram",
            "language": "en",
            "url": f"/telegram/{event_type}",
            "referrer": "",
            # Create unique identifier for user that's not their actual Telegram ID
            "id": base64.b64encode(str(user_id).encode()).decode(),
        },
        "type": "event",
        "data": {"name": event_type, **(event_data or {})},
    }

    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                UMAMI_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
                },
                ssl=ssl_context,
            ) as response:
                logging.info(f"Umami analytics response: {response.status}")
                return response.status == 200
    except Exception as e:
        logging.error(f"Failed to send Umami analytics: {e}")
        return False


# Middleware to track commands in Umami
def umami_middleware():
    """Middleware to track analytics in Umami for all commands."""

    async def middleware(handler, event, data):
        start_time = time()

        # Execute handler
        result = await handler(event, data)

        # Track command usage in Umami
        if isinstance(event, types.Message) and event.text.startswith("/"):
            command = event.text.split()[0][1:]  # Remove '/' prefix
            response_time = time() - start_time

            # Send event to Umami
            await send_umami_event(
                event.from_user.id,
                f"command_{command}",
                {
                    "response_time": round(response_time, 3),
                    "success": result is not None,
                },
            )

        return result

    return middleware


# Add Umami middleware to router
router.message.middleware(umami_middleware())


async def get_user_calendar_url(user_id: int) -> str:
    """Get calendar URL for specific user from Redis."""
    url = await redis_client.get(f"calendar_url:{user_id}")
    return url.decode("utf-8") if url else None


async def set_user_calendar_url(user_id: int, url: str) -> None:
    """Set calendar URL for specific user in Redis."""
    await redis_client.set(f"calendar_url:{user_id}", url.encode("utf-8"))


async def delete_user_calendar_url(user_id: int) -> None:
    """Delete calendar URL for specific user from Redis."""
    await redis_client.delete(f"calendar_url:{user_id}")


async def fetch_calendar(url: str):
    """Fetch and parse the ICS file from URL."""
    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.get(url, ssl=ssl_context) as response:
            if response.status == 200:
                ics_data = await response.text()
                return Calendar.from_ical(ics_data)
    return None


def format_event(event):
    """Format a calendar event into a readable message."""
    start = event["DTSTART"].dt
    end = event["DTEND"].dt
    summary = str(event["SUMMARY"])
    location = str(event.get("LOCATION", "No location specified"))

    return (
        f"📚 {summary}\n"
        f"⏰ {start.strftime('%H:%M')} - {end.strftime('%H:%M')}\n"
        f"📍 {location}\n"
    )


def is_valid_port_ics_url(url: str) -> bool:
    """Validate if URL is from timetabling.port.ac.uk and ends with .ics"""
    pattern = r"^https?://timetabling\.port\.ac\.uk/.*\.ics$"
    return bool(re.match(pattern, url))


@router.message(Command("start"))
async def send_welcome(message: types.Message):
    """Welcome message and command list."""
    welcome_text = (
        "👋 Welcome to your University Schedule Bot!\n\n"
        "To get started, please send me your timetabling.port.ac.uk ICS link.\n\n"
        "Available commands:\n"
        "/now - Show current/next class\n"
        "/today - Show today's schedule\n"
        "/tomorrow - Show tomorrow's schedule\n"
        "/week - Show this week's schedule\n"
        "/next - Show next class"
    )
    await message.reply(welcome_text)


@router.message(F.text.regexp(r"https?://.*\.ics"))
async def handle_calendar_url(message: types.Message):
    """Handle calendar URL submission."""
    url = message.text.strip()

    if not is_valid_port_ics_url(url):
        await message.reply("❌ Please provide a valid timetabling.port.ac.uk ICS URL.")
        return

    # Test if calendar is accessible
    cal = await fetch_calendar(url)
    if not cal:
        await message.reply(
            "❌ Couldn't access the calendar. Please check if the URL is correct and accessible."
        )
        return

    await set_user_calendar_url(message.from_user.id, url)
    await message.reply(
        "✅ Calendar URL successfully saved! You can now use the schedule commands."
    )


@router.message(Command("now"))
async def show_now(message: types.Message):
    """Show current or next upcoming class for today."""
    url = await get_user_calendar_url(message.from_user.id)
    if not url:
        await message.reply(
            "❌ Please set your calendar URL first by sending me your timetabling.port.ac.uk ICS link."
        )
        return

    cal = await fetch_calendar(url)
    if not cal:
        await message.reply("❌ Failed to fetch calendar data.")
        return

    now = datetime.now(pytz.UTC)
    today_end = now.replace(hour=23, minute=59, second=59)

    current_event = None
    next_event = None

    for component in cal.walk("VEVENT"):
        event_start = component["DTSTART"].dt
        event_end = component["DTEND"].dt

        if isinstance(event_start, datetime):
            # Check if event is happening now
            if event_start <= now <= event_end:
                current_event = component
                break
            # Find next event today
            elif now < event_start <= today_end:
                if next_event is None or event_start < next_event["DTSTART"].dt:
                    next_event = component

    if current_event:
        response = "Current Class:\n\n" + format_event(current_event)
        await message.reply(response)
    elif next_event:
        time_until = next_event["DTSTART"].dt - now
        minutes_until = int(time_until.total_seconds() / 60)
        response = f"Next Class (in {minutes_until} minutes):\n\n" + format_event(
            next_event
        )
        await message.reply(response)
    else:
        await message.reply("No more classes scheduled for today! 🎉")


@router.message(Command("today"))
async def show_today(message: types.Message):
    """Show today's schedule."""
    url = await get_user_calendar_url(message.from_user.id)
    if not url:
        await message.reply(
            "❌ Please set your calendar URL first by sending me your timetabling.port.ac.uk ICS link."
        )
        return

    cal = await fetch_calendar(url)
    if not cal:
        await message.reply("❌ Failed to fetch calendar data.")
        return

    now = datetime.now(pytz.UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    events = []
    for component in cal.walk("VEVENT"):
        event_date = component["DTSTART"].dt
        if isinstance(event_date, datetime):
            if today <= event_date < tomorrow:
                events.append(component)

    if not events:
        await message.reply("No classes scheduled for today! 🎉")
        return

    response = "Today's Schedule:\n\n"
    for event in sorted(events, key=lambda x: x["DTSTART"].dt):
        response += format_event(event) + "\n"

    await message.reply(response)


@router.message(Command("tomorrow"))
async def show_tomorrow(message: types.Message):
    """Show tomorrow's schedule."""
    url = await get_user_calendar_url(message.from_user.id)
    if not url:
        await message.reply(
            "❌ Please set your calendar URL first by sending me your timetabling.port.ac.uk ICS link."
        )
        return

    cal = await fetch_calendar(url)
    if not cal:
        await message.reply("❌ Failed to fetch calendar data.")
        return

    now = datetime.now(pytz.UTC)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day_after = tomorrow + timedelta(days=1)

    events = []
    for component in cal.walk("VEVENT"):
        event_date = component["DTSTART"].dt
        if isinstance(event_date, datetime):
            if tomorrow <= event_date < day_after:
                events.append(component)

    if not events:
        await message.reply("No classes scheduled for tomorrow! 🎉")
        return

    response = "Tomorrow's Schedule:\n\n"
    for event in sorted(events, key=lambda x: x["DTSTART"].dt):
        response += format_event(event) + "\n"

    await message.reply(response)


@router.message(Command("next"))
async def show_next_class(message: types.Message):
    """Show the next upcoming class."""
    url = await get_user_calendar_url(message.from_user.id)
    if not url:
        await message.reply(
            "❌ Please set your calendar URL first by sending me your timetabling.port.ac.uk ICS link."
        )
        return

    cal = await fetch_calendar(url)
    if not cal:
        await message.reply("❌ Failed to fetch calendar data.")
        return

    now = datetime.now(pytz.UTC)
    next_event = None
    min_time_diff = None

    for component in cal.walk("VEVENT"):
        event_date = component["DTSTART"].dt
        if isinstance(event_date, datetime) and event_date > now:
            time_diff = event_date - now
            if min_time_diff is None or time_diff < min_time_diff:
                min_time_diff = time_diff
                next_event = component

    if not next_event:
        await message.reply("No upcoming classes found! 🎉")
        return

    time_until = next_event["DTSTART"].dt - now
    hours = int(time_until.total_seconds() / 3600)
    minutes = int((time_until.total_seconds() % 3600) / 60)

    response = f"Next Class (in {hours}h {minutes}m):\n\n" + format_event(next_event)
    await message.reply(response)


@router.message(Command("week"))
async def show_week(message: types.Message):
    """Show this week's schedule."""
    url = await get_user_calendar_url(message.from_user.id)
    if not url:
        await message.reply(
            "❌ Please set your calendar URL first by sending me your timetabling.port.ac.uk ICS link."
        )
        return

    cal = await fetch_calendar(url)
    if not cal:
        await message.reply("❌ Failed to fetch calendar data.")
        return

    now = datetime.now(pytz.UTC)
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)

    events = []
    for component in cal.walk("VEVENT"):
        event_date = component["DTSTART"].dt
        if isinstance(event_date, datetime):
            if week_start <= event_date < week_end:
                events.append(component)

    if not events:
        await message.reply("No classes scheduled for this week! 🎉")
        return

    response = "This Week's Schedule:\n\n"
    current_day = None

    for event in sorted(events, key=lambda x: x["DTSTART"].dt):
        event_date = event["DTSTART"].dt
        if current_day != event_date.date():
            current_day = event_date.date()
            response += f"\n📅 {current_day.strftime('%A, %B %d')}:\n\n"
        response += format_event(event) + "\n"

    await message.reply(response)


@router.message(Command("delete"))
async def delete_calendar_url(message: types.Message):
    """Delete calendar URL for the user."""
    await delete_user_calendar_url(message.from_user.id)
    await message.reply("✅ Calendar URL successfully deleted!")


async def main():
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
