from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord
from app.schemas.enums import ProviderName
from app.services.fit_import_service import FIT_IMPORT_SOURCE, import_fit_file, parse_fit_summary
from tests.factories import DataSourceFactory, EventRecordFactory, UserFactory
from tests.fixtures.fit_builder import make_running_fit, make_session_fit

START = datetime(2025, 6, 4, 6, 30, 0, tzinfo=timezone.utc)


class TestParseFitSummary:
    def test_session_values(self) -> None:
        summary = parse_fit_summary(make_session_fit(start=START))

        assert summary is not None
        assert summary.workout_type == "running"
        assert summary.start == START
        assert summary.elapsed_seconds == 1608
        assert summary.timer_seconds == 1400
        assert summary.distance_m == Decimal("4000.0")
        assert summary.avg_hr == Decimal("173.0")
        assert summary.max_hr == 192
        assert summary.ascent_m == Decimal("51.0")
        assert summary.device == "Garmin"

    def test_sub_sport_maps_to_unified_type(self) -> None:
        summary = parse_fit_summary(make_session_fit(sub_sport=3))  # running / trail

        assert summary is not None
        assert summary.workout_type == "trail_running"

    def test_unknown_sport_is_other(self) -> None:
        summary = parse_fit_summary(make_session_fit(sport=200))

        assert summary is not None
        assert summary.workout_type == "other"

    def test_file_without_session_returns_none(self) -> None:
        assert parse_fit_summary(make_running_fit()) is None


class TestImportFitFile:
    def test_creates_workout_with_moving_time(self, db: Session) -> None:
        user = UserFactory()

        result = import_fit_file(db, user.id, make_session_fit(start=START))

        assert result.status == "imported"
        record = db.get(EventRecord, result.record_id)
        assert record is not None
        assert record.type == "running"
        assert record.duration_seconds == 1608
        assert record.workout_detail is not None
        assert record.workout_detail.moving_time_seconds == 1400
        assert record.workout_detail.distance == Decimal("4000")
        source = db.get(DataSource, record.data_source_id)
        assert source is not None
        assert source.provider == ProviderName.INTERNAL
        assert source.source == FIT_IMPORT_SOURCE
        assert source.user_id == user.id

    def test_reupload_is_duplicate(self, db: Session) -> None:
        user = UserFactory()
        data = make_session_fit(start=START)

        first = import_fit_file(db, user.id, data)
        second = import_fit_file(db, user.id, data)

        assert first.status == "imported"
        assert second.status == "duplicate"

    def test_existing_provider_workout_is_duplicate(self, db: Session) -> None:
        user = UserFactory()
        data_source = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        naive_start = START.replace(tzinfo=None) + timedelta(seconds=30)
        EventRecordFactory(
            data_source=data_source,
            category="workout",
            type_="running",
            start_datetime=naive_start,
            end_datetime=naive_start + timedelta(minutes=27),
        )

        result = import_fit_file(db, user.id, make_session_fit(start=START))

        assert result.status == "duplicate"

    def test_other_users_workout_is_not_a_duplicate(self, db: Session) -> None:
        other = UserFactory()
        import_fit_file(db, other.id, make_session_fit(start=START))
        user = UserFactory()

        result = import_fit_file(db, user.id, make_session_fit(start=START))

        assert result.status == "imported"

    def test_garbage_is_invalid(self, db: Session) -> None:
        user = UserFactory()

        result = import_fit_file(db, user.id, b"not a fit file")

        assert result.status == "invalid"
