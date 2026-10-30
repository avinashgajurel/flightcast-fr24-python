
import os
import time
import requests

from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Header, HTTPException
from FlightRadarAPI import FlightRadar24API

app = FastAPI()

ACCESS_TOKEN = os.environ.get("FLIGHTCAST_TOKEN")
FR24_TOKEN = os.environ.get("FR24_TOKEN")

# Callsign -> (last fetched timestamp, summary)
SUMMARY_CACHE = {}
SUMMARY_CACHE_SECONDS = 120


@app.get("/")
def health():
    return {"status": "FlightCast Python service is running"}


def fetch_fr24_summaries(callsigns):
    """Fetch flight summaries for up to 15 callsigns."""

    if not FR24_TOKEN:
        raise RuntimeError("FR24_TOKEN is not configured in Render")

    now = datetime.now(timezone.utc)

    params = {
        "callsigns": ",".join(callsigns),
        "flight_datetime_from": (
            now - timedelta(hours=24)
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "flight_datetime_to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sort": "desc",
        "limit": 1000
    }

    response = requests.get(
        "https://fr24api.flightradar24.com/api/flight-summary/full",
        params=params,
        headers={
            "Authorization": f"Bearer {FR24_TOKEN}",
            "Accept-Version": "v1",
            "Accept": "application/json"
        },
        timeout=15
    )

    response.raise_for_status()

    latest = {}

    for flight in response.json().get("data", []):
        callsign = (flight.get("callsign") or "").strip()

        if callsign and callsign not in latest:
            latest[callsign] = flight

    return latest


def add_takeoff_landing(aircraft):
    """Enrich live aircraft with take-off and landing details."""

    now = time.time()
    needed = []

    for aircraft_item in aircraft:
        callsign = (
            aircraft_item.get("callsign")
            or aircraft_item.get("flight")
            or ""
        ).strip()

        if not callsign:
            continue

        cached = SUMMARY_CACHE.get(callsign)

        if (
            cached is None
            or now - cached[0] > SUMMARY_CACHE_SECONDS
        ):
            if callsign not in needed:
                needed.append(callsign)

    # FR24 allows a maximum of 15 callsigns per request.
    for i in range(0, len(needed), 15):
        batch = needed[i:i + 15]

        try:
            summaries = fetch_fr24_summaries(batch)

            for callsign in batch:
                SUMMARY_CACHE[callsign] = (
                    now,
                    summaries.get(callsign)
                )

        except Exception as error:
            print(f"FR24 summary error: {error}")

            # Cache the failed lookup briefly to avoid repeated requests.
            for callsign in batch:
                SUMMARY_CACHE[callsign] = (now, None)

    for aircraft_item in aircraft:
        callsign = (
            aircraft_item.get("callsign")
            or aircraft_item.get("flight")
            or ""
        ).strip()

        cached = SUMMARY_CACHE.get(callsign)

        if not cached or not cached[1]:
            continue

        summary = cached[1]

        for field in (
            "datetime_takeoff",
            "datetime_landed",
            "runway_takeoff",
            "runway_landed"
        ):
            if summary.get(field) is not None:
                aircraft_item[field] = summary[field]

    return aircraft


@app.get("/flights")
def get_flights(
    lat: float,
    lon: float,
    radius: int = 15,
    x_flightcast_token: str = Header(default="")
):
    if not ACCESS_TOKEN or x_flightcast_token != ACCESS_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Unauthorised"
        )

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(
            status_code=400,
            detail="Invalid coordinates"
        )

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
                "hex": str(
                    get_value("id", "icao24") or ""
                ),
                "flight": callsign,
                "callsign": callsign,
                "lat": float(latitude),
                "lon": float(longitude),
                "alt_baro": get_value("altitude", "alt"),
                "gs": get_value("ground_speed", "groundspeed"),
                "track": get_value("heading", "track"),
                "baro_rate": get_value(
                    "vertical_speed",
                    "vertical_rate"
                ),
                "squawk": get_value("squawk"),
                "t": get_value(
                    "aircraft_code",
                    "aircraft_type"
                ),
                "r": get_value("registration"),
                "seen_pos": 0
            })

        # Add take-off and landing information.
        results = add_takeoff_landing(results)

        return {
            "source": "FlightRadarAPI Python + FR24 Flight Summary",
            "count": len(results),
            "now": now,
            "aircraft": results
        }

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Flight data request failed: {str(error)}"
        )
