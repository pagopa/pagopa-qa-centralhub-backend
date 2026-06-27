from __future__ import annotations

import calendar
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tm import ExternalResource, ResourceAbsence

# ── Italian public holidays (fixed + Easter-based approximation) ──────────────

def _italian_holidays(year: int, month: int) -> set[date]:
    """Return the set of Italian public holidays in a given month/year."""
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
    easter_monday = date(year, easter.month, easter.day + 1) if easter.day < 28 else date(
        year, easter.month + (1 if easter.month < 12 else 0),
        (easter.day + 1) if easter.day < 28 else 1
    )

    result: set[date] = set()
    for m_fixed, d_fixed in fixed:
        if m_fixed == month:
            result.add(date(year, m_fixed, d_fixed))
    if easter.month == month:
        result.add(easter)
    if easter_monday.month == month:
        result.add(easter_monday)
    return result


def _working_days_in_month(year: int, month: int) -> list[date]:
    """Return all working days (Mon–Fri, excluding Italian holidays) in the month."""
    holidays = _italian_holidays(year, month)
    _, n_days = calendar.monthrange(year, month)
    return [
        date(year, month, d)
        for d in range(1, n_days + 1)
        if date(year, month, d).weekday() < 5  # Mon=0 … Fri=4
        and date(year, month, d) not in holidays
    ]


# ── ExternalResource CRUD ─────────────────────────────────────────────────────

async def list_resources(db: AsyncSession, include_inactive: bool = False) -> list[ExternalResource]:
    q = select(ExternalResource)
    if not include_inactive:
        q = q.where(ExternalResource.is_active.is_(True))
    q = q.order_by(ExternalResource.last_name, ExternalResource.first_name)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_resource(db: AsyncSession, resource_id: uuid.UUID) -> ExternalResource | None:
    result = await db.execute(
        select(ExternalResource).where(ExternalResource.id == resource_id)
    )
    return result.scalar_one_or_none()


async def get_resource_by_email(db: AsyncSession, email: str) -> ExternalResource | None:
    result = await db.execute(
        select(ExternalResource).where(ExternalResource.email == email)
    )
    return result.scalar_one_or_none()


async def create_resource(db: AsyncSession, data: dict) -> ExternalResource:
    resource = ExternalResource(**data, id=uuid.uuid4())
    db.add(resource)
    await db.commit()
    await db.refresh(resource)
    return resource


async def update_resource(db: AsyncSession, resource: ExternalResource, data: dict) -> ExternalResource:
    for k, v in data.items():
        setattr(resource, k, v)
    resource.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(resource)
    return resource


# ── ResourceAbsence CRUD ──────────────────────────────────────────────────────

async def list_absences(
    db: AsyncSession,
    resource_id: uuid.UUID | None = None,
    year: int | None = None,
    month: int | None = None,
) -> list[ResourceAbsence]:
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
    return list(result.scalars().all())


async def create_absence(db: AsyncSession, data: dict) -> ResourceAbsence:
    absence = ResourceAbsence(**data, id=uuid.uuid4())
    db.add(absence)
    await db.commit()
    await db.refresh(absence)
    return absence


async def delete_absence(db: AsyncSession, absence_id: uuid.UUID) -> None:
    await db.execute(delete(ResourceAbsence).where(ResourceAbsence.id == absence_id))
    await db.commit()


async def import_absences_rows(db: AsyncSession, rows: list[dict]) -> dict:
    """Import absences from parsed rows: email, absence_date, absence_type, note."""
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
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ── Confluence Team Calendars sync ────────────────────────────────────────────

