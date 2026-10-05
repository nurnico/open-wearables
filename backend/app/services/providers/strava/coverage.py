from app.schemas.enums import SeriesType
from app.schemas.enums.health_score_category import HealthScoreCategory

# (/api/v3/activities/{id}/streams)
# time not included since it only defines offset
STREAM_KEY_SERIES_TYPE: dict[str, SeriesType] = {
    "heartrate": SeriesType.heart_rate,
    "velocity_smooth": SeriesType.speed,
    "cadence": SeriesType.cadence,
    "watts": SeriesType.power,
}

# GPS track: Strava's latlng stream carries [lat, lng] pairs, which map to TWO
# series types instead of one -- handled specially in workouts.py, which splits
# each pair into latitude + longitude rows.
STREAM_KEY_LATLNG = "latlng"

# Value for the Strava streams `keys` query param: time axis first, then every metric.
STREAM_KEYS_PARAM: str = ",".join(["time", *STREAM_KEY_SERIES_TYPE, STREAM_KEY_LATLNG])

# Workout-context only, gated by settings.ingest_workout_samples.
TIMESERIES: frozenset[SeriesType] = frozenset(
    [
        *STREAM_KEY_SERIES_TYPE.values(),  # /api/v3/activities/{id}/streams
        SeriesType.latitude,
        SeriesType.longitude,
    ]
)

# EventRecordDetail fields populated by workouts.py from workout records
# (/api/v3/athlete/activities + /api/v3/activities/{id}).
WORKOUT_FIELDS: frozenset[str] = frozenset(
    {
        "heart_rate_avg",
        "heart_rate_max",
        "distance",
        "average_speed",
        "max_speed",
        "average_watts",
        "max_watts",
        "total_elevation_gain",
        "elev_high",
        "elev_low",
        "energy_burned",
        "moving_time_seconds",
    }
)

SLEEP_FIELDS: frozenset[str] = frozenset()

HEALTH_SCORES: frozenset[HealthScoreCategory] = frozenset()
