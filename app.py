import calendar
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

import fitz


RWS_BASE_URL = "https://ddapi20-waterwebservices.rijkswaterstaat.nl"
WATERDATA_INFO_URL = "https://rijkswaterstaatdata.nl/waterdata/"
DEFAULT_LOCATION_CODE = os.getenv("RWS_LOCATION_CODE", "dordrecht.oudemaas.benedenmerwede")
TIMEZONE = ZoneInfo("Europe/Amsterdam")
LOCATION_CACHE_TTL = timedelta(hours=6)
MAX_LOCATION_CODE_LENGTH = 80
MAX_SEARCH_QUERY_LENGTH = 80
MIN_API_DAY_OFFSET = -31
MAX_API_DAY_OFFSET = 183
LOCATION_CODE_RE = re.compile(r"^[a-z0-9._-]+$", re.IGNORECASE)
STROOMATLAS_PDF_FILENAME = "Stroomatlas Dordrecht.pdf"
STROOMATLAS_MIN_OFFSET = -6
STROOMATLAS_MAX_OFFSET = 6
STROOMATLAS_PAGE_SHIFT = 7
STROOMATLAS_RENDER_SCALE = 1.5
STROOMATLAS_LEGEND_PAGE_INDEX = 0
STROOMATLAS_DORDRECHT_SHIFT_HOURS = -2

DUTCH_WEEKDAYS = [
    "maandag",
    "dinsdag",
    "woensdag",
    "donderdag",
    "vrijdag",
    "zaterdag",
    "zondag",
]

DUTCH_MONTHS = [
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
]


@dataclass(frozen=True)
class TidePoint:
    timestamp: datetime
    value_cm: float
    source: str


_location_cache: List[Dict[str, str]] = []
_location_cache_expires_at: datetime | None = None
_stroomatlas_image_cache: Dict[int, bytes] = {}


def _parse_date(raw: str | None) -> date:
    if not raw:
        return datetime.now(TIMEZONE).date()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return datetime.now(TIMEZONE).date()


def _validate_api_date(raw: str | None) -> date:
    if raw:
        try:
            selected = date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("date moet het formaat YYYY-MM-DD hebben") from exc
    else:
        selected = datetime.now(TIMEZONE).date()
    today = datetime.now(TIMEZONE).date()
    min_day = today + timedelta(days=MIN_API_DAY_OFFSET)
    max_day = today + timedelta(days=MAX_API_DAY_OFFSET)
    if selected < min_day or selected > max_day:
        raise ValueError(
            f"date buiten toegestane range ({min_day.isoformat()} t/m {max_day.isoformat()})"
        )
    return selected


def _validate_location_code(raw: str | None) -> str:
    value = (raw or DEFAULT_LOCATION_CODE).strip()
    if not value:
        raise ValueError("location is verplicht")
    if len(value) > MAX_LOCATION_CODE_LENGTH:
        raise ValueError("location is te lang")
    if not LOCATION_CODE_RE.fullmatch(value):
        raise ValueError("location bevat ongeldige tekens")
    return value


def _validate_locations_query(raw: str | None) -> str:
    query = (raw or "").strip().lower()
    if len(query) > MAX_SEARCH_QUERY_LENGTH:
        raise ValueError("q is te lang")
    if any(ord(ch) < 32 for ch in query):
        raise ValueError("q bevat ongeldige tekens")
    return query


def _validate_limit(raw: str | None) -> int:
    if raw is None:
        return 50
    if not raw.isdigit():
        raise ValueError("limit moet een positief geheel getal zijn")
    limit = int(raw)
    if limit < 1 or limit > 200:
        raise ValueError("limit moet tussen 1 en 200 liggen")
    return limit


def _json_bad_request(message: str):
    return jsonify({"error": message}), 400


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
        raw = response.read()
        if not raw or not raw.strip():
            return {}
        return json.loads(raw)


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