async def sync_from_confluence(
    db: AsyncSession,
    year: int,
    month: int,
) -> dict:
    """Fetch absence events from Confluence Team Calendars for QDP space and upsert."""
    if not settings.jira_api_token or not settings.jira_base_url:
        return {"synced": 0, "errors": ["Confluence credentials not configured"]}

    # Confluence base is typically the same Atlassian domain as Jira
    confluence_base = settings.jira_base_url.rstrip("/")
    _, last_day = calendar.monthrange(year, month)
    start_str = f"{year}-{month:02d}-01"
    end_str = f"{year}-{month:02d}-{last_day:02d}"

    auth = (settings.jira_email, settings.jira_api_token)
    synced = 0
    errors: list[str] = []

    resources = await list_resources(db, include_inactive=False)
    resources_by_email = {r.email.lower().strip(): r for r in resources}
    resources_by_name = {f"{r.first_name} {r.last_name}".lower().strip(): r for r in resources}

    event_endpoints = [
        f"{confluence_base}/wiki/rest/calendar-services/1.0/calendar/events.json",
        f"{confluence_base}/wiki/rest/calendar-services/1.0/events.json",
        f"{confluence_base}/rest/calendar-services/1.0/calendar/events.json",
        f"{confluence_base}/rest/calendar-services/1.0/events.json",
    ]
    calendar_discovery_endpoints = [
        f"{confluence_base}/wiki/rest/calendar-services/1.0/calendar/subcalendars.json",
        f"{confluence_base}/wiki/rest/calendar-services/1.0/calendar/list.json",
        f"{confluence_base}/wiki/rest/calendar-services/1.0/subcalendars.json",
        f"{confluence_base}/rest/calendar-services/1.0/calendar/subcalendars.json",
        f"{confluence_base}/rest/calendar-services/1.0/calendar/list.json",
        f"{confluence_base}/rest/calendar-services/1.0/subcalendars.json",
    ]

    def _extract_events(payload: Any) -> list[dict]:
        if isinstance(payload, dict):
            nested = payload.get("payload")
            if nested is not None:
                nested_events = _extract_events(nested)
                if nested_events:
                    return nested_events
            if isinstance(payload.get("events"), list):
                return [e for e in payload["events"] if isinstance(e, dict)]
            for key in ("results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [e for e in value if isinstance(e, dict)]
        if isinstance(payload, list):
            return [e for e in payload if isinstance(e, dict)]
        return []

    def _extract_calendar_ids(payload: Any) -> list[str]:
        ids: set[str] = set()
        candidates: list[Any] = []
        if isinstance(payload, dict):
            nested = payload.get("payload")
            if nested is not None:
                for cid in _extract_calendar_ids(nested):
                    ids.add(cid)
            for key in ("subCalendars", "subcalendars", "calendars", "results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    candidates.extend(value)
            if payload.get("id") is not None:
                ids.add(str(payload["id"]))
        elif isinstance(payload, list):
            candidates.extend(payload)

        for item in candidates:
            if not isinstance(item, dict):
                continue
            for key in ("id", "subCalendarId", "subcalendarId", "calendarId"):
                if item.get(key) is not None:
                    ids.add(str(item[key]))
            sub = item.get("subCalendar")
            if isinstance(sub, dict) and sub.get("id") is not None:
                ids.add(str(sub["id"]))
        return list(ids)

    def _calendar_api_error(resp: httpx.Response) -> str | None:
        if resp.status_code != 500:
            return None
        body = resp.text or ""
        if "BAD_START_DATETIME" in body:
            match = re.search(r"events of\s+(.+?)\s+\(([0-9a-fA-F\-]{8,})\)", body)
            if match:
                cal_name = match.group(1).strip()
                return (
                    f"Confluence calendar '{cal_name}' contains invalid event date/time (BAD_START_DATETIME). "
                    "Fix malformed events in Team Calendars, then retry sync."
                )
            return (
                "Confluence Team Calendars returned BAD_START_DATETIME (invalid event date/time in calendar data). "
                "Fix malformed events, then retry sync."
            )
        return None

    events_by_id: dict[str, dict] = {}
    discovered_calendar_ids: set[str] = set()
    last_exc: Exception | None = None

    async with httpx.AsyncClient(timeout=30) as client:
        # Preflight: verify Confluence space visibility with current credentials.
        try:
            space_check = await client.get(
                f"{confluence_base}/wiki/rest/api/space/QDP",
                auth=auth,
            )
            if space_check.status_code in (401, 403):
                return {
                    "synced": 0,
                    "errors": [
                        "Configured account has no access to Confluence space QDP (401/403).",
                    ],
                }
            if space_check.status_code == 404:
                return {
                    "synced": 0,
                    "errors": [
                        "Confluence space QDP not found or not visible to configured account.",
                    ],
                }
            space_check.raise_for_status()
        except Exception as exc:
            return {
                "synced": 0,
                "errors": [f"Cannot reach Confluence space API: {exc}"],
            }

        # 1) Try direct fetch by space key (works on some tenants)
        for endpoint in event_endpoints:
            try:
                r = await client.get(
                    endpoint,
                    auth=auth,
                    params={
                        "spaceKey": "QDP",
                        "start": start_str,
                        "end": end_str,
                        "userTimeZoneId": "Europe/Rome",
                    },
                )
                if r.status_code in (401, 403):
                    errors.append(
                        f"Permission denied on Team Calendars endpoint: {endpoint}"
                    )
                    continue
                known = _calendar_api_error(r)
                if known:
                    errors.append(known)
                    continue
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                for evt in _extract_events(r.json()):
                    eid = str(evt.get("id") or evt.get("eventId") or f"evt-{len(events_by_id)}")
                    events_by_id[eid] = evt
            except Exception as exc:  # pragma: no cover
                last_exc = exc

        # 2) If no events, discover calendars from /wiki/spaces/QDP/calendars context and fetch by calendar id
        if not events_by_id:
            for endpoint in calendar_discovery_endpoints:
                try:
                    r = await client.get(
                        endpoint,
                        auth=auth,
                        params={"spaceKey": "QDP"},
                    )
                    if r.status_code in (401, 403):
                        errors.append(
                            f"Permission denied on calendar discovery endpoint: {endpoint}"
                        )
                        continue
                    if r.status_code == 404:
                        continue
                    r.raise_for_status()
                    for cid in _extract_calendar_ids(r.json()):
                        discovered_calendar_ids.add(cid)
                except Exception as exc:  # pragma: no cover
                    last_exc = exc

            for cid in discovered_calendar_ids:
                for endpoint in event_endpoints:
                    for id_param in ("subCalendarId", "calendarId"):
                        try:
                            r = await client.get(
                                endpoint,
                                auth=auth,
                                params={
                                    id_param: cid,
                                    "start": start_str,
                                    "end": end_str,
                                    "userTimeZoneId": "Europe/Rome",
                                },
                            )
                            if r.status_code in (401, 403):
                                errors.append(
                                    f"Permission denied on Team Calendars endpoint: {endpoint}"
                                )
                                continue
                            known = _calendar_api_error(r)
                            if known:
                                errors.append(known)
                                continue
                            if r.status_code == 404:
                                continue
                            r.raise_for_status()
                            for evt in _extract_events(r.json()):
                                eid = str(evt.get("id") or evt.get("eventId") or f"evt-{len(events_by_id)}")
                                events_by_id[eid] = evt
                        except Exception as exc:  # pragma: no cover
                            last_exc = exc

    events = list(events_by_id.values())
    if not events:
        if errors:
            # Keep diagnostics compact and deduplicated for UI readability.
            errors = list(dict.fromkeys(errors))
        if last_exc:
            errors.append(str(last_exc))
        errors.append(
            "Team Calendars API not reachable for QDP space with this account. Verify app availability and API permissions on Confluence Cloud tenant."
        )
        return {"synced": 0, "errors": errors}
    for event in events:
        # Team Calendar event structure
        invitees = event.get("invitees", [])
        raw_start = event.get("start") or event.get("allDay", {}).get("startDate") or event.get("allDay", {}).get("start") or ""
        raw_end = event.get("end") or event.get("allDay", {}).get("endDate") or event.get("allDay", {}).get("end") or raw_start
        event_id = str(event.get("id", ""))
        event_type = (event.get("className") or event.get("subCalendar", {}).get("type") or "ferie").lower()

        # Map Confluence event type to our absence_type vocabulary
        absence_type = "ferie"
        if "malattia" in event_type or "sick" in event_type:
            absence_type = "malattia"
        elif "permesso" in event_type or "leave" in event_type:
            absence_type = "permesso"

        # Parse dates (ISO date strings like "2026-06-15")
        try:
            d_start = date.fromisoformat(raw_start[:10])
            d_end = date.fromisoformat(raw_end[:10])
        except (ValueError, TypeError):
            errors.append(f"Cannot parse date for event {event_id}")
            continue

        for invitee in invitees:
            email = (
                invitee.get("email")
                or (invitee.get("person") or {}).get("email")
                or ""
            ).lower().strip()
            display_name = (
                invitee.get("displayName")
                or invitee.get("name")
                or (invitee.get("person") or {}).get("displayName")
                or ""
            ).lower().strip()

            resource = None
            if email and "@" in email:
                resource = resources_by_email.get(email)
            if not resource and display_name:
                resource = resources_by_name.get(display_name)
            if not resource:
                continue

            # Expand multi-day events into individual absence rows
            current = d_start
            while current <= d_end:
                if current.weekday() < 5 and current not in _italian_holidays(current.year, current.month):
                    # Upsert: delete existing confluence entry for same day+resource, then insert
                    await db.execute(
                        delete(ResourceAbsence).where(
                            ResourceAbsence.resource_id == resource.id,
                            ResourceAbsence.absence_date == current,
                            ResourceAbsence.source == "confluence",
                        )
                    )
                    absence = ResourceAbsence(
                        id=uuid.uuid4(),
                        resource_id=resource.id,
                        absence_date=current,
                        absence_type=absence_type,
                        source="confluence",
                        confluence_event_id=event_id,
                    )
                    db.add(absence)
                    synced += 1
                current = current + timedelta(days=1)

    await db.commit()
    return {"synced": synced, "errors": errors}


# ── Cost report ───────────────────────────────────────────────────────────────

async def compute_cost_report(db: AsyncSession, year: int, month: int) -> dict:
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
