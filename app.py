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


@dataclass(frozen=True)
class TidePoint:
    timestamp: datetime
    value_cm: float
    source: str


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

    highs: List[TidePoint] = []
    lows: List[TidePoint] = []
    min_gap = timedelta(hours=4)

    for idx in range(1, len(points) - 1):
        prev_v = smooth[idx - 1]
        cur_v = smooth[idx]
        next_v = smooth[idx + 1]

        is_high = cur_v >= prev_v and cur_v > next_v
        is_low = cur_v <= prev_v and cur_v < next_v
        if not is_high and not is_low:
            continue

        candidate = points[idx]
        target = highs if is_high else lows

        if target and candidate.timestamp - target[-1].timestamp < min_gap:
            if is_high and candidate.value_cm > target[-1].value_cm:
                target[-1] = candidate
            if is_low and candidate.value_cm < target[-1].value_cm:
                target[-1] = candidate
            continue

        target.append(candidate)

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


app = Flask(__name__)


@app.route("/")
def index():
    selected_day = _parse_date(request.args.get("date"))
    return render_template(
        "index.html",
        selected_date=selected_day.isoformat(),
        date_options=_date_options(datetime.now(TIMEZONE).date()),
        default_location=DEFAULT_LOCATION_CODE,
        waterdata_info_url=WATERDATA_INFO_URL,
        swagger_url="https://ddapi20-waterwebservices.rijkswaterstaat.nl/swagger-ui/index.html",
    )


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


