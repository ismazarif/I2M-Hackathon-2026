import json
import sqlite3
import threading
import time
import requests
import hmac
import hashlib
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# =========================================================
# SYSTEM CONFIGURATION & AUTH
# =========================================================

SIMULATOR_BASE_URL = "http://127.0.0.1:9898/api/v1"
WEBHOOK_SECRET = b"your_secret_key_here"  # Replace with actual secret if provided
ADMIN_EMAIL = "admin"
ADMIN_PASSWORD = "admin"
JWT_TOKEN = None

# =========================================================
# INFRASTRUCTURE MAPPING (ZONES 1, 2, 3)
# =========================================================

ZONES = {
    "ZONE1": {
        "entry_spot": "ENTRY1",
        "exit_spot": "EXIT_EXIT",
        "gate_in": "gate1",
        "gate_out": "gate2",
        "fans": ["f_0", "f_1", "fan5", "fan4"],
        "lights": ["t_2", "t_8", "light22", "t_1", "t_0", "t_4", "light26", "t_9", "light19", "t_11"],
        "parking": {
            "Electric": ["S5", "S6", "S10", "S11", "S20", "S21", "S25", "S26"],
            "Accessible": ["S7", "S8", "S9"],
            "Any": ["S1", "S2", "S3", "S4", "S12", "S13", "S14", "S15", "S16", "S17", "S18", "S19", "S22", "S23", "S24", "S27", "S28", "S29", "S30"]
        }
    },
    "ZONE2": {
        "entry_spot": "ENTRY2",
        "exit_spot": "Exit67",
        "gate_in": "gate3",
        "gate_out": "gate4",
        "fans": ["fan0", "fan1", "fan2", "fan3"],
        "lights": ["t_6", "t_10", "t_3", "light28", "light27", "light26", "light23", "light21", "light20"],
        "parking": {
            "Electric": ["bay42", "bay43", "bay47", "bay48", "bay56", "bay57", "bay61", "bay62"],
            "Accessible": [], # None explicitly stated, fallback to Any
            "Any": ["bay36", "bay37", "bay39", "bay40", "bay41", "bay44", "bay45", "bay46", "bay49", "bay50", "bay51", "bay52", "bay53", "bay54", "bay55", "bay58", "bay59", "bay60", "bay63", "bay64", "bay65", "bay66"]
        }
    },
    "ZONE3": {
        "entry_spot": "ENTRY3",
        "exit_spot": "Exit100",
        "gate_in": "gate5",
        "gate_out": "gate6",
        "fans": ["fan 6", "fan 7", "fan 8", "fan 9"],
        "lights": ["light 32", "light 33", "light 34", "light 35", "light 36", "light 37", "light 38", "light 39", "light 40", "light 41"],
        "parking": {
            "Electric": [f"P{i}" for i in range(74, 79)],
            "Accessible": ["P79", "P80", "P81"],
            "Any": [f"P{i}" for i in range(69, 74)] + [f"P{i}" for i in range(82, 99)]
        }
    }
}

# State tracking
spot_lock = threading.Lock()
spot_state = {}           # spot_name -> plate
car_entry_times = {}      # plate -> timestamp
usage_cycles = {}         # component_name -> usage count
CO_LEVELS = {"ZONE1": 0, "ZONE2": 0, "ZONE3": 0}

last_sequence_id = None
total_revenue = 0.0

# =========================================================
# AUTHENTICATION & API HELPERS
# =========================================================

def authenticate():
    """Authenticates with the simulator and stores the JWT token[cite: 1]."""
    global JWT_TOKEN
    url = f"{SIMULATOR_BASE_URL}/auth/login"
    try:
        response = requests.post(url, json={"Email": ADMIN_EMAIL, "Password": ADMIN_PASSWORD}, timeout=5)
        if response.status_code == 200:
            JWT_TOKEN = response.json().get("token")
            print("System Authenticated Successfully.")
            log_login_attempt(ADMIN_EMAIL, True)
        else:
            print("Authentication Failed.")
            log_login_attempt(ADMIN_EMAIL, False)
    except Exception as e:
        print(f"Auth error: {e}")
        log_login_attempt(ADMIN_EMAIL, False)

def get_headers():
    return {"Authorization": f"Bearer {JWT_TOKEN}", "Content-Type": "application/json"}

# =========================================================
# HARDWARE CONTROL & PREVENTIVE MAINTENANCE
# =========================================================

def operate_gate(gate_name, action):
    """Opens or closes a barrier gate[cite: 1]."""
    requests.post(f"{SIMULATOR_BASE_URL}/barrier-gates/{gate_name}/{action}", headers=get_headers(), timeout=2)
    track_usage(gate_name, "gate")

