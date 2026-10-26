"""Tests for Garmin strategy."""

from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.config import settings
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.providers.garmin.workouts import GarminWorkouts


class TestGarminStrategy:
    """Tests for GarminStrategy class."""

    def test_name_is_garmin(self) -> None:
        """Strategy name should be 'garmin'."""
        strategy = GarminStrategy()
        assert strategy.name == "garmin"

    def test_api_base_url(self) -> None:
        """API base URL should be Garmin's API endpoint."""
        strategy = GarminStrategy()
        assert strategy.api_base_url == "https://apis.garmin.com"

    def test_display_name(self) -> None:
        """Display name should be capitalized provider name."""
        strategy = GarminStrategy()
        assert strategy.display_name == "Garmin"

    def test_has_cloud_api(self) -> None:
        """Garmin should have cloud API support."""
        strategy = GarminStrategy()
        assert strategy.has_cloud_api is True

    def test_icon_url(self) -> None:
        """Icon URL should point to Garmin SVG icon."""
        strategy = GarminStrategy()
        assert strategy.icon_url == "/static/provider-icons/garmin.svg"

    def test_oauth_component_initialized(self) -> None:
        """OAuth component should be initialized."""
        strategy = GarminStrategy()
        assert strategy.oauth is not None
        assert isinstance(strategy.oauth, GarminOAuth)

    def test_workouts_component_initialized(self) -> None:
        """Workouts component should be initialized."""
        strategy = GarminStrategy()
        assert strategy.workouts is not None
        assert isinstance(strategy.workouts, GarminWorkouts)

    def test_oauth_has_correct_provider_name(self) -> None:
        """OAuth component should have correct provider name."""
        strategy = GarminStrategy()
        assert strategy.oauth is not None
        assert strategy.oauth.provider_name == "garmin"

    def test_oauth_has_correct_api_base_url(self) -> None:
        """OAuth component should have correct API base URL."""
        strategy = GarminStrategy()
        assert strategy.oauth is not None
        assert strategy.oauth.api_base_url == "https://apis.garmin.com"

    def test_workouts_has_correct_provider_name(self) -> None:
        """Workouts component should have correct provider name."""
        strategy = GarminStrategy()
        assert strategy.workouts is not None
        assert strategy.workouts.provider_name == "garmin"

    def test_workouts_has_correct_api_base_url(self) -> None:
        """Workouts component should have correct API base URL."""
        strategy = GarminStrategy()
        assert strategy.workouts is not None
        assert strategy.workouts.api_base_url == "https://apis.garmin.com"

    def test_repositories_initialized(self) -> None:
        """All required repositories should be initialized."""
        strategy = GarminStrategy()
        assert strategy.user_repo is not None
        assert strategy.connection_repo is not None
        assert strategy.workout_repo is not None

    def test_historical_sync_blocked_when_disabled(self) -> None:
        """Default (Starter Access) must not trigger the Garmin backfill."""
        strategy = GarminStrategy()
        with patch.object(settings, "garmin_backfill_enabled", False), pytest.raises(HTTPException) as exc_info:
            strategy.start_historical_sync(uuid4(), days=30)
        assert exc_info.value.status_code == 403

    def test_historical_sync_dispatches_when_enabled(self) -> None:
        """GARMIN_BACKFILL_ENABLED=true (Enterprise Access) dispatches the backfill."""
        strategy = GarminStrategy()
        with (
            patch.object(settings, "garmin_backfill_enabled", True),
            patch("app.services.providers.garmin.strategy.start_garmin_full_backfill") as task,
        ):
            result = strategy.start_historical_sync(uuid4(), days=30)
        task.delay.assert_called_once()
        assert result.method == "webhook_backfill"
