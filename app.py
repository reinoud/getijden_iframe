import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request


RWS_BASE_URL = "https://ddapi20-waterwebservices.rijkswaterstaat.nl"
WATERDATA_INFO_URL = "https://rijkswaterstaatdata.nl/waterdata/"
DEFAULT_LOCATION_CODE = os.getenv("RWS_LOCATION_CODE", "dordrecht.oudemaas.benedenmerwede")
TIMEZONE = ZoneInfo("Europe/Amsterdam")
LOCATION_CACHE_TTL = timedelta(hours=6)


@dataclass(frozen=True)
class TidePoint:
    timestamp: datetime
    value_cm: float
    source: str


_location_cache: List[Dict[str, str]] = []
_location_cache_expires_at: datetime | None = None


def _parse_date(raw: str | None) -> date:
    if not raw:
        return datetime.now(TIMEZONE).date()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return datetime.now(TIMEZONE).date()


def _period_for_day(day: date) -> Tuple[str, str]:
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=TIMEZONE)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.isoformat(timespec="milliseconds"),
        end_local.isoformat(timespec="milliseconds"),
    )


def _post_json(path: str, payload: Dict) -> Dict:
    url = f"{RWS_BASE_URL}{path}"
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=30) as response:
        return json.loads(response.read())


def _fetch_observations(location_code: str, day: date, process_type: str) -> List[TidePoint]:
    start, end = _period_for_day(day)
    payload = {
        "AquoPlusWaarnemingMetadata": {
            "AquoMetadata": {
                "Compartiment": {"Code": "OW"},
                "Grootheid": {"Code": "WATHTE"},
                "Eenheid": {"Code": "cm"},
                "Hoedanigheid": {"Code": "NAP"},
                "ProcesType": process_type,
            }
        },
        "Locatie": {"Code": location_code},
        "Periode": {
            "Begindatumtijd": start,
            "Einddatumtijd": end,
        },
    }

    response = _post_json("/ONLINEWAARNEMINGENSERVICES/OphalenWaarnemingen", payload)
    rows: List[TidePoint] = []

    for observation in response.get("WaarnemingenLijst", []):
        for measurement in observation.get("MetingenLijst", []):
            measured = measurement.get("Meetwaarde", {})
            value = measured.get("Waarde_Numeriek")
            timestamp_str = measurement.get("Tijdstip")
            if value is None or not timestamp_str:
                continue
            ts = datetime.fromisoformat(timestamp_str).astimezone(TIMEZONE)
            if ts.date() == day:
                rows.append(TidePoint(timestamp=ts, value_cm=float(value), source=process_type))

    rows.sort(key=lambda p: p.timestamp)
    return rows


def _merge_points(day: date, location_code: str) -> List[TidePoint]:
    merged: Dict[datetime, TidePoint] = {}
    for process_type in ("verwachting", "meting", "astronomisch"):
        try:
            points = _fetch_observations(location_code, day, process_type)
        except (HTTPError, URLError, TimeoutError):
            continue

        for point in points:
            if point.timestamp not in merged:
                merged[point.timestamp] = point

    return sorted(merged.values(), key=lambda p: p.timestamp)


def _moving_average(values: List[float], window_size: int = 5) -> List[float]:
    if len(values) < window_size:
        return values[:]

    radius = window_size // 2
    smoothed: List[float] = []
    for idx in range(len(values)):
        lo = max(0, idx - radius)
        hi = min(len(values), idx + radius + 1)
        window = values[lo:hi]
        smoothed.append(sum(window) / len(window))
    return smoothed


def _find_high_low(points: List[TidePoint]) -> Tuple[List[TidePoint], List[TidePoint]]:
    if len(points) < 3:
        return ([], [])

    values = [p.value_cm for p in points]
    smooth = _moving_average(values)
    high_indices, low_indices = _local_extrema_indices(points, smooth)
    highs = _select_extrema(points, high_indices, kind="high", count=2)
    lows = _select_extrema(points, low_indices, kind="low", count=2)
    return (highs, lows)


def _serialize_points(points: List[TidePoint]) -> List[Dict]:
    return [
        {
            "timestamp": p.timestamp.isoformat(),
            "time": p.timestamp.strftime("%H:%M"),
            "value_cm": round(p.value_cm, 1),
            "source": p.source,
        }
        for p in points
    ]


def _date_options(anchor: date, span_days: int = 7) -> List[str]:
    return [(anchor + timedelta(days=delta)).isoformat() for delta in range(-span_days, span_days + 1)]


