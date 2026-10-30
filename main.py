import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from FlightRadarAPI import FlightRadar24API

app = FastAPI(title="FlightCast Python API")

ACCESS_TOKEN = os.getenv("FLIGHTCAST_TOKEN", "").strip()

# Public historical ADS-B trajectory API. No FR24 token is used.
HISTORY_API = "https://adsb.aero/api/v1/query"
HISTORY_TIMEOUT = float(os.getenv("HISTORY_TIMEOUT", "12"))
HISTORY_CACHE_SECONDS = int(os.getenv("HISTORY_CACHE_SECONDS", "60"))
HISTORY_LOOKBACK_DAYS = int(os.getenv("HISTORY_LOOKBACK_DAYS", "7"))
HISTORY_MATCH_MAX_AGE_HOURS = float(os.getenv("HISTORY_MATCH_MAX_AGE_HOURS", "3"))

_history_cache: Dict[str, Any] = {"expires": 0.0, "by_hex": {}}
_http = requests.Session()
_http.headers.update({
    "User-Agent": "FlightCast-Python/1.0 (historical ADS-B enrichment)",
    "Accept": "application/json",
})


def require_token(token: str) -> None:
    if not ACCESS_TOKEN or token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorised")


def parse_iso_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def fetch_recent_history(hex_codes: List[str]) -> Dict[str, List[dict]]:
    """
    One batched query for the unique ICAO24 addresses in this /flights response.
    adsb.aero returns completed archived trajectories; it does not promise a
    result for an aircraft whose current flight is still in progress.
    """
    unique_hex = sorted({
        str(code).strip().lower()
        for code in hex_codes
        if code and len(str(code).strip()) == 6
    })

    if not unique_hex:
        return {}

    now = time.time()
    cache_key = ",".join(unique_hex)

    if (
        _history_cache["expires"] > now
        and _history_cache.get("key") == cache_key
    ):
        return _history_cache["by_hex"]

    body = {
        "match": {"icao24": unique_hex},
        "window_days": max(1, min(HISTORY_LOOKBACK_DAYS, 7)),
        "limit": 1000,
        "include_path": False
    }

    try:
        response = _http.post(
            HISTORY_API,
            json=body,
            timeout=HISTORY_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
        flights = payload.get("flights", [])

        by_hex: Dict[str, List[dict]] = {}
        for flight in flights:
            if not isinstance(flight, dict):
                continue
            hex_code = str(flight.get("icao24") or "").lower()
            if not hex_code:
                continue
            by_hex.setdefault(hex_code, []).append(flight)

        for records in by_hex.values():
            records.sort(
                key=lambda item: parse_iso_utc(item.get("start_ts"))
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True
            )

        _history_cache.update({
            "expires": now + HISTORY_CACHE_SECONDS,
            "key": cache_key,
            "by_hex": by_hex
        })
        return by_hex

    except (requests.RequestException, ValueError, AttributeError):
        # Historical lookup is supplementary. A provider outage must not
        # prevent the existing live aircraft response from working.
        return {}


def enrich_with_history(aircraft: List[dict]) -> List[dict]:
    history = fetch_recent_history([item.get("hex", "") for item in aircraft])
    now_utc = datetime.now(timezone.utc)

    for item in aircraft:
        item.update({
            "datetime_takeoff": None,
            "datetime_landed": None,
            "runway_takeoff": None,
            "runway_landed": None,
            "departure_airport_icao": None,
            "arrival_airport_icao": None,
            "event_time_source": None,
            "event_time_status": "not_found_in_historical_adsb_archive"
        })

        hex_code = str(item.get("hex") or "").lower()
        callsign = str(item.get("flight") or item.get("callsign") or "").strip().upper()
        candidates = history.get(hex_code, [])

        matched = None
        for flight in candidates:
            history_callsign = str(flight.get("callsign") or "").strip().upper()

            # Require a callsign match when both sides have one. This reduces
            # the risk of assigning a previous flight by the same airframe.
            if callsign and history_callsign and callsign != history_callsign:
                continue

            end_time = parse_iso_utc(flight.get("end_ts"))
            if not end_time:
                continue

            age_hours = (now_utc - end_time).total_seconds() / 3600
            if age_hours < -0.1 or age_hours > HISTORY_MATCH_MAX_AGE_HOURS:
                continue

            matched = flight
            break

        if not matched:
            continue

        item.update({
            # These are the first/last airborne observations in the archive,
            # not certified wheels-off / wheels-on times.
            "datetime_takeoff": matched.get("start_ts"),
            "datetime_landed": matched.get("end_ts"),
            "departure_airport_icao": matched.get("start_airport_ident"),
            "arrival_airport_icao": matched.get("end_airport_ident"),
            "event_time_source": "adsb.aero_historical_trajectory",
            "event_time_status": "estimated_first_and_last_airborne_observation",
            "event_time_flight_id": matched.get("flight_id")
        })

    return aircraft


@app.get("/")
def health():
    return {
        "status": "FlightCast Python service is running",
        "historical_enrichment": "adsb.aero",
        "official_fr24_api_required": False
    }


@app.get("/flights")
def get_flights(
    lat: float,
    lon: float,
    radius: int = 15,
    x_flightcast_token: str = Header(default="")
):
    require_token(x_flightcast_token)

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="Invalid coordinates")

    radius = max(1, min(radius, 120))

    try:
        api = FlightRadar24API()

        bounds = api.get_bounds_by_point(
            lat,
            lon,
            radius * 1000
        )

        flights = api.get_flights(bounds=bounds)

        now = time.time()
        results = []

        for flight in flights:
            data = vars(flight)

            def get_value(*names):
                for name in names:
                    value = data.get(name)
                    if value is not None:
                        return value
                return None

            latitude = get_value("latitude", "lat")
            longitude = get_value("longitude", "lon")

            if latitude is None or longitude is None:
                continue

            callsign = str(
                get_value("callsign", "number") or ""
            ).strip()

            results.append({
                # Do NOT use "id" here: FlightRadarAPI's id is its own flight ID,
                # not the aircraft's ICAO24 transponder address.
                "hex": str(get_value("icao_24", "icao24", "icao_hex", "hex") or "").lower(),
                "flight": callsign,
                "callsign": callsign,
                "lat": float(latitude),
                "lon": float(longitude),
                "alt_baro": get_value("altitude", "alt"),
                "gs": get_value("ground_speed", "groundspeed"),
                "track": get_value("heading", "track"),
                "baro_rate": get_value("vertical_speed", "vertical_rate"),
                "squawk": get_value("squawk"),
                "t": get_value("aircraft_code", "aircraft_type"),
                "r": get_value("registration"),
                "seen_pos": 0
            })

        # Enrich the existing aircraft records in one batched historical query.
        results = enrich_with_history(results)

        return {
            "source": "FlightRadarAPI Python + adsb.aero historical enrichment",
            "count": len(results),
            "now": now,
            "aircraft": results
        }

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Flight data request failed: {str(error)}"
        )