def operate_fan(fan_name, action):
    """Turns an exhaust fan on or off[cite: 1]."""
    requests.post(f"{SIMULATOR_BASE_URL}/exhaust-fans/{fan_name}/{action}", headers=get_headers(), timeout=2)

def track_usage(component, comp_type):
    """Tracks usage cycles to trigger preventive maintenance before failure[cite: 3]."""
    threshold = 50 if comp_type == "gate" else 100
    usage_cycles[component] = usage_cycles.get(component, 0) + 1
    
    if usage_cycles[component] >= threshold:
        print(f"[MAINTENANCE] Triggering preventive repair for {component}")
        endpoint = "barrier-gates" if comp_type == "gate" else "parking-spots"
        requests.post(f"{SIMULATOR_BASE_URL}/{endpoint}/{component}/repair", headers=get_headers())
        usage_cycles[component] = 0
        log_audit(f"System", f"Preventive repair initiated for {component}")

def check_co_levels(zone):
    """Activates fans if CO > 50, deactivates if below to save electricity[cite: 3]."""
    level = CO_LEVELS.get(zone, 0)
    action = "on" if level > 50 else "off"
    for fan in ZONES[zone]["fans"]:
        threading.Thread(target=operate_fan, args=(fan, action), daemon=True).start()

# =========================================================
# DATABASE LOGGING
# =========================================================

