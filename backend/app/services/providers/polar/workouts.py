from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID, uuid4

import isodate
from sqlalchemy import func

from app.config import settings
from app.constants.workout_types.polar import get_unified_workout_type
from app.database import DbSession
from app.models import DataPointSeries, EventRecord
from app.schemas.enums import SeriesType, get_series_type_id
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordMetrics,
    TimeSeriesSampleCreate,
)
from app.schemas.providers.polar import ExerciseJSON as PolarExerciseJSON
from app.services.event_record_service import event_record_service
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate
from app.services.timeseries_service import timeseries_service
from app.utils.dates import offset_to_iso
from app.utils.sentry_helpers import log_and_capture_error


class PolarWorkouts(BaseWorkoutsTemplate):
    """Polar implementation of workouts template."""

    def get_workouts(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[Any]:
        """Get exercises from Polar API."""
        return self._make_api_request(db, user_id, "/v3/exercises")

    def get_workouts_from_api(self, db: DbSession, user_id: UUID, **kwargs: Any) -> Any:
        """Get exercises from Polar API with options."""
        samples = kwargs.get("samples", False)
        zones = kwargs.get("zones", False)
        route = kwargs.get("route", False)

        params = {
            "samples": str(samples).lower(),
            "zones": str(zones).lower(),
            "route": str(route).lower(),
        }
        return self._make_api_request(db, user_id, "/v3/exercises", params=params)

    def get_workout_detail_from_api(self, db: DbSession, user_id: UUID, workout_id: str, **kwargs: Any) -> Any:
        """Get detailed exercise data from Polar API."""
        samples = kwargs.get("samples", False)
        zones = kwargs.get("zones", False)
        route = kwargs.get("route", False)
        return self.get_exercise_detail(db, user_id, workout_id, samples, zones, route)

    def _extract_dates(self, start_timestamp: Any, end_timestamp: Any) -> tuple[datetime, datetime]:
        """Extract start and end dates from timestamps.

        Note: Polar uses a different format with offset, so this delegates to _extract_dates_with_offset.
        This is required by the base template but not used directly.
        """
        raise NotImplementedError("Use _extract_dates_with_offset for Polar workouts")

    def _extract_dates_with_offset(
        self,
        start_time: str,
        start_time_utc_offset: int,
        duration: str,
    ) -> tuple[datetime, datetime]:
        """Extract start and end dates from timestamps with UTC offset."""
        start_date = isodate.parse_datetime(start_time)
        offset = timedelta(minutes=start_time_utc_offset)
        start_date = start_date + offset
        duration_td = isodate.parse_duration(duration)
        end_date = start_date + duration_td
        return start_date, end_date

    def _build_metrics(self, raw_workout: PolarExerciseJSON) -> EventRecordMetrics:
        hr_avg = (
            Decimal(str(raw_workout.heart_rate.average))
            if raw_workout.heart_rate and raw_workout.heart_rate.average is not None
            else None
        )
        hr_max = (
            Decimal(str(raw_workout.heart_rate.maximum))
            if raw_workout.heart_rate and raw_workout.heart_rate.maximum is not None
            else None
        )

        energy_burned = Decimal(str(raw_workout.calories)) if raw_workout.calories is not None else None

        distance = Decimal(str(raw_workout.distance)) if raw_workout.distance is not None else None

        return {
            "heart_rate_max": int(hr_max) if hr_max is not None else None,
            "heart_rate_avg": hr_avg,
            "energy_burned": energy_burned,
            "distance": distance,
        }

    def _normalize_workout(
        self,
        raw_workout: PolarExerciseJSON,
        user_id: UUID,
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate]:
        """Normalize Polar exercise to EventRecordCreate and EventRecordDetailCreate."""
        workout_id = uuid4()

        workout_type = get_unified_workout_type(raw_workout.sport, raw_workout.detailed_sport_info)

        start_date, end_date = self._extract_dates_with_offset(
            raw_workout.start_time,
            raw_workout.start_time_utc_offset,
            raw_workout.duration,
        )
        duration_seconds = int((end_date - start_date).total_seconds())

        metrics = self._build_metrics(raw_workout)

        # convert from offset minutes to seconds first
        zone_offset = offset_to_iso(raw_workout.start_time_utc_offset * 60)

        record = EventRecordCreate(
            category="workout",
            type=workout_type.value,
            source_name=raw_workout.device,
            device_model=raw_workout.device,
            duration_seconds=duration_seconds,
            start_datetime=start_date,
            end_datetime=end_date,
            zone_offset=zone_offset,
            id=workout_id,
            external_id=raw_workout.id,
            source="polar",
            user_id=user_id,
        )

        detail = EventRecordDetailCreate(
            record_id=workout_id,
            **metrics,
        )

        return record, detail

    def _build_bundles(
        self,
        raw: list[PolarExerciseJSON],
        user_id: UUID,
    ) -> Iterable[tuple[EventRecordCreate, EventRecordDetailCreate]]:
        """Build event record payloads for Polar exercises."""
        for raw_workout in raw:
            yield self._normalize_workout(raw_workout, user_id)

    def load_data(
        self,
        db: DbSession,
        user_id: UUID,
        **kwargs: Any,
    ) -> int:
        """Load data from Polar API."""
        workouts_data = self.get_workouts_from_api(db, user_id, **kwargs)
        workouts = [PolarExerciseJSON(**w) for w in workouts_data]

        # Polar's list endpoint ignores the sync window and returns the full
        # exercise history; the per-exercise route fetch below must not follow
        # suit, or one sync would hammer the detail endpoint for every GPS
        # workout ever recorded. Restrict it to the requested window (the
        # historical sync task passes 90 days by default).
        window_start = self._parse_window_bound(kwargs.get("start_date"))

        count = 0
        for raw_workout in workouts:
            record, detail = self._normalize_workout(raw_workout, user_id)
            created_record = event_record_service.create(db, record)
            detail_for_record = detail.model_copy(update={"record_id": created_record.id})
            event_record_service.create_detail(db, detail_for_record)
            count += 1
            if window_start is None or created_record.start_datetime >= window_start:
                self._ingest_workout_route(db, user_id, raw_workout, created_record)

        return count

    @staticmethod
    def _parse_window_bound(value: Any) -> datetime | None:
        """Sync window bound as naive UTC (matches how exercise records store datetimes)."""
        if not value:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = isodate.parse_datetime(str(value).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @staticmethod
    def _workout_has_track(db: DbSession, record: EventRecord) -> bool:
        """True when the workout's data source already holds latitude samples.

        The per-exercise route fetch below runs on every sync (Polar's list
        endpoint returns the full exercise history each time), so this check is
        what keeps API calls and upsert traffic bounded to the first sync.
        """
        latitude_id = get_series_type_id(SeriesType.latitude)
        return (
            db.query(func.count(DataPointSeries.id))
            .filter(
                DataPointSeries.data_source_id == record.data_source_id,
                DataPointSeries.series_type_definition_id == latitude_id,
                DataPointSeries.recorded_at >= record.start_datetime,
                DataPointSeries.recorded_at < record.end_datetime,
            )
            .scalar()
            or 0
        ) > 0

    def _ingest_workout_route(
        self,
        db: DbSession,
        user_id: UUID,
        raw_exercise: PolarExerciseJSON,
        record: EventRecord,
    ) -> int:
        """Fetch the exercise route (GPS) on demand and persist it as samples.

        The list endpoint only carries aggregates; the route arrives via the
        exercise detail endpoint with ``route=true``. Flag-gated and
        failure-isolated like the Strava stream ingest.
        """
        if not settings.ingest_workout_samples:
            return 0
        if raw_exercise.has_route is False:
            return 0
        if self._workout_has_track(db, record):
            return 0

        try:
            raw = self.get_exercise_detail(db, user_id, raw_exercise.id, samples=False, zones=False, route=True)
            exercise = PolarExerciseJSON(**raw)
        except Exception as exc:
            log_and_capture_error(
                exc,
                self.logger,
                "Failed to fetch Polar exercise route, skipping samples",
                extra={"exercise_id": raw_exercise.id},
            )
            return 0

        samples = self._build_route_samples(exercise, user_id, record)
        if not samples:
            return 0
        # Same savepoint+commit dance as Strava's stream ingest: the sync task
        # never commits its session, so without the explicit commit the route
        # rows silently vanish when the session closes.
        nested = db.begin_nested()
        try:
            timeseries_service.bulk_create_samples(db, samples)
            nested.commit()
            db.commit()
            return len(samples)
        except Exception as exc:
            nested.rollback()
            log_and_capture_error(
                exc,
                self.logger,
                "Polar route sample ingestion failed; continuing",
                extra={"exercise_id": raw_exercise.id, "sample_count": len(samples)},
            )
            return 0

    def _build_route_samples(
        self,
        exercise: PolarExerciseJSON,
        user_id: UUID,
        record: EventRecord,
    ) -> list[TimeSeriesSampleCreate]:
        """Turn Polar route points into latitude/longitude sample rows.

        ``device_model`` must match the workout record's so both resolve to the
        same data source -- the samples endpoint joins on data_source_id, a
        different source would hide the track from /workouts/{id}/samples.
        """
        route = exercise.route or []
        points = [p for p in route if p.latitude is not None and p.longitude is not None]
        if not points:
            return []

        # Polar encodes route point times as ISO 8601 durations relative to the
        # start. Tolerate numbers (plain seconds) and, if no point carries a
        # parseable time at all, spread the points evenly across the workout
        # duration -- the map only needs the geometry, and dropping the whole
        # track over missing offsets would be worse.
        def _offset_seconds(time_value: Any) -> float | None:
            if time_value is None:
                return None
            if isinstance(time_value, (int, float)):
                return float(time_value)
            try:
                return isodate.parse_duration(str(time_value)).total_seconds()
            except Exception:
                return None

        offsets = [_offset_seconds(p.time) for p in points]
        if all(offset is None for offset in offsets):
            span = max((record.end_datetime - record.start_datetime).total_seconds(), 1.0)
            offsets = [span * i / max(len(points) - 1, 1) for i in range(len(points))]

        zone_offset = record.zone_offset
        samples: list[TimeSeriesSampleCreate] = []
        for point, offset in zip(points, offsets):
            if offset is None:
                continue
            recorded_at = record.start_datetime + timedelta(seconds=offset)
            for value, series_type in (
                (point.latitude, SeriesType.latitude),
                (point.longitude, SeriesType.longitude),
            ):
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        source="polar",
                        device_model=exercise.device,
                        recorded_at=recorded_at,
                        zone_offset=zone_offset,
                        value=Decimal(str(value)),
                        series_type=series_type,
                    )
                )
        return samples

    def fetch_and_save_exercise(self, db: DbSession, user_id: UUID, path: str) -> int:
        """Fetch a single exercise by URL path and save it. Used by webhook handler."""
        raw = self._make_api_request(db, user_id, path)
        if not raw:
            return 0
        count = 0
        for record, detail in self._build_bundles([PolarExerciseJSON(**raw)], user_id):
            created = event_record_service.create(db, record)
            event_record_service.create_detail(db, detail.model_copy(update={"record_id": created.id}))
            self._ingest_workout_route(db, user_id, PolarExerciseJSON(**raw), created)
            count += 1
        return count

    def get_exercise_detail(
        self,
        db: DbSession,
        user_id: UUID,
        exercise_id: str,
        samples: bool = False,
        zones: bool = False,
        route: bool = False,
    ) -> dict:
        """Get detailed exercise data from Polar API."""
        params = {
            "samples": str(samples).lower(),
            "zones": str(zones).lower(),
            "route": str(route).lower(),
        }
        return self._make_api_request(db, user_id, f"/v3/exercises/{exercise_id}", params=params)
