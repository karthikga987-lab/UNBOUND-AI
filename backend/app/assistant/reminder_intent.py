"""Conservative, explicit reminder command parsing for ordinary conversation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time, timedelta, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.app.reminders.service import ReminderAmbiguousError
from backend.app.reminders.store import ReminderNotFoundError


class ReminderAction(StrEnum):
    CREATE = "create"
    CONFIRM = "confirm"
    LIST = "list"
    COMPLETE = "complete"
    DELETE = "delete"


@dataclass
class ReminderCommand:
    action: ReminderAction
    title: str | None = None
    due_date: date | str | None = None
    due_time: time | None = None
    ambiguous_time: bool = False
    all_day: bool = False
    invalid_time: bool = False


def _relative_date(text: str, timezone_name: str) -> date | str | None:
    lowered = text.casefold()
    if re.search(r"\btomorrow\b", lowered):
        return "tomorrow"
    if re.search(r"\btoday\b", lowered):
        return "today"
    days = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
    match = re.search(r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered)
    if match:
        try:
            local_today = datetime.now(ZoneInfo(timezone_name)).date()
        except (ZoneInfoNotFoundError, ValueError):
            return None
        delta = (days[match.group(1)] - local_today.weekday()) % 7
        return local_today + timedelta(days=delta or 7)
    match = re.search(r"\bin\s+(\d{1,2})\s+days?\b", lowered)
    if match:
        try:
            return datetime.now(ZoneInfo(timezone_name)).date() + timedelta(days=int(match.group(1)))
        except (ZoneInfoNotFoundError, ValueError):
            return None
    month_names = {
        name.casefold(): month for month, name in enumerate(("", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")) if name
    }
    month_names.update({name[:3].casefold(): month for name, month in month_names.items()})
    month_pattern = "|".join(sorted(month_names, key=len, reverse=True))
    match = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\.?[,]?\s+(\d{{4}})\b", lowered)
    if match:
        day, month, year = int(match.group(1)), month_names[match.group(2)], int(match.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            return None
    match = re.search(rf"\b({month_pattern})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(\d{{4}})\b", lowered)
    if match:
        month, day, year = month_names[match.group(1)], int(match.group(2)), int(match.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            return None
    match = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b", lowered)
    if match:
        day, month, year = map(int, match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


_MERIDIAN_PATTERN = r"(?:a\.?\s*m\.?|p\.?\s*m\.?|ante\s+meridiem|post\s+meridian)"
_CLOCK_MARKER = re.compile(
    rf"(?:(?:\bat\s+)?\d{{1,2}}(?::\d{{1,2}})?\s*{_MERIDIAN_PATTERN}\b|\bat\s+\d{{1,2}}(?::\d{{1,2}})?\b)",
    re.IGNORECASE,
)


def _parse_time(text: str) -> tuple[time | None, bool]:
    match = re.search(
        rf"\bat\s+(\d{{1,2}})(?::(\d{{2}}))?\s*({_MERIDIAN_PATTERN})?(?=\s|[.,!?]|$)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, False
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridian = re.sub(r"[.\s]", "", (match.group(3) or "").casefold())
    is_pm = meridian.startswith("p") or meridian == "postmeridiem"
    has_meridian = bool(meridian)
    if minute > 59 or hour > 23:
        return None, False
    if has_meridian:
        if hour < 1 or hour > 12:
            return None, False
        hour = hour % 12 + (12 if is_pm else 0)
    return time(hour, minute), not has_meridian and 1 <= hour <= 12


def _extract_title(text: str) -> str | None:
    candidate = text.strip()
    if ":" in candidate and re.search(r"\bremember\s+this\b", candidate, re.I):
        candidate = candidate.split(":", 1)[1]
    else:
        patterns = (
            r"\bremind\s+me\s+to\s+(.+)$",
            r"\bremind\s+me\b.*?\bto\s+(.+)$",
            r"\bremind\s+me\s+about\s+(.+)$",
            r"\bremember\s+to\s+remind\s+me\s+to\s+(.+)$",
            r"\b(?:i\s+)?need\s+to\s+remember\s+to\s+(.+)$",
            r"\bremember\s+to\s+(.+)$",
            r"\bdon['’]t\s+let\s+me\s+forget\s+(.+)$",
            r"\bcreate\s+(?:me\s+)?(?:a\s+)?(?:reminder|remainder)\s+(?:for|to)\s+(.+)$",
            r"\b(?:set|make|schedule|add)\s+(?:me\s+)?(?:a\s+)?(?:reminder|remainder)\s+(?:for|to)\s+(.+)$",
            r"\b(?:create|set|make|schedule|add)\s+(?:me\s+)?(?:a\s+)?(?:reminder|remainder)\s+(.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, candidate, re.I)
            if match:
                candidate = match.group(1)
                break
        else:
            return None
    candidate = re.sub(rf"\bat\s+\d{{1,2}}(?::\d{{2}})?\s*(?:{_MERIDIAN_PATTERN})?", " ", candidate, flags=re.I)
    candidate = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?[,]?\s+\d{4}\b", " ", candidate, flags=re.I)
    candidate = re.sub(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+\d{4}\b", " ", candidate, flags=re.I)
    candidate = re.sub(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b", " ", candidate)
    candidate = re.sub(r"\b(?:today|tomorrow|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|in\s+\d{1,2}\s+days?)\b", " ", candidate, flags=re.I)
    candidate = re.sub(r"\b(?:on|for)\s+(?:today|tomorrow|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", " ", candidate, flags=re.I)
    candidate = re.sub(r"\b(?:all[- ]day|whole day|for the day)\b", " ", candidate, flags=re.I)
    candidate = re.sub(r"\b(?:set\s+it\s+up\s+)?on\s+my\s+laptop\b", " ", candidate, flags=re.I)
    candidate = re.sub(r"\bset\s+it\s+up\b", " ", candidate, flags=re.I)
    candidate = re.sub(r"\b(?:in\s+unbound|locally)\b", " ", candidate, flags=re.I)
    candidate = re.sub(r"^\s*(?:named|called)\s+", "", candidate, flags=re.I)
    candidate = re.sub(r"^\s*(?:of|on|for)\s+", "", candidate, flags=re.I)
    candidate = re.sub(r"^\s*remind(?:ing)?\s+me\s+about\s+", "", candidate, flags=re.I)
    candidate = candidate.strip(" \t\r\n.,!?;:")
    candidate = re.sub(r"\s+(?:of|on|for)\s*$", "", candidate, flags=re.I)
    candidate = re.sub(r"\s+", " ", candidate).strip(" \t\r\n.,!?;:")
    return candidate[:200] if candidate and candidate.casefold() not in {"of", "on", "for"} else None


def _extract_title_reply(text: str) -> str | None:
    """Extract a title from a clarification reply without retaining clock text."""
    candidate = _CLOCK_MARKER.sub(" ", text)
    candidate = re.sub(r"\b(?:today|tomorrow|next\s+\w+|in\s+\d+\s+days?)\b", " ", candidate, flags=re.I)
    candidate = re.sub(
        r"^\s*(?:of\s+)?(?:remind(?:ing)?\s+me\s+(?:about|to)\s+|(?:reminder\s+)?(?:about|for)\s+)",
        "",
        candidate,
        flags=re.I,
    )
    candidate = re.sub(r"\s+", " ", candidate).strip(" .,!?:;\t\r\n\"'")
    return candidate[:200] or None


def _has_clock_marker(text: str) -> bool:
    return bool(_CLOCK_MARKER.search(text))


def parse_reminder_command(message: str, timezone_name: str) -> ReminderCommand | None:
    text = message.strip().strip('"\'')
    lowered = text.casefold()
    if re.search(r"\b(how\s+(?:do|does|can)|what\s+is)\b", lowered) and re.search(r"\breminders?\b", lowered):
        return None
    if re.search(r"\b(confirm|approve)\s+(?:the\s+)?reminder\s+(?:for\s+)?", lowered):
        title = re.sub(r"^.*?\b(?:confirm|approve)\s+(?:the\s+)?reminder\s+(?:for\s+)?", "", text, flags=re.I).strip(" \t\r\n.,!?;:\"'")
        return ReminderCommand(ReminderAction.CONFIRM, title=title or None)
    if re.search(r"\b(mark|complete|finish)\b", lowered) and re.search(r"\breminder\b", lowered) and re.search(r"\b(completed|complete|done)\b", lowered):
        title = re.sub(r"[.!?]+\s*$", "", text.strip())
        title = re.sub(r"^\s*(?:please\s+)?(?:mark|complete|finish)\s+", "", title, flags=re.I)
        title = re.sub(r"\s+(?:reminder\s+)?as\s+(?:completed|complete|done)\s*$", "", title, flags=re.I)
        title = re.sub(r"\s+reminder\s*$", "", title, flags=re.I).strip(" \t\r\n.,!?;:\"'")
        title = re.sub(r"^(?:my|the)\s+", "", title, flags=re.I)
        return ReminderCommand(ReminderAction.COMPLETE, title=title or None)
    if re.search(r"\b(show|list|display)\b.*\breminders?\b", lowered) or re.search(r"\bwhat\s+reminders?\s+(?:do\s+i\s+have|are\s+there)\b", lowered):
        return ReminderCommand(ReminderAction.LIST)
    if re.search(r"\b(delete|remove|cancel)\b.*\breminders?\b", lowered):
        title = re.sub(r"^.*?\b(?:delete|remove|cancel)\s+(?:the\s+|my\s+)?", "", text, flags=re.I)
        title = re.sub(r"\s+reminders?\s*$", "", title, flags=re.I).strip(" \t\r\n.,!?;:\"'")
        title = re.sub(r"^(?:reminder\s+for|reminder)\s+", "", title, flags=re.I)
        return ReminderCommand(ReminderAction.DELETE, title=title or None)

    if re.search(r"\b(?:how\s+(?:do\s+i|can\s+i|to)|what\s+is|explain\s+how\s+to)\b.*\b(?:reminder|remainder)s?\b", lowered):
        return None
    if re.search(r"\bremind\s+me\s+(?:what|who|where|when|why|how)\b", lowered):
        return None
    has_reminder_request = bool(
        re.search(r"\bremind\s+me\b", lowered)
        or re.search(r"\b(?:create|set|make|schedule|add)\b.{0,50}\b(?:a\s+)?(?:reminder|remainder)\b", lowered)
        or re.search(r"\b(?:remember\s+to\s+remind\s+me|(?:i\s+)?need\s+to\s+remember\s+to|don['’]t\s+let\s+me\s+forget)\b", lowered)
        or re.search(r"\bremember\s+this\b.*\b(?:today|tomorrow|next\s+\w+|in\s+\d+\s+days?)\b", lowered)
    )
    if not has_reminder_request:
        return None
    due_date = _relative_date(text, timezone_name)
    due_time, ambiguous_time = _parse_time(text)
    title = _extract_title(text)
    invalid_time = _has_clock_marker(text) and due_time is None and not ambiguous_time
    all_day = bool(re.search(r"\b(?:all[- ]day|whole day|for the day)\b", lowered))
    return ReminderCommand(ReminderAction.CREATE, title, due_date, due_time, ambiguous_time, all_day, invalid_time)


def _clarification(draft: dict[str, object], timezone_name: str) -> str:
    if not draft.get("title"):
        return "What should I remind you about?"
    if not draft.get("due_date"):
        return "When would you like me to remind you? You can give a date such as tomorrow or next Monday."
    if draft.get("ambiguous_time"):
        return f"Do you mean AM or PM for that time in {timezone_name}?"
    if draft.get("invalid_time"):
        return "I couldn't parse that time. Please provide a valid time, such as 8:45 PM."
    if not draft.get("due_time"):
        return f"Would you like a specific time, or should I make it an all-day reminder for that date ({timezone_name})?"
    return "I need one more detail before I can prepare that reminder."


def _notification_guidance(runtime, *, proposed: bool = False) -> str:
    """Describe notification availability without promising delivery on unknown status."""
    try:
        status = runtime.get_notification_agent_status()
    except Exception:
        status = {"installed": None, "running": None}
    installed, running = status.get("installed"), status.get("running")
    if installed is False:
        message = "Windows notifications require the local UNBOUND notification agent to be installed."
    elif installed is True and running is False:
        message = "The notification agent is installed but stopped; notifications require it to be running."
    elif installed is True and running is True:
        message = "The running UNBOUND notification agent can send Windows notifications while the browser and FastAPI are closed, while Windows is running and you are logged in."
    else:
        message = "I can't verify the notification agent status, so I can't promise a Windows notification."
    if proposed:
        return "Once you confirm it, " + message[0].lower() + message[1:]
    return message


def _response_for_proposal(runtime, draft: dict[str, object], timezone_name: str) -> str:
    try:
        reminder = runtime.reminder_service.propose(
            title=str(draft["title"]), due_date=draft["due_date"], due_time=draft["due_time"], timezone=timezone_name,
        )
    except ValueError as exc:
        draft["ambiguous_time"] = True
        return f"I can't resolve that local time safely. {exc}. Please choose a different time in {timezone_name}."
    runtime.set_reminder_draft(str(draft["conversation_key"]), None)
    clock = reminder.due_time.strftime("%I:%M %p").lstrip("0") if reminder.due_time else "all day"
    due = f"{reminder.due_date.isoformat()} {clock} {reminder.timezone}"
    title = reminder.title
    draft["awaiting"] = "confirmation"
    draft["reminder_id"] = str(reminder.reminder_id)
    runtime.set_reminder_draft(str(draft["conversation_key"]), draft)
    return (
        f"I saved a reminder proposal in UNBOUND: {title} — {due}. It is not active until you confirm it. "
        f"To activate it, say: confirm reminder for {title}. {_notification_guidance(runtime, proposed=True)}"
    )


def handle_reminder_conversation(
    runtime,
    message: str,
    *,
    timezone_name: str,
    conversation_key: str | None,
) -> str | None:
    """Handle explicit reminder commands; return None for ordinary conversation/task text."""
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        timezone_name = ""
    if not timezone_name:
        timezone_name = runtime.user_timezone
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return "Please configure a valid IANA time zone before creating local reminders."

    command = parse_reminder_command(message, timezone_name)
    draft = runtime.get_reminder_draft(conversation_key) if conversation_key else None
    is_local_setup_phrase = bool(re.search(r"\b(?:set\s+it\s+up\s+)?(?:on\s+my\s+laptop|in\s+unbound|locally)\b", message, re.I))
    if draft and draft.get("awaiting") == "confirmation" and re.fullmatch(r"\s*(?:yes|confirm|approve|do it|activate it)\s*[.!]?\s*", message, re.I):
        try:
            reminder = runtime.reminder_service.confirm(str(draft["reminder_id"]))
        except (ReminderAmbiguousError, ReminderNotFoundError):
            return "I couldn't confirm that reminder proposal. Please ask me to list reminders and confirm it by title."
        runtime.set_reminder_draft(conversation_key, None)
        return f"Reminder saved and active in UNBOUND: {reminder.title}. {_notification_guidance(runtime)}"
    if command is None and draft and conversation_key:
        awaiting = draft.get("awaiting")
        if is_local_setup_phrase:
            if awaiting == "confirmation":
                return f"This reminder is saved as a proposal in UNBOUND, but it is not active until you confirm it. {_notification_guidance(runtime, proposed=True)}"
            return _clarification(draft, timezone_name)
        if awaiting == "confirmation":
            return f"That reminder proposal is saved in UNBOUND but is not active yet. Say “yes” to confirm it, or “confirm reminder for <title>”. {_notification_guidance(runtime, proposed=True)}"
        meridian_match = re.fullmatch(
            r"\s*(a\.?\s*m\.?|p\.?\s*m\.?|ante\s+meridiem|post\s+meridian)\s*[.!]?\s*",
            message,
            re.I,
        )
        if meridian_match and draft.get("ambiguous_time") and isinstance(draft.get("due_time"), time):
            meridian = re.sub(r"[.\s]", "", meridian_match.group(1).casefold())
            is_pm = meridian.startswith("p") or meridian == "postmeridiem"
            previous_time = draft["due_time"]
            resolved_hour = previous_time.hour % 12 + (12 if is_pm else 0)
            draft["due_time"] = time(resolved_hour, previous_time.minute)
            draft["ambiguous_time"] = False
        parsed_date = _relative_date(message, timezone_name)
        if parsed_date:
            draft["due_date"] = parsed_date
        time_marker = _CLOCK_MARKER.search(message)
        if time_marker:
            time_input = time_marker.group(0)
            if not re.match(r"\s*at\b", time_input, re.I):
                time_input = f"at {time_input}"
        else:
            time_input = message if re.search(r"\bat\b", message, re.I) else f"at {message}"
        parsed_time, ambiguous = _parse_time(time_input)
        if parsed_time:
            draft["due_time"] = parsed_time
            draft["ambiguous_time"] = ambiguous
            draft["invalid_time"] = False
        elif _has_clock_marker(message):
            # An invalid replacement must not leave stale time data eligible
            # for proposal creation.
            draft["due_time"] = None
            draft["ambiguous_time"] = False
            draft["invalid_time"] = True
        elif ambiguous:
            draft["ambiguous_time"] = True
        elif re.search(r"\b(?:all[- ]day|whole day|for the day)\b", message, re.I):
            draft["all_day"] = True
            draft["due_time"] = None
            draft["ambiguous_time"] = False
            draft["invalid_time"] = False
        if awaiting == "title":
            title_reply = _extract_title_reply(message)
            if title_reply:
                draft["title"] = title_reply
                draft["awaiting"] = ""
        draft["conversation_key"] = conversation_key
        if draft.get("title") and draft.get("due_date") and (draft.get("due_time") or draft.get("all_day")):
            return _response_for_proposal(runtime, draft, timezone_name)
        if draft.get("title") and draft.get("due_date"):
            draft["awaiting"] = "time"
        elif draft.get("title"):
            draft["awaiting"] = "date"
        elif draft.get("due_date"):
            draft["awaiting"] = "title"
        runtime.set_reminder_draft(conversation_key, draft)
        return _clarification(draft, timezone_name)
    if command is None:
        return None

    if command.action == ReminderAction.CREATE:
        draft = {
            "title": command.title,
            "due_date": command.due_date,
            "due_time": command.due_time,
            "ambiguous_time": command.ambiguous_time,
            "invalid_time": command.invalid_time,
            "all_day": command.all_day,
            "awaiting": "",
            "conversation_key": conversation_key or "",
        }
        if not draft["title"]:
            draft["awaiting"] = "title"
        elif not draft["due_date"]:
            draft["awaiting"] = "date"
        elif draft["ambiguous_time"]:
            draft["awaiting"] = "time"
        elif not draft["due_time"] and not draft["all_day"]:
            draft["awaiting"] = "time"
        if draft["awaiting"]:
            if conversation_key:
                runtime.set_reminder_draft(conversation_key, draft)
            return _clarification(draft, timezone_name)
        return _response_for_proposal(runtime, draft, timezone_name)

    service = runtime.reminder_service
    if command.action == ReminderAction.LIST:
        records = service.list()
        if not records:
            return "You don't have any reminders yet."
        result = "Your reminders: " + "; ".join(
            f"{item.title} — {item.due_date.isoformat()} {item.due_time or '(time not set)'} {item.timezone} [{item.status.value}]"
            for item in records
        )
        if any(item.status.value == "active" for item in records):
            result += ". " + _notification_guidance(runtime)
        return result
    if not command.title:
        return "Which reminder do you mean? Please include part of its title."
    try:
        if command.action == ReminderAction.CONFIRM:
            reminder = service.confirm_by_title(command.title)
            return f"Reminder saved and active in UNBOUND: {reminder.title}. {_notification_guidance(runtime)}"
        if command.action == ReminderAction.COMPLETE:
            reminder = service.complete_by_title(command.title)
            return f"Marked reminder completed: {reminder.title}."
        if command.action == ReminderAction.DELETE:
            reminder = service.delete_by_title(command.title)
            return f"Deleted reminder: {reminder.title}."
    except ReminderAmbiguousError:
        return "I found more than one matching reminder. Which title should I use?"
    except ReminderNotFoundError:
        return f"I couldn't find a matching reminder for “{command.title}”. You can ask me to list your reminders."
    return None
