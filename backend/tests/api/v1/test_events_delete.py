"""
Tests for event record DELETE endpoints.

Tests the DELETE /api/v1/users/{user_id}/events/workouts/{id}
and DELETE /api/v1/users/{user_id}/events/sleep/{id} endpoints including:
- Successful deletion with cascade (details removed)
- 404 for non-existent records
- 404 for records belonging to another user
- 404 for wrong category (e.g. deleting sleep via workouts endpoint)
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataPointSeries, EventRecord, SleepDetails, UserConnection, WorkoutDetails
from tests.factories import (
    ApiKeyFactory,
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    SleepDetailsFactory,
    UserConnectionFactory,
    UserFactory,
    WorkoutDetailsFactory,
)
from tests.utils import api_key_headers


class TestDeleteWorkout:
    """Test suite for DELETE /users/{user_id}/events/workouts/{workout_id}."""

    def test_delete_workout_success(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user)
        workout = EventRecordFactory(mapping=ds, category="workout", type_="running")
        WorkoutDetailsFactory(event_record=workout)
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{user.id}/events/workouts/{workout.id}",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 204
        assert db.get(EventRecord, workout.id) is None

    def test_delete_workout_cascades_details(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user)
        workout = EventRecordFactory(mapping=ds, category="workout", type_="cycling")
        detail = WorkoutDetailsFactory(event_record=workout)
        detail_record_id = detail.record_id
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{user.id}/events/workouts/{workout.id}",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 204
        assert db.get(EventRecord, workout.id) is None
        # Detail should be cascade-deleted
        assert db.get(WorkoutDetails, detail_record_id) is None

    def test_delete_workout_not_found(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{user.id}/events/workouts/{uuid4()}",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 404

    def test_delete_workout_wrong_user(self, client: TestClient, db: Session) -> None:
        owner = UserFactory()
        other_user = UserFactory()
        ds = DataSourceFactory(user=owner)
        workout = EventRecordFactory(mapping=ds, category="workout", type_="running")
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{other_user.id}/events/workouts/{workout.id}",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 404
        # Record should still exist
        assert db.get(EventRecord, workout.id) is not None

    def test_delete_workout_wrong_category(self, client: TestClient, db: Session) -> None:
        """Deleting a sleep record via workouts endpoint should return 404."""
        user = UserFactory()
        ds = DataSourceFactory(user=user)
        sleep = EventRecordFactory(mapping=ds, category="sleep", type="sleep")
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{user.id}/events/workouts/{sleep.id}",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 404
        assert db.get(EventRecord, sleep.id) is not None


class TestDeleteAllWorkouts:
    """Test suite for DELETE /users/{user_id}/events/workouts."""

    def test_deletes_workouts_and_in_workout_samples_only(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user)
        connection = UserConnectionFactory(user=user)
        start = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
        workout = EventRecordFactory(mapping=ds, category="workout", type_="running", start_datetime=start)
        detail = WorkoutDetailsFactory(event_record=workout)
        detail_record_id = detail.record_id
        other_ds = DataSourceFactory(user=user)
        second = EventRecordFactory(mapping=other_ds, category="workout", type_="cycling")
        sleep = EventRecordFactory(mapping=ds, category="sleep", type="sleep")
        inside = DataPointSeriesFactory(data_source=ds, recorded_at=start + timedelta(minutes=10))
        outside = DataPointSeriesFactory(data_source=ds, recorded_at=start - timedelta(hours=3))
        ids = {
            "workout": workout.id,
            "second": second.id,
            "sleep": sleep.id,
            "inside": inside.id,
            "outside": outside.id,
            "connection": connection.id,
        }
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{user.id}/events/workouts",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 200
        assert response.json() == {"deleted": 2}
        db.expire_all()
        assert db.get(EventRecord, ids["workout"]) is None
        assert db.get(EventRecord, ids["second"]) is None
        assert db.get(WorkoutDetails, detail_record_id) is None
        assert db.get(DataPointSeries, ids["inside"]) is None
        assert db.get(DataPointSeries, ids["outside"]) is not None
        assert db.get(EventRecord, ids["sleep"]) is not None
        assert db.get(UserConnection, ids["connection"]) is not None

    def test_leaves_other_users_untouched(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        other_user = UserFactory()
        other_workout = EventRecordFactory(
            mapping=DataSourceFactory(user=other_user), category="workout", type_="running"
        )
        other_workout_id = other_workout.id
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{user.id}/events/workouts",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 200
        assert response.json() == {"deleted": 0}
        db.expire_all()
        assert db.get(EventRecord, other_workout_id) is not None


class TestDeleteSleepSession:
    """Test suite for DELETE /users/{user_id}/events/sleep/{sleep_id}."""

    def test_delete_sleep_success(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user)
        sleep = EventRecordFactory(mapping=ds, category="sleep", type="sleep")
        SleepDetailsFactory(event_record=sleep)
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{user.id}/events/sleep/{sleep.id}",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 204
        assert db.get(EventRecord, sleep.id) is None

    def test_delete_sleep_cascades_details(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user)
        sleep = EventRecordFactory(mapping=ds, category="sleep", type="sleep")
        detail = SleepDetailsFactory(event_record=sleep)
        detail_record_id = detail.record_id
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{user.id}/events/sleep/{sleep.id}",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 204
        assert db.get(SleepDetails, detail_record_id) is None

    def test_delete_sleep_not_found(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{user.id}/events/sleep/{uuid4()}",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 404

    def test_delete_sleep_wrong_user(self, client: TestClient, db: Session) -> None:
        owner = UserFactory()
        other_user = UserFactory()
        ds = DataSourceFactory(user=owner)
        sleep = EventRecordFactory(mapping=ds, category="sleep", type="sleep")
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{other_user.id}/events/sleep/{sleep.id}",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 404
        assert db.get(EventRecord, sleep.id) is not None

    def test_delete_sleep_wrong_category(self, client: TestClient, db: Session) -> None:
        """Deleting a workout record via sleep endpoint should return 404."""
        user = UserFactory()
        ds = DataSourceFactory(user=user)
        workout = EventRecordFactory(mapping=ds, category="workout", type_="running")
        api_key = ApiKeyFactory()

        response = client.delete(
            f"/api/v1/users/{user.id}/events/sleep/{workout.id}",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 404
        assert db.get(EventRecord, workout.id) is not None
