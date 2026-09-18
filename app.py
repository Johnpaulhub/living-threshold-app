from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime
import math
import uuid
import sqlite3
import os
from threading import Lock

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "living-threshold-secret-change-me")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
DB_PATH = "threshold.db"
users = {}
lock = Lock()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS residues (
            id TEXT PRIMARY KEY,
            lat REAL,
            lon REAL,
            people INTEGER,
            experience TEXT,
            created_at TEXT,
            seed INTEGER
        )
    """)
    conn.commit()
    conn.close()

init_db()

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def get_nearby_users(lat, lon, max_distance=800):
    nearby = []
    with lock:
        for sid, u in users.items():
            if u.get("lat") is None:
                continue
            dist = haversine(lat, lon, u["lat"], u["lon"])
            if dist <= max_distance:
                nearby.append({**u, "sid": sid, "distance": dist})
    return nearby

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("connect")
def on_connect():
    sid = request.sid
    with lock:
        users[sid] = {
            "id": str(uuid.uuid4())[:8],
            "lat": None,
            "lon": None,
            "name": f"Person-{str(uuid.uuid4())[:4]}",
            "in_threshold": False,
            "joined_at": datetime.utcnow().isoformat()
        }
    emit("connected", {"id": users[sid]["id"]})

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    with lock:
        users.pop(sid, None)

@socketio.on("update_location")
def on_location(data):
    sid = request.sid
    lat = data.get("lat")
    lon = data.get("lon")
    if lat is None or lon is None:
        return

    with lock:
        if sid in users:
            users[sid]["lat"] = float(lat)
            users[sid]["lon"] = float(lon)

    nearby = get_nearby_users(lat, lon)
    emit("nearby_update", {
        "count": len(nearby),
        "people": [{"id": u["id"], "distance": round(u["distance"])} for u in nearby]
    })

@socketio.on("scan")
def on_scan(data):
    sid = request.sid
    with lock:
        user = users.get(sid)
        if not user or user["lat"] is None:
            emit("scan_result", {"error": "Location required"})
            return

    nearby = get_nearby_users(user["lat"], user["lon"])
    count = len(nearby)

    experiences = [
        {"id": "stillness", "name": "Collective Stillness", "duration": 7},
        {"id": "slow_walk", "name": "Synchronized Slow Walk", "duration": 6},
        {"id": "mirror", "name": "Mirror Gaze", "duration": 5},
        {"id": "listening", "name": "Deep Listening", "duration": 8}
    ]
    import random
    exp = random.choice(experiences)

    emit("scan_result", {
        "count": count,
        "experience": exp,
        "message": f"{count} people nearby" if count > 0 else "No one else nearby yet"
    })

@socketio.on("join_threshold")
def on_join(data):
    sid = request.sid
    exp = data.get("experience", {})
    with lock:
        user = users.get(sid)
        if not user:
            return
        user["in_threshold"] = True
        user["current_exp"] = exp

    lat = user["lat"]
    lon = user["lon"]
    room = f"grid_{round(lat, 3)}_{round(lon, 3)}"
    join_room(room)

    nearby = get_nearby_users(lat, lon)
    emit("threshold_joined", {
        "people": len(nearby),
        "experience": exp,
        "room": room
    }, room=room)

@socketio.on("end_threshold")
def on_end(data):
    sid = request.sid
    with lock:
        user = users.get(sid)
        if not user:
            return
        user["in_threshold"] = False
        lat = user.get("lat")
        lon = user.get("lon")
        exp = user.get("current_exp", {})
        people = data.get("people", 1)

    if lat is None:
        return

    residue_id = str(uuid.uuid4())
    seed = int((lat * 10000 + lon * 10000 + people * 17) % 999999)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO residues (id, lat, lon, people, experience, created_at, seed) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (residue_id, lat, lon, people, exp.get("name", "Unknown"), datetime.utcnow().isoformat(), seed)
    )
    conn.commit()
    conn.close()

    room = f"grid_{round(lat, 3)}_{round(lon, 3)}"
    emit("threshold_ended", {
        "residue_id": residue_id,
        "people": people,
        "experience": exp.get("name"),
        "seed": seed
    }, room=room)

@socketio.on("get_residues")
def on_get_residues(data):
    lat = data.get("lat")
    lon = data.get("lon")
    if lat is None:
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, lat, lon, people, experience, created_at, seed FROM residues ORDER BY created_at DESC LIMIT 50")
    rows = c.fetchall()
    conn.close()

    results = []
    for r in rows:
        dist = haversine(lat, lon, r[1], r[2])
        if dist < 1200:
            results.append({
                "id": r[0],
                "lat": r[1],
                "lon": r[2],
                "people": r[3],
                "experience": r[4],
                "created_at": r[5],
                "seed": r[6],
                "distance": round(dist)
            })

    emit("residues", results)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
