from __future__ import annotations

import calendar
import re
import unicodedata
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tm import ExternalResource, ResourceAbsence

logger = structlog.get_logger(__name__)

# ── Italian public holidays (fixed + Easter-based approximation) ──────────────

def _italian_holidays(year: int, month: int) -> set[date]:
    """Return the set of Italian public holidays in a given month/year."""
    try:
        fixed = {
            (1, 1),   # Capodanno
            (1, 6),   # Epifania
            (4, 25),  # Liberazione
            (5, 1),   # Festa del Lavoro
            (6, 2),   # Festa della Repubblica
            (8, 15),  # Ferragosto
            (11, 1),  # Ognissanti
            (12, 8),  # Immacolata
            (12, 25), # Natale
            (12, 26), # Santo Stefano
        }
        # Easter Sunday (Anonymous Gregorian algorithm)
        a = year % 19
        b = year // 100
        c = year % 100
        d = b // 4
        e = b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i = c // 4
        k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        easter_month = (h + l - 7 * m + 114) // 31
        easter_day = ((h + l - 7 * m + 114) % 31) + 1
        easter = date(year, easter_month, easter_day)
        easter_monday = easter + timedelta(days=1)

        result: set[date] = set()
        for m_fixed, d_fixed in fixed:
            if m_fixed == month:
                result.add(date(year, m_fixed, d_fixed))
        if easter.month == month:
            result.add(easter)
        if easter_monday.month == month:
            result.add(easter_monday)
        return result
    except Exception:
        logger.exception("_italian_holidays failed")
        raise


def _working_days_in_month(year: int, month: int) -> list[date]:
    """Return all working days (Mon–Fri, excluding Italian holidays) in the month."""
    try:
        holidays = _italian_holidays(year, month)
        _, n_days = calendar.monthrange(year, month)
        return [
            date(year, month, d)
            for d in range(1, n_days + 1)
            if date(year, month, d).weekday() < 5  # Mon=0 … Fri=4
            and date(year, month, d) not in holidays
        ]
    except Exception:
        logger.exception("_working_days_in_month failed")
        raise


# ── ExternalResource CRUD ─────────────────────────────────────────────────────