def _fetch_locations() -> List[Dict[str, str]]:
    payload = {"CatalogusFilter": {}}
    response = _post_json("/METADATASERVICES/OphalenCatalogus", payload)

    seen: set[str] = set()
    locations: List[Dict[str, str]] = []
    for item in response.get("LocatieLijst", []):
        code = (item.get("Code") or "").strip()
        name = (item.get("Naam") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        locations.append({"code": code, "name": name})

    locations.sort(key=lambda loc: (loc["name"].lower() or loc["code"].lower(), loc["code"].lower()))
    return locations


def _get_locations_cached() -> List[Dict[str, str]]:
    global _location_cache, _location_cache_expires_at

    now = datetime.now(TIMEZONE)
    if _location_cache and _location_cache_expires_at and now < _location_cache_expires_at:
        return _location_cache

    try:
        _location_cache = _fetch_locations()
        _location_cache_expires_at = now + LOCATION_CACHE_TTL
    except Exception:
        if not _location_cache:
            _location_cache = [{"code": DEFAULT_LOCATION_CODE, "name": DEFAULT_LOCATION_CODE}]
            _location_cache_expires_at = now + timedelta(minutes=10)

    return _location_cache


def _local_extrema_indices(points: List[TidePoint], smooth: List[float]) -> Tuple[List[int], List[int]]:
    high_indices: List[int] = []
    low_indices: List[int] = []

    for idx in range(1, len(points) - 1):
        prev_v = smooth[idx - 1]
        cur_v = smooth[idx]
        next_v = smooth[idx + 1]
        if cur_v >= prev_v and cur_v > next_v:
            high_indices.append(idx)
        if cur_v <= prev_v and cur_v < next_v:
            low_indices.append(idx)

    return (high_indices, low_indices)


def _select_extrema(points: List[TidePoint], candidate_indices: List[int], *, kind: str, count: int) -> List[TidePoint]:
    selected: List[TidePoint] = []
    selected_indices: set[int] = set()
    min_gap_seconds = timedelta(hours=4).total_seconds()

    def _can_add(candidate: TidePoint) -> bool:
        return all(abs((candidate.timestamp - item.timestamp).total_seconds()) >= min_gap_seconds for item in selected)

    def _pick(indices: List[int]) -> None:
        for idx in indices:
            candidate = points[idx]
            if not _can_add(candidate):
                continue
            selected.append(candidate)
            selected_indices.add(idx)
            if len(selected) >= count:
                return

    if kind == "high":
        ranked_candidates = sorted(candidate_indices, key=lambda idx: points[idx].value_cm, reverse=True)
        fallback_pool = sorted(range(1, len(points) - 1), key=lambda idx: points[idx].value_cm, reverse=True)
    else:
        ranked_candidates = sorted(candidate_indices, key=lambda idx: points[idx].value_cm)
        fallback_pool = sorted(range(1, len(points) - 1), key=lambda idx: points[idx].value_cm)

    _pick(ranked_candidates)
    if len(selected) < count:
        _pick([idx for idx in fallback_pool if idx not in selected_indices])

    selected.sort(key=lambda point: point.timestamp)
    return selected[:count]


app = Flask(__name__)


@app.route("/")
def index():
    selected_day = _parse_date(request.args.get("date"))
    selected_location = request.args.get("location", DEFAULT_LOCATION_CODE)
    return render_template(
        "index.html",
        selected_date=selected_day.isoformat(),
        date_options=_date_options(datetime.now(TIMEZONE).date()),
        default_location=DEFAULT_LOCATION_CODE,
        selected_location=selected_location,
        waterdata_info_url=WATERDATA_INFO_URL,
        swagger_url="https://ddapi20-waterwebservices.rijkswaterstaat.nl/swagger-ui/index.html",
    )


@app.route("/api/locations")
def api_locations():
    query = (request.args.get("q") or "").strip().lower()
    try:
        limit = max(1, min(200, int(request.args.get("limit", "50"))))
    except ValueError:
        limit = 50

    locations = _get_locations_cached()
    if query:
        filtered = [
            loc
            for loc in locations
            if query in loc["code"].lower() or query in loc["name"].lower()
        ]
    else:
        filtered = locations

    return jsonify({"count": len(filtered), "items": filtered[:limit]})


@app.route("/api/tides")
def api_tides():
    selected_day = _parse_date(request.args.get("date"))
    location = request.args.get("location", DEFAULT_LOCATION_CODE)

    try:
        points = _merge_points(selected_day, location)
        highs, lows = _find_high_low(points)
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": str(exc)}), 502

    return jsonify(
        {
            "date": selected_day.isoformat(),
            "location": location,
            "point_count": len(points),
            "points": _serialize_points(points),
            "high_waters": _serialize_points(highs),
            "low_waters": _serialize_points(lows),
            "sources_checked": ["verwachting", "meting", "astronomisch"],
        }
    )


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "time": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
            "default_location": DEFAULT_LOCATION_CODE,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


