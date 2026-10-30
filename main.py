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
