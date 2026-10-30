
import inspect
import importlib.metadata
from pathlib import Path
import FlightRadarAPI as fr24_package
import os
import time
from fastapi import FastAPI, Header, HTTPException
from FlightRadarAPI import FlightRadar24API

app = FastAPI()

ACCESS_TOKEN = os.environ.get("FLIGHTCAST_TOKEN")

@app.get("/")
def health():
    return {"status": "FlightCast Python service is running"}

@app.get("/flights")
def get_flights(
    lat: float,
    lon: float,
    radius: int = 15,
    x_flightcast_token: str = Header(default="")
):
    if not ACCESS_TOKEN or x_flightcast_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorised")

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
            # Read the Flight object's available attributes.
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

            results.append({
                "hex": str(get_value("id", "icao24") or ""),
                "flight": str(get_value("callsign", "number") or "").strip(),
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

        return {
            "source": "FlightRadarAPI Python",
            "count": len(results),
            "now": now,
            "aircraft": results
        }

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Flight data request failed: {str(error)}"
        )

@app.get("/diagnostics")
def diagnostics(x_flightcast_token: str = Header(default="")):
    if not ACCESS_TOKEN or x_flightcast_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorised")

    try:
        version = importlib.metadata.version("FlightRadarAPI")
        package_path = Path(fr24_package.__file__).parent

        methods = {}
        for name, method in inspect.getmembers(
            FlightRadar24API, predicate=inspect.isfunction
        ):
            try:
                methods[name] = str(inspect.signature(method))
            except Exception:
                methods[name] = "Signature unavailable"

        keywords = (
            "takeoff", "take_off", "landed", "landing",
            "runway", "datetime", "destination_airport",
            "origin_airport", "flight_details"
        )

        matches = []
        for source_file in package_path.rglob("*.py"):
            try:
                for line_number, line in enumerate(
                    source_file.read_text(
                        encoding="utf-8", errors="ignore"
                    ).splitlines(), start=1
                ):
                    if any(word in line.lower() for word in keywords):
                        matches.append({
                            "file": source_file.name,
                            "line": line_number,
                            "code": line.strip()[:250]
                        })
                        if len(matches) >= 100:
                            break
            except Exception:
                continue

            if len(matches) >= 100:
                break

        return {
            "status": "diagnostics completed",
            "library": "FlightRadarAPI",
            "version": version,
            "available_methods": methods,
            "matching_source_lines": matches
        }

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Diagnostics failed: {str(error)}"
        )
