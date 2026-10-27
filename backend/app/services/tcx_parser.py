"""Parser for TCX activity files (Garmin Training Center XML, e.g. Polar Flow exports).

Produces the same shapes as the FIT import: a :class:`FitSummary` for the
workout record and a :class:`FitParseResult` with per-second samples and lap
segments, so :func:`app.services.fit_import_service.import_activity_file`
stores both formats identically.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.schemas.enums.series_types import SeriesType
from app.schemas.enums.workout_types import WorkoutType
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.services.fit_parser import FitParseResult

_NS = {
    "tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2",
    "ext": "http://www.garmin.com/xmlschemas/ActivityExtension/v2",
}
_SPORT_TYPES = {
    "running": WorkoutType.RUNNING,
    "biking": WorkoutType.CYCLING,
}


def is_tcx(data: bytes) -> bool:
    return b"TrainingCenterDatabase" in data[:2048]


def _float(el: ET.Element | None, path: str) -> float | None:
    if el is None:
        return None
    node = el.find(path, _NS)
    if node is None or node.text is None:
        return None
    try:
        return float(node.text)
    except ValueError:
        return None


def _time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dec(value: float | None) -> Decimal | None:
    return Decimal(str(round(value, 3))) if value is not None else None


def parse_tcx(data: bytes, user_id: UUID, source: str):
    """Summary + samples of the first activity, or None if the file has none.

    Returns ``(FitSummary, FitParseResult)``. Samples carry no data source;
    the caller assigns provider and device like for FIT.
    """
    from app.services.fit_import_service import FitSummary

    # No DTDs: rules out entity expansion tricks in user-uploaded XML
    if b"<!DOCTYPE" in data[:4096].upper():
        raise ValueError("doctype_not_allowed")
    root = ET.fromstring(data)
    activity = root.find("tcx:Activities/tcx:Activity", _NS)
    if activity is None:
        return None
    laps = activity.findall("tcx:Lap", _NS)
    start = _time(activity.findtext("tcx:Id", namespaces=_NS)) or (
        _time(laps[0].get("StartTime")) if laps else None
    )
    if start is None:
        return None

    result = FitParseResult()
    hr_values: list[float] = []
    cadences: list[float] = []
    powers: list[float] = []
    ascent = 0.0
    last_alt: float | None = None
    last_point: tuple[datetime, float] | None = None
    last_ts = start

    def add(ts: datetime, series: SeriesType, value: float) -> None:
        result.samples.append(
            TimeSeriesSampleCreate(
                id=uuid4(),
                user_id=user_id,
                source=source,
                recorded_at=ts,
                zone_offset=None,
                value=Decimal(str(round(value, 6))),
                series_type=series,
            )
        )

    for lap_index, lap in enumerate(laps):
        lap_seconds = _float(lap, "tcx:TotalTimeSeconds")
        segment = {
            "kind": "lap",
            "index": lap_index,
            "start_time": lap.get("StartTime"),
            "elapsed_seconds": round(lap_seconds, 2) if lap_seconds is not None else None,
            "distance_meters": _float(lap, "tcx:DistanceMeters"),
            "avg_heart_rate": _float(lap, "tcx:AverageHeartRateBpm/tcx:Value"),
            "max_heart_rate": _float(lap, "tcx:MaximumHeartRateBpm/tcx:Value"),
            "max_speed": _float(lap, "tcx:MaximumSpeed"),
        }
        if segment["elapsed_seconds"] is not None:
            result.segments.append({k: v for k, v in segment.items() if v is not None})

        for point in lap.iterfind("tcx:Track/tcx:Trackpoint", _NS):
            ts = _time(point.findtext("tcx:Time", namespaces=_NS))
            if ts is None:
                continue
            last_ts = max(last_ts, ts)
            if (hr := _float(point, "tcx:HeartRateBpm/tcx:Value")) is not None:
                add(ts, SeriesType.heart_rate, hr)
                hr_values.append(hr)
            if (lat := _float(point, "tcx:Position/tcx:LatitudeDegrees")) is not None:
                add(ts, SeriesType.latitude, lat)
            if (lon := _float(point, "tcx:Position/tcx:LongitudeDegrees")) is not None:
                add(ts, SeriesType.longitude, lon)
            if (alt := _float(point, "tcx:AltitudeMeters")) is not None:
                add(ts, SeriesType.elevation, alt)
                if last_alt is not None and alt > last_alt:
                    ascent += alt - last_alt
                last_alt = alt
            cadence = _float(point, "tcx:Cadence")
            if cadence is None:
                cadence = _float(point, "tcx:Extensions/ext:TPX/ext:RunCadence")
            if cadence is not None:
                add(ts, SeriesType.cadence, cadence)
                cadences.append(cadence)
            if (watts := _float(point, "tcx:Extensions/ext:TPX/ext:Watts")) is not None:
                add(ts, SeriesType.power, watts)
                powers.append(watts)
            speed = _float(point, "tcx:Extensions/ext:TPX/ext:Speed")
            distance = _float(point, "tcx:DistanceMeters")
            if speed is None and distance is not None and last_point is not None:
                dt = (ts - last_point[0]).total_seconds()
                if dt > 0:
                    speed = max(distance - last_point[1], 0.0) / dt
            if distance is not None:
                last_point = (ts, distance)
            if speed is not None:
                add(ts, SeriesType.speed, speed)

    timer = sum(_float(lap, "tcx:TotalTimeSeconds") or 0.0 for lap in laps)
    distance_m = sum(_float(lap, "tcx:DistanceMeters") or 0.0 for lap in laps)
    calories = sum(_float(lap, "tcx:Calories") or 0.0 for lap in laps)
    max_speed = max((_float(lap, "tcx:MaximumSpeed") or 0.0 for lap in laps), default=0.0)
    elapsed = max((last_ts - start).total_seconds(), timer)
    sport = (activity.get("Sport") or "").lower()

    summary = FitSummary(
        workout_type=_SPORT_TYPES.get(sport, WorkoutType.OTHER).value,
        start=start,
        elapsed_seconds=int(round(elapsed)),
        timer_seconds=int(round(timer)) if timer else None,
        distance_m=_dec(distance_m or None),
        avg_hr=_dec(sum(hr_values) / len(hr_values)) if hr_values else None,
        max_hr=int(max(hr_values)) if hr_values else None,
        calories=_dec(calories or None),
        ascent_m=_dec(ascent or None),
        avg_speed=_dec(distance_m / timer) if distance_m and timer else None,
        max_speed=_dec(max_speed or None),
        avg_cadence=_dec(sum(cadences) / len(cadences)) if cadences else None,
        avg_power=_dec(sum(powers) / len(powers)) if powers else None,
        max_power=_dec(max(powers)) if powers else None,
        device=(activity.findtext("tcx:Creator/tcx:Name", namespaces=_NS) or "").strip() or None,
    )
    return summary, result