def _build_stroomatlas_windows(high_waters: List[TidePoint]) -> List[Dict]:
    offsets = list(range(STROOMATLAS_MIN_OFFSET, STROOMATLAS_MAX_OFFSET + 1))
    windows: List[Dict] = []

    for hw in sorted(high_waters, key=lambda p: p.timestamp):
        hw_dordrecht = hw.timestamp + timedelta(hours=STROOMATLAS_DORDRECHT_SHIFT_HOURS)
        rows: List[Dict[str, str | int]] = []
        for offset in offsets:
            ts = hw_dordrecht + timedelta(hours=offset)
            if offset == 0:
                relative_label = "HW Dordrecht"
            elif offset < 0:
                relative_label = f"{abs(offset)} uur voor HW Dordrecht"
            else:
                relative_label = f"{offset} uur na HW Dordrecht"

            day_shift = (ts.date() - hw_dordrecht.date()).days
            shift_suffix = ""
            if day_shift > 0:
                shift_suffix = f" (+{day_shift}d)"
            elif day_shift < 0:
                shift_suffix = f" ({day_shift}d)"

            rows.append(
                {
                    "offset_hours": offset,
                    "relative_label": relative_label,
                    "time": f"{ts.strftime('%H:%M')}{shift_suffix}",
                    "image_url": f"/stroomatlas/moment/{_canonical_stroomatlas_image_offset(offset)}.png",
                }
            )

        windows.append(
            {
                "hw_time": hw_dordrecht.strftime("%H:%M"),
                "rows": rows,
            }
        )

    return windows


def _date_options(anchor: date, span_days: int = 7) -> List[str]:
    return [(anchor + timedelta(days=delta)).isoformat() for delta in range(-span_days, span_days + 1)]


def _format_date_label_nl(day: date) -> str:
    weekday = DUTCH_WEEKDAYS[day.weekday()]
    month = DUTCH_MONTHS[day.month - 1]
    return f"{day.day} {month} {day.year} ({weekday})"


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _date_options_with_labels(anchor: date) -> List[Dict[str, str]]:
    start_day = _add_months(anchor, -1)
    end_day = _add_months(anchor, 6)
    options: List[Dict[str, str]] = []
    day_count = (end_day - start_day).days
    for delta in range(day_count + 1):
        current_day = start_day + timedelta(days=delta)
        label = _format_date_label_nl(current_day)
        if current_day == anchor:
            label = f"{label} - vandaag"
        options.append({"value": current_day.isoformat(), "label": label})
    return options


def _has_displayable_height_data(points: List[TidePoint]) -> bool:
    return any(point.source in {"verwachting", "meting"} for point in points)


def _stroomatlas_pdf_path() -> str:
    return os.path.join(app.template_folder or "templates", STROOMATLAS_PDF_FILENAME)


def _stroomatlas_page_index(offset_hours: int) -> int:
    return offset_hours + STROOMATLAS_PAGE_SHIFT


def _canonical_stroomatlas_image_offset(offset_hours: int) -> int:
    # -1 and 6 refer to the same atlas moment image; normalize to the working URL set.
    return offset_hours % 7


def _render_stroomatlas_moment_png(offset_hours: int) -> bytes:
    if offset_hours in _stroomatlas_image_cache:
        return _stroomatlas_image_cache[offset_hours]

    if offset_hours < STROOMATLAS_MIN_OFFSET or offset_hours > STROOMATLAS_MAX_OFFSET:
        raise ValueError("ongeldig stroomatlas moment")

    page_index = _stroomatlas_page_index(offset_hours)
    with fitz.open(_stroomatlas_pdf_path()) as doc:
        if page_index < 0 or page_index >= doc.page_count:
            raise ValueError("stroomatlas bevat geen pagina voor dit moment")
        page = doc[page_index]
        pix = page.get_pixmap(matrix=fitz.Matrix(STROOMATLAS_RENDER_SCALE, STROOMATLAS_RENDER_SCALE), alpha=False)
        png_bytes = pix.tobytes("png")
        _stroomatlas_image_cache[offset_hours] = png_bytes
        return png_bytes


