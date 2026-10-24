"""Import of user-uploaded FIT activity files (e.g. exported from Garmin Connect).

Each file becomes one workout EventRecord under the INTERNAL provider with
source ``fit_import``: summary values from the FIT ``session`` message, laps
and HR/power zones from :func:`parse_fit_file`, per-second samples when
``INGEST_WORKOUT_SAMPLES`` is enabled. A file whose start time lies within
``DUPLICATE_WINDOW`` of an existing workout of the same user is skipped, so
re-uploads and activities that also arrive via a provider are not doubled.
"""

import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

import fitdecode
from sqlalchemy import select

from app.config import settings
from app.database import DbSession
from app.models import DataSource, EventRecord
from app.schemas.enums import ProviderName
from app.schemas.enums.workout_types import WorkoutType
from app.schemas.model_crud.activities import EventRecordCreate, EventRecordDetailCreate
from app.services.event_record_service import event_record_service
from app.services.fit_parser import parse_fit_file
from app.services.timeseries_service import timeseries_service

logger = logging.getLogger(__name__)

FIT_IMPORT_SOURCE = "fit_import"
DUPLICATE_WINDOW = timedelta(seconds=60)

ImportStatus = Literal["imported", "duplicate", "invalid"]


@dataclass
class FitSummary:
    workout_type: str
    start: datetime
    elapsed_seconds: int
    timer_seconds: int | None
    distance_m: Decimal | None
    avg_hr: Decimal | None
    max_hr: int | None
    calories: Decimal | None
    ascent_m: Decimal | None
    avg_speed: Decimal | None
    max_speed: Decimal | None
    avg_cadence: Decimal | None
    avg_power: Decimal | None
    max_power: Decimal | None
    device: str | None


@dataclass
class FitImportResult:
    status: ImportStatus
    record_id: UUID | None = None
    start: datetime | None = None
    reason: str | None = None


# (FIT sport, FIT sub_sport) → unified workout type; sport alone as fallback
_SUB_SPORT_TYPES: dict[tuple[str, str], WorkoutType] = {
    ("running", "trail"): WorkoutType.TRAIL_RUNNING,
    ("running", "treadmill"): WorkoutType.TREADMILL,
    ("cycling", "mountain"): WorkoutType.MOUNTAIN_BIKING,
    ("cycling", "indoor_cycling"): WorkoutType.INDOOR_CYCLING,
    ("cycling", "virtual_activity"): WorkoutType.INDOOR_CYCLING,
    ("cycling", "cyclocross"): WorkoutType.CYCLOCROSS,
    ("swimming", "lap_swimming"): WorkoutType.POOL_SWIMMING,
    ("swimming", "open_water"): WorkoutType.OPEN_WATER_SWIMMING,
    ("training", "strength_training"): WorkoutType.STRENGTH_TRAINING,
    ("training", "yoga"): WorkoutType.YOGA,
    ("training", "pilates"): WorkoutType.PILATES,
    ("fitness_equipment", "elliptical"): WorkoutType.ELLIPTICAL,
    ("fitness_equipment", "indoor_rowing"): WorkoutType.ROWING_MACHINE,
    ("fitness_equipment", "stair_climbing"): WorkoutType.STAIR_CLIMBING,
}
_SPORT_TYPES: dict[str, WorkoutType] = {
    "training": WorkoutType.CARDIO_TRAINING,
}
_KNOWN_TYPES = {t.value for t in WorkoutType}


def _workout_type(sport: Any, sub_sport: Any) -> str:
    sport_s = str(sport) if sport is not None else ""
    sub_s = str(sub_sport) if sub_sport is not None else ""
    if (mapped := _SUB_SPORT_TYPES.get((sport_s, sub_s))) is not None:
        return mapped.value
    if (mapped := _SPORT_TYPES.get(sport_s)) is not None:
        return mapped.value
    return sport_s if sport_s in _KNOWN_TYPES else WorkoutType.OTHER.value


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(round(float(value), 3)))
    except (TypeError, ValueError):
        return None