def init_database():
    conn = sqlite3.connect("parking.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS webhook_events (id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, event_class TEXT, car_plate TEXT, spot_name TEXT, direction TEXT, raw_data TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS login_attempts (id INTEGER PRIMARY KEY, email TEXT, success BOOLEAN, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY, user TEXT, action TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS penalties (id INTEGER PRIMARY KEY, reason TEXT, amount REAL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    conn.close()

def log_login_attempt(email, success):
    conn = sqlite3.connect("parking.db")
    conn.execute("INSERT INTO login_attempts (email, success) VALUES (?, ?)", (email, success))
    conn.commit()
    conn.close()

def log_audit(user, action):
    conn = sqlite3.connect("parking.db")
    conn.execute("INSERT INTO audit_logs (user, action) VALUES (?, ?)", (user, action))
    conn.commit()
    conn.close()

# =========================================================
# CORE ROUTING LOGIC
# =========================================================

def get_zone_by_entry(entry_spot):
    for zone, data in ZONES.items():
        if data["entry_spot"] == entry_spot:
            return zone
    return None

def reserve_spot(zone, car_type):
    """Finds the appropriate free spot for the car type in the assigned zone."""
    prefs = [car_type, "Any"] if car_type in ["Electric", "Accessible"] else ["Any"]
    with spot_lock:
        for pref in prefs:
            for spot in ZONES[zone]["parking"].get(pref, []):
                if spot not in spot_state.values():
                    return spot
    return None

def auto_park_car(plate, entry_spot, car_type):
    zone = get_zone_by_entry(entry_spot)
    if not zone: return

    spot = reserve_spot(zone, car_type)
    if not spot:
        print(f"NO FREE SPOT for {plate} in {zone}")
        return

    gate = ZONES[zone]["gate_in"]
    operate_gate(gate, "open")
    
    url = f"{SIMULATOR_BASE_URL}/car/{plate}/goto/{spot}"
    requests.post(url, headers=get_headers(), timeout=2)
    print(f"Directed {plate} to {spot} in {zone}")

def process_exit_and_charge(plate, car_type, exit_spot):
    """Calculates billing and releases the car from the correct zone[cite: 3]."""
    global total_revenue
    
    # Locate which zone the exit belongs to
    zone = None
    for z, data in ZONES.items():
        if data["exit_spot"] == exit_spot:
            zone = z
            break
            
    if not zone: return

    # Ghost Car Protocol: Car never triggered entry sensor but is trying to leave
    if plate not in car_entry_times:
        print(f"[GHOST CAR DETECTED] {plate} has no entry record. Applying maximum daily penalty rate.")
        duration_sec = 86400  # Assume 24 hours
        log_audit("System", f"Ghost car {plate} processed at exit {exit_spot}")
    else:
        duration_sec = time.time() - car_entry_times[plate]

    minutes_spent = max(1, int(duration_sec / 60))
    
    # Billing calculation[cite: 3]
    parking_cost = float(minutes_spent)
    charging_cost = float(minutes_spent) if car_type == "Electric" else 0.0
    
    charge_url = f"{SIMULATOR_BASE_URL}/car/{plate}/charge?parkingCost={parking_cost}&chargingCost={charging_cost}"
    requests.post(charge_url, headers=get_headers())
    total_revenue += (parking_cost + charging_cost)

    gate = ZONES[zone]["gate_out"]
    operate_gate(gate, "open")
    
    leave_url = f"{SIMULATOR_BASE_URL}/car/{plate}/goto/leavepark"
    requests.post(leave_url, headers=get_headers())
    
    if plate in car_entry_times:
        del car_entry_times[plate]

# =========================================================
# WEBHOOK ENDPOINT
# =========================================================

def verify_signature(req):
    """Verifies webhook signature to prevent unauthorized calls."""
    signature = req.headers.get("X-Webhook-Signature")
    if not signature:
        return False
    payload = req.get_data()
    expected = hmac.new(WEBHOOK_SECRET, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

@app.route("/webhook", methods=["POST"])
def receive_webhook():
    # Enforce signed webhooks
    if not verify_signature(request):
        print("WARNING: Unsigned webhook rejected.")
        log_audit("System", "Rejected unsigned webhook payload")
        return jsonify({"status": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data: return jsonify({"status": "invalid json"}), 400

    event_class = data.get("EventClass")
    plate = data.get("CarPlateNumber")
    car_type = data.get("CarType")
    spot_name = data.get("SpotName")
    spot_type = data.get("SpotType")
    direction = data.get("Direction")

    # CAR ENTRY / EXIT ROUTING
    if event_class == "car_spot_action":
        if spot_type == "EntrySpot" and direction == "CarIn":
            car_entry_times[plate] = time.time()
            threading.Thread(target=auto_park_car, args=(plate, spot_name, car_type), daemon=True).start()
            
        elif spot_type == "EntrySpot" and direction == "CarOut":
            zone = get_zone_by_entry(spot_name)
            threading.Thread(target=operate_gate, args=(ZONES[zone]["gate_in"], "close")).start()

        elif spot_type == "Park" and direction == "CarIn":
            spot_state[spot_name] = plate
            track_usage(spot_name, "spot")

        elif spot_type == "Park" and direction == "CarOut":
            spot_state.pop(spot_name, None)

        elif spot_type == "ExitSpot" and direction == "CarIn":
            threading.Thread(target=process_exit_and_charge, args=(plate, car_type, spot_name), daemon=True).start()

        elif spot_type == "ExitSpot" and direction == "CarOut":
            zone = None
            for z, z_data in ZONES.items():
                if z_data["exit_spot"] == spot_name:
                    zone = z
            threading.Thread(target=operate_gate, args=(ZONES[zone]["gate_out"], "close")).start()

    # ENVIRONMENTAL MONITORING[cite: 3]
    elif event_class == "co_level_change":
        zone = data.get("ZoneName")
        CO_LEVELS[zone] = data.get("Level", 0)
        check_co_levels(zone)

    # PENALTY LOGGING
    elif event_class == "penalty":
        conn = sqlite3.connect("parking.db")
        conn.execute("INSERT INTO penalties (reason, amount) VALUES (?, ?)", (data.get("Reason"), data.get("Amount")))
        conn.commit()
        conn.close()
        print(f"PENALTY INCURRED: {data.get('Reason')}")

    return jsonify({"status": "received"}), 200

# =========================================================
# DASHBOARD ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "<h1>Level 2 Parking Server Active</h1><a href='/events'>Dashboard</a>"

@app.route("/events", methods=["GET"])
def events_page():
    # RBAC logic would wrap these routes in a production app
    conn = sqlite3.connect("parking.db")
    conn.row_factory = sqlite3.Row
    penalties = conn.execute("SELECT * FROM penalties ORDER BY id DESC LIMIT 10").fetchall()
    logins = conn.execute("SELECT * FROM login_attempts ORDER BY id DESC LIMIT 3").fetchall()
    conn.close()

    html = """
    <html><head><title>Dashboard</title></head><body>
    <h1>Level 2 Dashboard</h1>
    <h3>Recent Logins</h3>
    <ul>{% for l in logins %}<li>{{ l['email'] }} - {{ 'Success' if l['success'] else 'Failed' }}</li>{% endfor %}</ul>
    <h3>Recent Penalties</h3>
    <table border='1'><tr><th>Reason</th><th>Amount</th><th>Time</th></tr>
    {% for p in penalties %}<tr><td>{{ p['reason'] }}</td><td>{{ p['amount'] }}</td><td>{{ p['timestamp'] }}</td></tr>{% endfor %}
    </table></body></html>
    """
    return render_template_string(html, logins=logins, penalties=penalties)

if __name__ == "__main__":
    init_database()
    authenticate()
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)