async def list_resources(db: AsyncSession, include_inactive: bool = False) -> list[ExternalResource]:
    try:
        q = select(ExternalResource)
        if not include_inactive:
            q = q.where(ExternalResource.is_active.is_(True))
        q = q.order_by(ExternalResource.last_name, ExternalResource.first_name)
        result = await db.execute(q)
        _query_result_for_logging = list(result.scalars().all())
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("list_resources query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("list_resources failed")
        raise


async def get_resource(db: AsyncSession, resource_id: uuid.UUID) -> ExternalResource | None:
    try:
        result = await db.execute(
            select(ExternalResource).where(ExternalResource.id == resource_id)
        )
        _query_result_for_logging = result.scalar_one_or_none()
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("get_resource query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_resource failed")
        raise


async def get_resource_by_email(db: AsyncSession, email: str) -> ExternalResource | None:
    try:
        result = await db.execute(
            select(ExternalResource).where(ExternalResource.email == email)
        )
        _query_result_for_logging = result.scalar_one_or_none()
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("get_resource_by_email query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("get_resource_by_email failed")
        raise


async def create_resource(db: AsyncSession, data: dict) -> ExternalResource:
    try:
        resource = ExternalResource(**data, id=uuid.uuid4())
        db.add(resource)
        await db.commit()
        await db.refresh(resource)
        return resource
    except Exception:
        logger.exception("create_resource failed")
        raise


async def update_resource(db: AsyncSession, resource: ExternalResource, data: dict) -> ExternalResource:
    try:
        for k, v in data.items():
            setattr(resource, k, v)
        resource.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(resource)
        return resource
    except Exception:
        logger.exception("update_resource failed")
        raise


# ── ResourceAbsence CRUD ──────────────────────────────────────────────────────

async def list_absences(
    db: AsyncSession,
    resource_id: uuid.UUID | None = None,
    year: int | None = None,
    month: int | None = None,
) -> list[ResourceAbsence]:
    try:
        q = select(ResourceAbsence)
        if resource_id:
            q = q.where(ResourceAbsence.resource_id == resource_id)
        if year and month:
            first = date(year, month, 1)
            _, last_day = calendar.monthrange(year, month)
            last = date(year, month, last_day)
            q = q.where(ResourceAbsence.absence_date.between(first, last))
        q = q.order_by(ResourceAbsence.absence_date)
        result = await db.execute(q)
        _query_result_for_logging = list(result.scalars().all())
        if _query_result_for_logging is None or not _query_result_for_logging:
            logger.info("list_absences query succeeded with 0 results")
        return _query_result_for_logging
    except Exception:
        logger.exception("list_absences failed")
        raise


async def create_absence(db: AsyncSession, data: dict) -> ResourceAbsence:
    try:
        absence = ResourceAbsence(**data, id=uuid.uuid4())
        db.add(absence)
        await db.commit()
        await db.refresh(absence)
        return absence
    except Exception:
        logger.exception("create_absence failed")
        raise


async def delete_absence(db: AsyncSession, absence_id: uuid.UUID) -> None:
    try:
        result = await db.execute(delete(ResourceAbsence).where(ResourceAbsence.id == absence_id))
        if result.rowcount == 0:
            logger.info("delete_absence query succeeded with 0 affected rows")
        await db.commit()
    except Exception:
        logger.exception("delete_absence failed")
        raise


async def delete_all_absences(db: AsyncSession) -> int:
    """Remove every absence, whatever its source. Returns how many rows went."""
    try:
        result = await db.execute(delete(ResourceAbsence))
        await db.commit()
        count = int(getattr(result, "rowcount", 0) or 0)
        if count == 0:
            logger.info("delete_all_absences query succeeded with 0 affected rows")
        return count
    except Exception:
        logger.exception("delete_all_absences failed")
        raise


async def import_absences_rows(db: AsyncSession, rows: list[dict]) -> dict:
    """Import absences from parsed rows: email, absence_date, absence_type, note."""
    try:
        resources = await list_resources(db, include_inactive=True)
        by_email = {r.email.lower().strip(): r for r in resources}

        imported = 0
        skipped = 0
        errors: list[str] = []

        for idx, row in enumerate(rows, start=1):
            email = str(row.get("email", "")).lower().strip()
            absence_date = row.get("absence_date")
            absence_type = str(row.get("absence_type") or "ferie").lower().strip()
            note = row.get("note")

            resource = by_email.get(email)
            if not resource:
                skipped += 1
                errors.append(f"Row {idx}: email '{email}' not found in anagrafica")
                continue

            if not isinstance(absence_date, date):
                skipped += 1
                errors.append(f"Row {idx}: invalid absence_date")
                continue

            if absence_type not in {"ferie", "malattia", "permesso", "altro"}:
                absence_type = "altro"

            # Upsert by source=csv to keep imports idempotent.
            await db.execute(
                delete(ResourceAbsence).where(
                    ResourceAbsence.resource_id == resource.id,
                    ResourceAbsence.absence_date == absence_date,
                    ResourceAbsence.source == "csv",
                )
            )
            db.add(
                ResourceAbsence(
                    id=uuid.uuid4(),
                    resource_id=resource.id,
                    absence_date=absence_date,
                    absence_type=absence_type,
                    source="csv",
                    note=note,
                )
            )
            imported += 1

        await db.commit()
        if imported == 0:
            logger.info("import_absences_rows completed with 0 imported records")
        return {"imported": imported, "skipped": skipped, "errors": errors}
    except Exception:
        logger.exception("import_absences_rows failed")
        raise


# ── Confluence Team Calendars sync ────────────────────────────────────────────
#
# The Events and Leaves sub-calendars of the QDP space calendar are absences; the
# custom ones are role rotations (e.g. "NRT Review Approver") and are never read.
# The sync is a snapshot over the whole window (previous, current and next year):
# the Confluence-sourced absences in the window are replaced by what the calendar
# shows now; manual and CSV absences are never touched.

TM_CALENDAR_SPACE = "QDP"
_CALENDAR_API = "/wiki/rest/calendar-services/1.0/calendar"
_ROME = ZoneInfo("Europe/Rome")


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    try:
        _, last_day = calendar.monthrange(year, month)
        return date(year, month, 1), date(year, month, last_day)
    except Exception:
        logger.exception("_month_bounds failed")
        raise


def sync_window(today: date | None = None) -> tuple[date, date]:
    """Days the Confluence sync covers: 1 Jan of last year → 31 Dec of next year."""
    try:
        year = (today or date.today()).year
        return date(year - 1, 1, 1), date(year + 1, 12, 31)
    except Exception:
        logger.exception("sync_window failed")
        raise


def _holidays_between(first: date, last: date) -> set[date]:
    try:
        days: set[date] = set()
        y, m = first.year, first.month
        while (y, m) <= (last.year, last.month):
            days |= _italian_holidays(y, m)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return days
    except Exception:
        logger.exception("_holidays_between failed")
        raise


def _utc_window(year: int, month: int) -> tuple[str, str]:
    """Midnight Europe/Rome of the first day and of the day after the last, in UTC.

    Team Calendars rejects plain dates with BAD_START_DATETIME: it wants ISO datetimes.
    """
    try:
        return _utc_range(*_month_bounds(year, month))
    except Exception:
        logger.exception("_utc_window failed")
        raise


def _utc_range(first: date, last: date) -> tuple[str, str]:
    """Rome-midnight bounds of ``first``..``last`` (inclusive) as ISO UTC datetimes."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        midnight = datetime.min.time()
        start = datetime.combine(first, midnight, _ROME).astimezone(UTC)
        end = datetime.combine(last + timedelta(days=1), midnight, _ROME).astimezone(UTC)
        return start.strftime(fmt), end.strftime(fmt)
    except Exception:
        logger.exception("_utc_range failed")
        raise


def _event_days(event: dict[str, Any], first: date, last: date) -> list[date]:
    """Calendar days covered by an event, clipped to ``first``..``last``.

    All-day events have an exclusive end (a one-day leave on the 3rd ends on the 4th).
    """
    try:
        try:
            start = date.fromisoformat(str(event.get("start") or "")[:10])
            end_raw = str(event.get("end") or "")[:10]
            end = date.fromisoformat(end_raw) if end_raw else start
        except ValueError:
            logger.exception("_event_days failed")
            return []
        if event.get("allDay", True):
            end = max(start, end - timedelta(days=1))
        days = []
        current = max(start, first)
        while current <= min(end, last):
            days.append(current)
            current += timedelta(days=1)
        return days
    except Exception:
        logger.exception("_event_days failed")
        raise


def _event_absence(event: dict[str, Any]) -> tuple[str, str | None]:
    """Whole days are ferie, events with a time range are permessi (note = the hours)."""
    try:
        if event.get("allDay", True):
            return "ferie", None
        start, end = str(event.get("start") or ""), str(event.get("end") or "")
        hours = f"{start[11:16]}–{end[11:16]}" if len(start) >= 16 and len(end) >= 16 else None
        return "permesso", hours
    except Exception:
        logger.exception("_event_absence failed")
        raise


def _normalize_name(value: str) -> str:
    try:
        decomposed = unicodedata.normalize("NFKD", value)
        ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
        return " ".join(re.sub(r"[^a-z0-9 ]", " ", ascii_only.lower()).split())
    except Exception:
        logger.exception("_normalize_name failed")
        raise


def _resource_name_index(resources: list[ExternalResource]) -> dict[str, ExternalResource]:
    try:
        index: dict[str, ExternalResource] = {}
        for r in resources:
            first, last = _normalize_name(r.first_name), _normalize_name(r.last_name)
            index[f"{first} {last}"] = r
            index[f"{last} {first}"] = r
        return index
    except Exception:
        logger.exception("_resource_name_index failed")
        raise


# "other" is the Events sub-calendar, "leaves" the Leaves one; custom ones (PR Review
# Coordinator, NRT Review Approver, ...) are rotations, not absences.
_ABSENCE_CALENDAR_TYPES = {"leaves", "other"}


def _event_people(
    event: dict[str, Any], by_name: dict[str, ExternalResource]
) -> tuple[list[ExternalResource], list[str]]:
    """Resources an event is about, plus the names that matched no resource.

    Leaves carry the person as invitee; plain events (e.g. "Permesso - Daniele Quero")
    often have no invitees, so the name is looked up in the title instead.
    """
    try:
        found: list[ExternalResource] = []
        unmatched: list[str] = []
        invitees = event.get("invitees") or []
        for invitee in invitees:
            display = str(invitee.get("displayName") or invitee.get("name") or "").strip()
            resource = by_name.get(_normalize_name(display))
            if resource is not None:
                found.append(resource)
            elif display:
                unmatched.append(display)
        if invitees:
            return found, unmatched
        title = str(event.get("title") or "").strip()
        padded = f" {_normalize_name(title)} "
        seen: set[uuid.UUID] = set()
        for name, resource in by_name.items():
            if f" {name} " in padded and resource.id not in seen:
                seen.add(resource.id)
                found.append(resource)
        if not found and title:
            unmatched.append(title)
        return found, unmatched
    except Exception:
        logger.exception("_event_people failed")
        raise


async def _leave_calendars(
    client: httpx.AsyncClient, base: str, auth: tuple[str, str]
) -> list[dict[str, Any]]:
    """The Events and Leaves sub-calendars of the QDP space calendar(s)."""
    try:
        resp = await client.get(
            f"{base}{_CALENDAR_API}/subcalendars.json",
            auth=auth,
            params={"calendarContext": "spaceCalendars", "viewingSpaceKey": TM_CALENDAR_SPACE},
        )
        if resp.status_code in (401, 403):
            raise PermissionError(
                "L'utenza configurata non può leggere i calendari dello spazio "
                f"{TM_CALENDAR_SPACE} ({resp.status_code})."
            )
        resp.raise_for_status()
        found: list[dict[str, Any]] = []
        for item in (resp.json() or {}).get("payload") or []:
            parent = item.get("subCalendar") or {}
            children = [c.get("subCalendar") or {} for c in item.get("childSubCalendars") or []]
            for sub in [parent, *children]:
                if sub.get("type") in _ABSENCE_CALENDAR_TYPES and sub.get("id"):
                    name = sub.get("name") or parent.get("name") or sub["id"]
                    found.append({"id": sub["id"], "name": name})
        return found
    except Exception:
        logger.exception("_leave_calendars failed")
        raise


async def sync_from_confluence(
    db: AsyncSession,
    year: int | None = None,
    month: int | None = None,
) -> dict[str, Any]:
    """Snapshot of the QDP team calendar (events + leaves) into ``resource_absences``.

    Without ``year``/``month`` it covers the whole :func:`sync_window`. Whole-day
    entries become ferie, entries with a time range become permessi; a day with both
    counts as ferie.
    """
    try:
        if year is not None and month is not None:
            first, last = _month_bounds(year, month)
        else:
            first, last = sync_window()
        if not (settings.jira_api_token and settings.jira_base_url and settings.jira_email):
            return {
                "synced": 0,
                "errors": ["Credenziali Atlassian non configurate (JIRA_BASE_URL/EMAIL/API_TOKEN)."],
            }

        base = settings.jira_base_url.rstrip("/")
        auth = (settings.jira_email, settings.jira_api_token)
        # one request per calendar per year keeps each response a reasonable size
        chunks = [
            (max(first, date(y, 1, 1)), min(last, date(y, 12, 31)))
            for y in range(first.year, last.year + 1)
        ]

        events: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                calendars = await _leave_calendars(client, base, auth)
                if not calendars:
                    logger.info("sync_from_confluence calendars query succeeded with 0 results")
                    return {
                        "synced": 0,
                        "errors": [
                            "Nessun calendario Eventi/Ferie nello spazio "
                            f"{TM_CALENDAR_SPACE}: nulla da sincronizzare."
                        ],
                    }
                for cal in calendars:
                    for chunk_first, chunk_last in chunks:
                        start_utc, end_utc = _utc_range(chunk_first, chunk_last)
                        resp = await client.get(
                            f"{base}{_CALENDAR_API}/events.json",
                            auth=auth,
                            params={
                                "subCalendarId": cal["id"],
                                "userTimeZoneId": "Europe/Rome",
                                "start": start_utc,
                                "end": end_utc,
                            },
                        )
                        if resp.status_code != 200:
                            # never replace the period with a partial picture
                            return {
                                "synced": 0,
                                "errors": [
                                    f"Lettura del calendario '{cal['name']}' non riuscita "
                                    f"(HTTP {resp.status_code}): nessuna modifica applicata."
                                ],
                            }
                        payload = (resp.json() or {}).get("events") or []
                        events.extend(e for e in payload if isinstance(e, dict))
            if not events:
                logger.info("sync_from_confluence events query succeeded with 0 results")
        except PermissionError as exc:
            logger.exception("sync_from_confluence failed")
            return {"synced": 0, "errors": [str(exc)]}
        except httpx.HTTPError as exc:
            logger.exception("sync_from_confluence failed")
            return {"synced": 0, "errors": [f"Confluence non raggiungibile: {exc}"]}

        resources = await list_resources(db, include_inactive=False)
        by_name = _resource_name_index(resources)
        holidays = _holidays_between(first, last)

        wanted: dict[tuple[uuid.UUID, date], tuple[str, str, str | None]] = {}
        unmatched: set[str] = set()
        for event in events:
            days = [
                d for d in _event_days(event, first, last) if d.weekday() < 5 and d not in holidays
            ]
            if not days:
                continue
            absence_type, note = _event_absence(event)
            people, missing = _event_people(event, by_name)
            unmatched.update(missing)
            for resource in people:
                for d in days:
                    current = wanted.get((resource.id, d))
                    if current and current[0] == "ferie":
                        continue  # a whole day off already covers any permesso
                    wanted[(resource.id, d)] = (absence_type, str(event.get("id") or ""), note)

        await db.execute(
            delete(ResourceAbsence).where(
                ResourceAbsence.source == "confluence",
                ResourceAbsence.absence_date >= first,
                ResourceAbsence.absence_date <= last,
            )
        )
        for (resource_id, day), (absence_type, event_id, note) in wanted.items():
            db.add(
                ResourceAbsence(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    absence_date=day,
                    absence_type=absence_type,
                    source="confluence",
                    confluence_event_id=event_id or None,
                    note=note,
                )
            )
        await db.commit()

        errors: list[str] = []
        if unmatched:
            names = ", ".join(sorted(unmatched))
            errors.append(f"Persone nel calendario non presenti tra le risorse T&M attive: {names}")
        return {
            "synced": len(wanted),
            "errors": errors,
            "period_start": first.isoformat(),
            "period_end": last.isoformat(),
        }
    except Exception:
        logger.exception("sync_from_confluence failed")
        raise


# ── Cost report ───────────────────────────────────────────────────────────────

async def compute_cost_report(db: AsyncSession, year: int, month: int) -> dict:
    try:
        resources = await list_resources(db, include_inactive=False)
        working_days = _working_days_in_month(year, month)
        n_working = len(working_days)

        rows = []
        grand_total = 0.0

        for resource in resources:
            # Only include resource if contract covers any part of the month
            _, last_day = calendar.monthrange(year, month)
            month_start = date(year, month, 1)
            month_end = date(year, month, last_day)
            if resource.contract_start > month_end:
                continue
            if resource.contract_end and resource.contract_end < month_start:
                continue

            absences = await list_absences(db, resource_id=resource.id, year=year, month=month)
            absence_dates = {a.absence_date for a in absences}
            absence_days = len([d for d in working_days if d in absence_dates])
            billable = n_working - absence_days
            rate = float(resource.daily_rate)
            total = billable * rate
            grand_total += total

            rows.append({
                "resource_id": resource.id,
                "full_name": f"{resource.first_name} {resource.last_name}",
                "company": resource.company,
                "role": resource.role,
                "working_days": n_working,
                "absence_days": absence_days,
                "billable_days": billable,
                "daily_rate": rate,
                "total_cost": total,
            })

        return {
            "year": year,
            "month": month,
            "rows": rows,
            "grand_total": grand_total,
        }
    except Exception:
        logger.exception("compute_cost_report failed")
        raise