def _get(frame: fitdecode.FitDataMessage, *names: str) -> Any:
    for name in names:
        if frame.has_field(name):
            value = frame.get_value(name)
            if value is not None:
                return value
    return None


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _device_name(file_id: dict[str, Any], device_info: dict[str, Any]) -> str | None:
    """Readable device name: product_name if the file carries one, else the manufacturer."""
    for source in (device_info, file_id):
        name = source.get("product_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    manufacturer = file_id.get("manufacturer")
    if isinstance(manufacturer, str) and manufacturer not in ("development", "unknown"):
        return manufacturer.replace("_", " ").title()
    return None


def parse_fit_summary(data: bytes) -> FitSummary | None:
    """Summary of the first ``session`` message, or None if the file has none."""
    session: fitdecode.FitDataMessage | None = None
    file_id: dict[str, Any] = {}
    device_info: dict[str, Any] = {}

    with fitdecode.FitReader(io.BytesIO(data)) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "file_id" and not file_id:
                file_id = {f.name: f.value for f in frame.fields}
            elif frame.name == "device_info" and not device_info:
                # device_index 0 / "creator" is the recording device itself
                if _get(frame, "device_index") in (0, "creator"):
                    device_info = {f.name: f.value for f in frame.fields}
            elif frame.name == "session" and session is None:
                session = frame

    if session is None:
        return None
    start = _as_utc(_get(session, "start_time"))
    elapsed = _get(session, "total_elapsed_time")
    if start is None or elapsed is None:
        return None

    timer = _get(session, "total_timer_time")
    max_hr = _get(session, "max_heart_rate")
    return FitSummary(
        workout_type=_workout_type(_get(session, "sport"), _get(session, "sub_sport")),
        start=start,
        elapsed_seconds=int(round(float(elapsed))),
        timer_seconds=int(round(float(timer))) if timer is not None else None,
        distance_m=_dec(_get(session, "total_distance")),
        avg_hr=_dec(_get(session, "avg_heart_rate")),
        max_hr=int(max_hr) if max_hr is not None else None,
        calories=_dec(_get(session, "total_calories")),
        ascent_m=_dec(_get(session, "total_ascent")),
        avg_speed=_dec(_get(session, "enhanced_avg_speed", "avg_speed")),
        max_speed=_dec(_get(session, "enhanced_max_speed", "max_speed")),
        avg_cadence=_dec(_get(session, "avg_running_cadence", "avg_cadence")),
        avg_power=_dec(_get(session, "avg_power")),
        max_power=_dec(_get(session, "max_power")),
        device=_device_name(file_id, device_info),
    )


def _has_workout_near(db: DbSession, user_id: UUID, start: datetime) -> bool:
    naive = start.astimezone(timezone.utc).replace(tzinfo=None)
    stmt = (
        select(EventRecord.id)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .where(
            DataSource.user_id == user_id,
            EventRecord.category == "workout",
            EventRecord.start_datetime >= naive - DUPLICATE_WINDOW,
            EventRecord.start_datetime <= naive + DUPLICATE_WINDOW,
        )
        .limit(1)
    )
    return db.execute(stmt).first() is not None


def import_fit_file(db: DbSession, user_id: UUID, data: bytes) -> FitImportResult:
    """Parse one FIT file and store it as a workout of ``user_id``."""
    try:
        summary = parse_fit_summary(data)
    except Exception as exc:  # fitdecode raises various errors on broken files
        logger.info("FIT import: unreadable file for user %s: %s", user_id, exc)
        return FitImportResult(status="invalid", reason="unreadable")
    if summary is None:
        return FitImportResult(status="invalid", reason="no_session")

    if _has_workout_near(db, user_id, summary.start):
        return FitImportResult(status="duplicate", start=summary.start)

    record_id = uuid4()
    end = summary.start + timedelta(seconds=summary.elapsed_seconds)
    record = event_record_service.create(
        db,
        EventRecordCreate(
            id=record_id,
            category="workout",
            type=summary.workout_type,
            source_name="FIT-Import",
            device_model=summary.device,
            duration_seconds=summary.elapsed_seconds,
            start_datetime=summary.start,
            end_datetime=end,
            source=FIT_IMPORT_SOURCE,
            provider=ProviderName.INTERNAL.value,
            user_id=user_id,
        ),
    )

    parsed = parse_fit_file(data, user_id, record.data_source_id, source=FIT_IMPORT_SOURCE)
    event_record_service.create_detail(
        db,
        EventRecordDetailCreate(
            record_id=record.id,
            heart_rate_avg=summary.avg_hr,
            heart_rate_max=summary.max_hr,
            energy_burned=summary.calories,
            distance=summary.distance_m,
            moving_time_seconds=summary.timer_seconds,
            total_elevation_gain=summary.ascent_m,
            average_speed=summary.avg_speed,
            max_speed=summary.max_speed,
            average_cadence=summary.avg_cadence,
            average_watts=summary.avg_power,
            max_watts=summary.max_power,
            segments=parsed.segments or None,
            hr_zones=parsed.hr_zones,
            power_zones=parsed.power_zones,
        ),
    )

    if settings.ingest_workout_samples and parsed.samples:
        timeseries_service.bulk_create_samples(db, parsed.samples)
        db.commit()

    return FitImportResult(status="imported", record_id=record.id, start=summary.start)