def _render_stroomatlas_legend_png() -> bytes:
    cache_key = -999
    if cache_key in _stroomatlas_image_cache:
        return _stroomatlas_image_cache[cache_key]

    with fitz.open(_stroomatlas_pdf_path()) as doc:
        if STROOMATLAS_LEGEND_PAGE_INDEX >= doc.page_count:
            raise ValueError("stroomatlas bevat geen legenda-pagina")
        page = doc[STROOMATLAS_LEGEND_PAGE_INDEX]
        pix = page.get_pixmap(matrix=fitz.Matrix(STROOMATLAS_RENDER_SCALE, STROOMATLAS_RENDER_SCALE), alpha=False)
        png_bytes = pix.tobytes("png")
        _stroomatlas_image_cache[cache_key] = png_bytes
        return png_bytes


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
    selected_location = DEFAULT_LOCATION_CODE
    return render_template(
        "index.html",
        selected_date=selected_day.isoformat(),
        date_options=_date_options_with_labels(datetime.now(TIMEZONE).date()),
        default_location=DEFAULT_LOCATION_CODE,
        selected_location=selected_location,
        include_stroomatlas=False,
        waterdata_info_url=WATERDATA_INFO_URL,
        swagger_url="https://ddapi20-waterwebservices.rijkswaterstaat.nl/swagger-ui/index.html",
    )


@app.route("/stroomatlas")
def index_with_stroomatlas():
    selected_day = _parse_date(request.args.get("date"))
    selected_location = DEFAULT_LOCATION_CODE
    return render_template(
        "index.html",
        selected_date=selected_day.isoformat(),
        date_options=_date_options_with_labels(datetime.now(TIMEZONE).date()),
        default_location=DEFAULT_LOCATION_CODE,
        selected_location=selected_location,
        include_stroomatlas=True,
        waterdata_info_url=WATERDATA_INFO_URL,
        swagger_url="https://ddapi20-waterwebservices.rijkswaterstaat.nl/swagger-ui/index.html",
    )


@app.route("/vandaag")
def vandaag():
    today = datetime.now(TIMEZONE).date()
    return render_template(
        "vandaag.html",
        selected_date=today.isoformat(),
        selected_location=DEFAULT_LOCATION_CODE,
    )


@app.route("/stroomatlas/moment/<int:offset_hours>.png")
def stroomatlas_moment_image(offset_hours: int):
    try:
        png_bytes = _render_stroomatlas_moment_png(offset_hours)
    except ValueError:
        return jsonify({"error": "ongeldig stroomatlas moment"}), 404
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": str(exc)}), 502

    return app.response_class(png_bytes, mimetype="image/png")


@app.route("/stroomatlas/legend.png")
def stroomatlas_legend_image():
    try:
        png_bytes = _render_stroomatlas_legend_png()
    except ValueError:
        return jsonify({"error": "stroomatlas legenda niet beschikbaar"}), 404
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": str(exc)}), 502

    return app.response_class(png_bytes, mimetype="image/png")


@app.route("/api/locations")
def api_locations():
    try:
        query = _validate_locations_query(request.args.get("q"))
        limit = _validate_limit(request.args.get("limit"))
    except ValueError as exc:
        return _json_bad_request(str(exc))

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
    try:
        selected_day = _validate_api_date(request.args.get("date"))
        location = _validate_location_code(request.args.get("location"))
    except ValueError as exc:
        return _json_bad_request(str(exc))

    try:
        points = _merge_points(selected_day, location)
        highs, lows = _find_high_low(points)
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": str(exc)}), 502

    is_future = selected_day > datetime.now(TIMEZONE).date()
    graph_points = points
    message = None
    if is_future and not _has_displayable_height_data(points):
        graph_points = []
        message = "geen toekomstige hoogteinformatie beschikbaar"

    return jsonify(
        {
            "date": selected_day.isoformat(),
            "location": location,
            "point_count": len(graph_points),
            "points": _serialize_points(graph_points),
            "high_waters": _serialize_points(highs),
            "low_waters": _serialize_points(lows),
            "stroomatlas_windows": _build_stroomatlas_windows(highs),
            "sources_checked": ["verwachting", "meting", "astronomisch"],
            "message": message,
            "is_future": is_future,
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


