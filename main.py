import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import requests

# Local database helpers (fallback/persistence)
from database import create_document
from schemas import AttendanceRecord

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AttendanceIn(BaseModel):
    name: str = Field(...)
    email: str = Field(...)
    latitude: float = Field(...)
    longitude: float = Field(...)
    accuracy_m: float = Field(..., ge=0)
    photo_base64: Optional[str] = None


@app.get("/")
def read_root():
    return {"message": "Employee Attendance Backend running"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/api/info")
def info():
    return {
        "service": "attendance-backend",
        "apps_script_configured": bool(os.getenv("APPS_SCRIPT_URL")),
        "sheets_dashboard_url": os.getenv("SHEETS_DASHBOARD_URL") or None,
        "geofence": {
            "lat": os.getenv("OFFICE_LAT"),
            "lng": os.getenv("OFFICE_LNG"),
            "radius_m": os.getenv("OFFICE_RADIUS_M")
        }
    }


@app.post("/api/attendance")
def submit_attendance(payload: AttendanceIn):
    # Basic validation for accuracy threshold; frontend also enforces
    max_accuracy = float(os.getenv("MAX_ALLOWED_ACCURACY_M", "50"))
    if payload.accuracy_m > max_accuracy:
        raise HTTPException(status_code=400, detail=f"Location accuracy too low (> {max_accuracy} m)")

    # Server-side timestamp
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")

    # Optional geofence check (server-side)
    geofence_ok: Optional[bool] = None
    try:
        office_lat = float(os.getenv("OFFICE_LAT")) if os.getenv("OFFICE_LAT") else None
        office_lng = float(os.getenv("OFFICE_LNG")) if os.getenv("OFFICE_LNG") else None
        office_radius = float(os.getenv("OFFICE_RADIUS_M")) if os.getenv("OFFICE_RADIUS_M") else None
        if all(v is not None for v in [office_lat, office_lng, office_radius]):
            # Haversine distance in meters
            from math import radians, sin, cos, atan2, sqrt
            R = 6371000
            dlat = radians(payload.latitude - office_lat)
            dlon = radians(payload.longitude - office_lng)
            a = sin(dlat/2)**2 + cos(radians(office_lat)) * cos(radians(payload.latitude)) * sin(dlon/2)**2
            c = 2 * atan2(sqrt(a), sqrt(1-a))
            dist_m = R * c
            geofence_ok = dist_m <= office_radius
            if not geofence_ok:
                raise HTTPException(status_code=400, detail=f"Outside geofence (distance {dist_m:.1f} m > {office_radius:.0f} m)")
    except ValueError:
        # If envs are present but invalid, ignore geofence
        geofence_ok = None

    # Build record
    record = AttendanceRecord(
        name=payload.name,
        email=payload.email,
        date=date_str,
        time=time_str,
        latitude=payload.latitude,
        longitude=payload.longitude,
        accuracy_m=payload.accuracy_m,
        photo_url=None,
        geofence_ok=geofence_ok,
        raw_photo_base64=payload.photo_base64,
    )

    # Forward to Google Apps Script if configured
    apps_script_url = os.getenv("APPS_SCRIPT_URL")
    photo_url_from_apps: Optional[str] = None
    forwarded_ok = False
    apps_script_error = None

    if apps_script_url:
        try:
            # Payload for Apps Script: include month sheet name like "Nov-2025"
            month_tab = now.strftime("%b-%Y")
            gs_payload = {
                "name": record.name,
                "email": record.email,
                "date": record.date,
                "time": record.time,
                "latitude": record.latitude,
                "longitude": record.longitude,
                "accuracy_m": record.accuracy_m,
                "month_tab": month_tab,
                "photo_base64": record.raw_photo_base64,
                "geofence_ok": record.geofence_ok,
            }
            resp = requests.post(apps_script_url, json=gs_payload, timeout=15)
            if resp.status_code == 200:
                forwarded_ok = True
                try:
                    data = resp.json()
                    photo_url_from_apps = data.get("photoUrl") or data.get("photo_url")
                except Exception:
                    pass
            else:
                apps_script_error = f"Apps Script HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            apps_script_error = str(e)

    # Persist to MongoDB regardless, for audit/fallback
    try:
        inserted_id = create_document("attendancerecord", record)
    except Exception as e:
        inserted_id = None

    # Build response
    return {
        "success": True,
        "message": "Attendance marked successfully!",
        "data": {
            "id": inserted_id,
            "date": record.date,
            "time": record.time,
            "latitude": record.latitude,
            "longitude": record.longitude,
            "accuracy_m": record.accuracy_m,
            "geofence_ok": record.geofence_ok,
            "photo_url": photo_url_from_apps,
            "apps_script_forwarded": forwarded_ok,
            "apps_script_error": apps_script_error,
        },
        "dashboard_url": os.getenv("SHEETS_DASHBOARD_URL") or None
    }


@app.get("/test")
def test_database():
    """Test endpoint to check if database is available and accessible"""
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    
    try:
        from database import db
        
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
            
    except ImportError:
        response["database"] = "❌ Database module not found (run enable-database first)"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    
    import os as _os
    response["database_url"] = "✅ Set" if _os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if _os.getenv("DATABASE_NAME") else "❌ Not Set"
    
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
