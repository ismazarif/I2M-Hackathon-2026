import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import quote
import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# =========================================================
# SYSTEM CONFIGURATION & AUTH
# =========================================================

SIMULATOR_BASE_URL = "http://127.0.0.1:9898/api/v1"
WEBHOOK_SECRET = b"your_secret_key_here"  

ADMIN_EMAIL = "admin"
ADMIN_PASSWORD = "admin"
JWT_TOKEN = None

# =========================================================
# LIGHTING, FAN & ENERGY CONFIGURATION
# =========================================================

LIGHT_ON_HOUR = 19      # night starts at 19:00[cite: 3]
LIGHT_OFF_HOUR = 7      # and ends at 07:00[cite: 3]

DEFAULT_LIGHT_WATTS = 60.0
FAN_WATTS = 200.0                  # ~0.2 kWh per hour per fan
LIGHT_WATTS_OVERRIDE = {}          

MOTION_TIMEOUT_REAL_SEC = 60       # safety net: forget a "moving" car after this
LIGHT_TICK_SEC = 2                 
MAX_PROJECT_REAL_SEC = 60          

# Maintenance Thresholds
GATE_CYCLE_LIMIT = 50
FAN_CYCLE_LIMIT = 50
PROACTIVE_MAINTENANCE_THRESHOLD = 45

# A spot is claimed the instant we decide to send a car there, before the
# simulator ever confirms arrival. If that confirmation never comes (goto
# rejected, car breaks down, webhook dropped), the spot would stay marked
# occupied forever with no way to free it. This timeout bounds how long
# an UNCONFIRMED claim can hold a spot before it's released automatically.
SPOT_RESERVATION_TIMEOUT_SEC = 45

# =========================================================
# PUBLIC TARIFF & PENALTY CONFIGURATION
# ---------------------------------------------------------
# Single source of truth: billing code AND the public display
# both read these values, so the board can never lie.
# =========================================================

CURRENCY = "RM"

# Base tariff (matches existing billing behaviour, just named)
PARKING_RATE_PER_MIN = 1.0      # charged to every car, per minute parked
EV_CHARGING_RATE_PER_MIN = 1.0  # extra, only for Electric cars
FREE_GRACE_MINUTES = 0          # minutes billed at zero on entry

# Penalty system
PENALTY_ENABLED = True              # master switch; False = detect + log only
PENALTY_OVERSTAY_PER_MIN = 2.0      # per minute beyond planned duration
PENALTY_OVERSTAY_GRACE_MIN = 10     # free minutes past planned duration
PENALTY_OVERSTAY_CAP = 120.0        # max overstay charge per visit
PENALTY_MISUSE_EV = 40.0            # non-EV car parked in an EV bay
PENALTY_MISUSE_OKU = 80.0           # non-accessible car in an OKU bay
PENALTY_SPOT_TAKEN = 25.0           # parked in a bay assigned to another car

# Driving-conduct violations (admin dashboard). Detected heuristically from
# the same webhook stream / hardware-health polling that already exists -
# see register on entry (tailgating), and hardware_monitoring_daemon
# (bay-exceeded, barrier-break).
PENALTY_TAILGATING = 100.0          # a 2nd car rode through a gate opened for another plate
PENALTY_BAY_EXCEEDED = 60.0         # a bay sensor reports damage while that car was parked in it
PENALTY_BARRIER_BREAK = 250.0       # a gate is reported broken right after that car passed through

PENALTY_LABELS = {
    "OVERSTAY":     ("Overstay",                 f"{CURRENCY} {PENALTY_OVERSTAY_PER_MIN:.2f} / min after a {PENALTY_OVERSTAY_GRACE_MIN} min grace period"),
    "MISUSE_EV":    ("EV bay misuse",            f"{CURRENCY} {PENALTY_MISUSE_EV:.2f} flat"),
    "MISUSE_OKU":   ("OKU bay misuse",           f"{CURRENCY} {PENALTY_MISUSE_OKU:.2f} flat"),
    "SPOT_TAKEN":   ("Occupying a taken bay",    f"{CURRENCY} {PENALTY_SPOT_TAKEN:.2f} flat"),
    "TAILGATING":   ("Tailgating through a gate",f"{CURRENCY} {PENALTY_TAILGATING:.2f} flat"),
    "BAY_EXCEEDED": ("Parked outside bay lines", f"{CURRENCY} {PENALTY_BAY_EXCEEDED:.2f} flat"),
    "BARRIER_BREAK":("Damaged a barrier gate",   f"{CURRENCY} {PENALTY_BARRIER_BREAK:.2f} flat"),
}

# Cost the operator is billed by the maintenance crew per repair event.
MAINTENANCE_COST = {
    "Barrier gate": 150.0,
    "Ventilation fan": 80.0,
    "Parking bay": 300.0,
}

PARK_DISPLAY_NAME = "Smart Park"
PUBLIC_REFRESH_SEC = 3

# =========================================================
# INFRASTRUCTURE MAPPING (ZONES 1, 2, 3)
# ---------------------------------------------------------
# Each zone declares its bays in PHYSICAL ORDER, nearest to
# the entrance first. The category pools below preserve that
# order, so find_zone_spot() always hands out the closest
# free bay of the right type.
# =========================================================

def _pool(order, members):
    """Filter `order` down to `members`, keeping nearest-first sequence."""
    want = set(members)
    return [n for n in order if n in want]


# ---------------- ZONE 1 : S1 - S30 ----------------
# Walking order: S1-S5, S16-S20 | S6-S10, S21-S25 | S11-S15, S26-S30
Z1_ORDER = [f"S{i}" for i in (
    list(range(1, 6))   + list(range(16, 21)) +
    list(range(6, 11))  + list(range(21, 26)) +
    list(range(11, 16)) + list(range(26, 31))
)]
Z1_EV_SET  = ["S5", "S6", "S10", "S11", "S20", "S21", "S25", "S26"]
Z1_OKU_SET = ["S7", "S8", "S9"]

Z1_EV   = _pool(Z1_ORDER, Z1_EV_SET)
Z1_OKU  = _pool(Z1_ORDER, Z1_OKU_SET)
Z1_NORM = [s for s in Z1_ORDER if s not in Z1_EV_SET and s not in Z1_OKU_SET]


# ---------------- ZONE 2 : bay36 - bay66 ----------------
# Walking order given by site survey: 36,39-42 | 43-47 | 48-52 | 37,53-56 | 57-61 | 62-66.
# bay38 is not in that survey list, so it is excluded entirely rather
# than guessed into a slot - it isn't a real, usable bay.
_Z2_SEQ = [36, 39, 40, 41, 42,
           43, 44, 45, 46, 47,
           48, 49, 50, 51, 52,
           37, 53, 54, 55, 56,
           57, 58, 59, 60, 61,
           62, 63, 64, 65, 66]
Z2_ORDER = [f"bay{n}" for n in _Z2_SEQ]

Z2_EV_SET  = ["bay42", "bay43", "bay47", "bay48", "bay56", "bay57", "bay61", "bay62"]
Z2_OKU_SET = ["bay58", "bay59", "bay60"]

Z2_EV   = _pool(Z2_ORDER, Z2_EV_SET)
Z2_OKU  = _pool(Z2_ORDER, Z2_OKU_SET)
Z2_NORM = [s for s in Z2_ORDER if s not in Z2_EV_SET and s not in Z2_OKU_SET]


# ---------------- ZONE 3 : P69 - P98 ----------------
# ENTRY3/gate5 on the left, Exit100/gate6 on the right.
# Nearest-first therefore runs left to right: P69 upward.
Z3_ORDER = [f"P{i}" for i in range(69, 99)]
Z3_EV_SET  = [f"P{i}" for i in range(74, 79)]    # P74 - P78
Z3_OKU_SET = [f"P{i}" for i in range(79, 82)]    # P79 - P81

Z3_EV   = _pool(Z3_ORDER, Z3_EV_SET)
Z3_OKU  = _pool(Z3_ORDER, Z3_OKU_SET)
Z3_NORM = [s for s in Z3_ORDER if s not in Z3_EV_SET and s not in Z3_OKU_SET]


ZONES = {
    "ZONE1": {
        "entry_spot": "ENTRY1",
        "exit_spot": "EXIT_EXIT",
        "gate_in": "gate1",
        "gate_out": "gate2",
        "fans": ["f_0", "f_1", "fan5", "fan4"],
        "lights": ["t_2", "t_8", "light22", "t_1", "t_0",
                   "t_4", "light26", "t_9", "light19", "t_11"],
        "order": Z1_ORDER,
        "parking": {
            "Electric": Z1_EV,
            "Accessible": Z1_OKU,
            "Any": Z1_NORM
        }
    },
    "ZONE2": {
        "entry_spot": "ENTRY2",
        "exit_spot": "Exit67",
        "gate_in": "gate3",
        "gate_out": "gate4",
        "fans": ["fan0", "fan1", "fan2", "fan3"],
        # light26 is shared with ZONE1; first zone to claim it owns it.
        "lights": ["t_6", "t_10", "t_3", "light28", "light27",
                   "light26", "light23", "light21", "light20"],
        "order": Z2_ORDER,
        "parking": {
            "Electric": Z2_EV,
            "Accessible": Z2_OKU,
            "Any": Z2_NORM
        }
    },
    "ZONE3": {
        "entry_spot": "ENTRY3",
        "exit_spot": "Exit100",
        "gate_in": "gate5",
        "gate_out": "gate6",
        "fans": ["fan6", "fan7", "fan8", "fan9"],
        "lights": ["light 32", "light 33", "light 34", "light 35", "light 36",
                   "light 37", "light 38", "light 39", "light 40", "light 41"],
        "order": Z3_ORDER,
        "parking": {
            "Electric": Z3_EV,
            "Accessible": Z3_OKU,
            "Any": Z3_NORM
        }
    }
}

# Lookup tables
SPOT_TO_ZONE = {}
CONFIG_LIGHT_ZONE = {}
FAN_TO_ZONE = {}
for _zone, _data in ZONES.items():
    for _pool in _data["parking"].values():
        for _spot in _pool:
            SPOT_TO_ZONE.setdefault(_spot, _zone)
    for _light in _data["lights"]:
        CONFIG_LIGHT_ZONE.setdefault(_light, _zone)
    for _fan in _data["fans"]:
        FAN_TO_ZONE.setdefault(_fan, _zone)

# State tracking
spot_lock = threading.Lock()
spot_state = {}           
spot_reservations = {}    # spot -> {"plate": .., "at": time.time()} - UNCONFIRMED claims only,
                           # removed as soon as arrival is confirmed. Anything left in here
                           # past SPOT_RESERVATION_TIMEOUT_SEC gets auto-released.
car_entry_times = {}      
gate_status_cache = {}    
fan_status_cache = {}
gate_cycles = {}          
fan_cycles = {}
fan_active_state = {}     
CO_LEVELS = {"ZONE1": 0.0, "ZONE2": 0.0, "ZONE3": 0.0}

last_sequence_id = None
total_revenue = 0.0

# ---- Public display / penalty state (additive, no routing impact) ----

# spot name -> "Electric" | "Accessible" | "Any"
SPOT_CATEGORY = {}
for _zone, _data in ZONES.items():
    for _cat, _pool in _data["parking"].items():
        for _spot in _pool:
            SPOT_CATEGORY.setdefault(_spot, _cat)

CATEGORY_LABELS = {
    "Any":        "Standard",
    "Electric":   "EV",
    "Accessible": "OKU",
}

spot_health_cache = {}      # spot -> {"broken": bool, "isUnderMaintenance": bool}

penalty_lock = threading.Lock()
planned_duration = {}       # plate -> planned minutes (from entry webhook)
pending_penalties = {}      # plate -> [ {code, amount, detail} ] not yet billed
recent_penalties = []       # rolling list for the public board
total_penalty_revenue = 0.0

charge_lock = threading.Lock()
charged_plates = set()    

# ---- Admin dashboard state (additive, no routing impact) ----
car_type_map = {}           # plate -> CarType, set on entry, used for revenue-by-type
revenue_by_type = {}        # CarType -> RM billed so far (parking + charging + penalties)
gate_open_for = {}          # gate_in -> plate the gate is CURRENTLY open for (tailgate check)
gate_last_plate = {}        # gate name -> last plate that passed through it (barrier-break attribution)
_gate_was_down = {}         # gate name -> bool, previous polled broken/maintenance state
_fan_was_down = {}          # fan name -> bool, previous polled broken/maintenance state
_spot_was_down = {}         # spot name -> bool, previous polled broken/maintenance state

maintenance_lock = threading.Lock()
total_maintenance_cost = 0.0
maintenance_log = []        # rolling list of repair events for the admin board

ADMIN_SESSIONS = set()      # valid bearer tokens issued by /api/admin/login

# Lighting / energy state
light_lock = threading.Lock()
light_state = {}          
light_clock = {"last": None, "total_sec": 0.0, "night_sec": 0.0}   
fan_energy_clock = {"last": None, "total_wh": 0.0}
sim_clock = {"sim": None, "real": None, "speed": None, "cal_sim": None, "cal_real": None}
car_motion = {}           

# =========================================================
# AUTHENTICATION & STARTUP SYNC
# =========================================================

def authenticate_and_sync():
    global JWT_TOKEN
    url = f"{SIMULATOR_BASE_URL}/auth/login"
    try:
        response = requests.post(url, json={"Email": ADMIN_EMAIL, "Password": ADMIN_PASSWORD}, timeout=5)
        if response.status_code == 200:
            JWT_TOKEN = response.json().get("token")
            print("System Authenticated Successfully.")
            log_login_attempt(ADMIN_EMAIL, True)
            time.sleep(1)
            sync_parking_spots()
            sync_lights()
            sync_fans()
            
            threading.Thread(target=hardware_monitoring_daemon, daemon=True).start()
        else:
            print("Authentication Failed.")
            log_login_attempt(ADMIN_EMAIL, False)
    except Exception as e:
        print(f"Auth error: {e}")
        log_login_attempt(ADMIN_EMAIL, False)

def get_headers():
    return {"Authorization": f"Bearer {JWT_TOKEN}", "Content-Type": "application/json"}

def sync_parking_spots():
    print("Syncing live parking spot states from simulator...")
    url = f"{SIMULATOR_BASE_URL}/list-parking-spots"
    try:
        response = requests.get(url, headers=get_headers(), timeout=5)
        if response.status_code == 200:
            spots = response.json()
            locked_count = 0
            with spot_lock:
                for spot in spots:
                    name = spot.get("name")
                    detected_cars = spot.get("detectedCars", [])
                    broken = spot.get("broken", False)
                    maintenance = spot.get("isUnderMaintenance", False)

                    if name and spot.get("zoneParent"):
                        SPOT_TO_ZONE[name] = spot["zoneParent"]
                    
                    if len(detected_cars) > 0 or broken or maintenance:
                        spot_state[name] = "PRE_OCCUPIED"
                        locked_count += 1
            print(f"Sync complete. {locked_count} unavailable spots locked.")
    except Exception as e:
        print(f"Failed to sync parking spots: {e}")

def sync_fans():
    """Reconcile our configured fan names against what the simulator
    actually reports, per zone - the same reconciliation sync_lights()
    already does for lights. If a zone's configured fan names don't
    match the live API's names (spacing, casing, anything), every
    on/off command we send for that zone silently targets a fan that
    doesn't exist, and CO in that zone never gets brought down."""
    print("Syncing exhaust fan names from simulator...")
    try:
        r = requests.get(f"{SIMULATOR_BASE_URL}/list-exhaust-fans", headers=get_headers(), timeout=5)
        if r.status_code != 200:
            print(f"[FANS] list-exhaust-fans returned {r.status_code}; keeping configured names.")
            return
        live = r.json()
    except Exception as e:
        print(f"[FANS] sync failed ({e}); keeping configured names.")
        return

    live_by_zone = {}
    for f in live:
        name, zone = f.get("name"), f.get("zoneParent")
        if name and zone:
            live_by_zone.setdefault(zone, []).append(name)

    changed = False
    for zone_name, zdata in ZONES.items():
        configured = zdata["fans"]
        live_names = live_by_zone.get(zone_name)
        if not live_names:
            print(f"[FANS] {zone_name}: simulator reported no fans for this zone; keeping configured {configured}.")
            continue
        if set(live_names) == set(configured):
            continue
        print(f"[FANS] {zone_name}: configured names {configured} don't match live "
              f"names {live_names}. Switching to the live names.")
        zdata["fans"] = live_names
        changed = True

    if changed:
        # Rebuild the fan -> zone lookup that operate_fan() and the
        # maintenance daemon rely on, now that names have shifted.
        FAN_TO_ZONE.clear()
        for zone_name, zdata in ZONES.items():
            for fan in zdata["fans"]:
                FAN_TO_ZONE.setdefault(fan, zone_name)

    print(f"[FANS] Registry after sync: { {z: d['fans'] for z, d in ZONES.items()} }")

# =========================================================
# HARDWARE CONTROL & PROACTIVE MAINTENANCE (GATES & FANS)
# =========================================================

def operate_gate(gate_name, action):
    with spot_lock:
        status = gate_status_cache.get(gate_name, {})
        if action == "open" and (status.get("broken") or status.get("isUnderMaintenance")):
            print(f"[SECURITY BLOCK] Prevented opening gate {gate_name} because it is broken or under maintenance!")
            return
        if action == "open":
            gate_cycles[gate_name] = gate_cycles.get(gate_name, 0) + 1

    try:
        requests.post(f"{SIMULATOR_BASE_URL}/barrier-gates/{gate_name}/{action}", headers=get_headers(), timeout=2)
        print(f"Gate {gate_name} command: {action}")
    except Exception as e:
        print(f"Error operating gate {gate_name}: {e}")

def operate_fan(fan_name, action):
    with spot_lock:
        status = fan_status_cache.get(fan_name, {})
        already_on = fan_active_state.get(fan_name, False)

        if action == "on":
            if status.get("broken") or status.get("isUnderMaintenance"):
                print(f"[SECURITY BLOCK] Prevented turning on fan {fan_name} because it is broken or under maintenance!")
                return
            if already_on:
                return   # already running: no real transition, so no cycle/CO/API call
            fan_cycles[fan_name] = fan_cycles.get(fan_name, 0) + 1
            fan_active_state[fan_name] = True
            
            zone_name = FAN_TO_ZONE.get(fan_name)
            if zone_name:
                current_co = CO_LEVELS.get(zone_name, 0.0)
                CO_LEVELS[zone_name] = max(0.0, current_co - 2.0)
        else:
            if not already_on:
                return   # already off: nothing to do
            fan_active_state[fan_name] = False

    try:
        requests.post(f"{SIMULATOR_BASE_URL}/exhaust-fans/{quote(fan_name, safe='')}/{action}", headers=get_headers(), timeout=2)
    except Exception as e:
        print(f"Error operating fan {fan_name}: {e}")

def hardware_monitoring_daemon():
    global gate_status_cache, fan_status_cache
    while True:
        try:
            res_barriers = requests.get(f"{SIMULATOR_BASE_URL}/list-barriers", headers=get_headers(), timeout=3)
            if res_barriers.status_code == 200:
                with spot_lock:
                    for gate in res_barriers.json():
                        name = gate.get("name")
                        broken = gate.get("broken", False)
                        maintenance = gate.get("isUnderMaintenance", False)
                        gate_status_cache[name] = {"broken": broken, "isUnderMaintenance": maintenance}
                        
                        # A gate that just NOW became broken (not already known broken,
                        # and not a routine maintenance flag) is treated as physical
                        # damage - attribute it to whichever plate most recently drove
                        # through it, if any.
                        was_down = _gate_was_down.get(name, False)
                        if broken and not was_down:
                            culprit = gate_last_plate.get(name)
                            if culprit:
                                register_penalty(culprit, "BARRIER_BREAK", PENALTY_BARRIER_BREAK,
                                                 f"Gate {name} reported broken after {culprit} passed through")
                        _gate_was_down[name] = bool(broken or maintenance)

                        cycles = gate_cycles.get(name, 0)
                        if broken or maintenance or cycles >= PROACTIVE_MAINTENANCE_THRESHOLD:
                            reason = "broken/maint" if (broken or maintenance) else f"reached {cycles} cycles (proactive)"
                            print(f"[MAINTENANCE] Gate {name} needs service ({reason}). Triggering repair...")
                            requests.post(f"{SIMULATOR_BASE_URL}/barrier-gates/{name}/repair", headers=get_headers(), timeout=2)
                            # Bill the repair only once per breakdown episode - not on
                            # every 3s poll for as long as the gate stays broken. The
                            # repeated repair POST above is left as-is (existing
                            # retry-until-confirmed behaviour); only the COST is gated.
                            if not was_down:
                                register_maintenance_cost("Barrier gate", name, _zone_of_gate(name), reason)
                            gate_cycles[name] = 0

            res_fans = requests.get(f"{SIMULATOR_BASE_URL}/list-exhaust-fans", headers=get_headers(), timeout=3)
            if res_fans.status_code == 200:
                with spot_lock:
                    for fan in res_fans.json():
                        name = fan.get("name")
                        broken = fan.get("broken", False)
                        maintenance = fan.get("isUnderMaintenance", False)
                        fan_status_cache[name] = {"broken": broken, "isUnderMaintenance": maintenance}
                        
                        cycles = fan_cycles.get(name, 0)
                        was_down = _fan_was_down.get(name, False)
                        _fan_was_down[name] = bool(broken or maintenance)
                        if broken or maintenance or cycles >= PROACTIVE_MAINTENANCE_THRESHOLD:
                            reason = "broken/maint" if (broken or maintenance) else f"reached {cycles} cycles (proactive)"
                            print(f"[MAINTENANCE] Fan {name} needs service ({reason}). Triggering repair...")
                            requests.post(f"{SIMULATOR_BASE_URL}/exhaust-fans/{quote(name, safe='')}/repair", headers=get_headers(), timeout=2)
                            # Same fix as gates: bill once per breakdown, not once per poll.
                            if not was_down:
                                register_maintenance_cost("Ventilation fan", name, FAN_TO_ZONE.get(name), reason)
                            fan_cycles[name] = 0

            # Read-only: parking spot health, used purely for the public board.
            # Intentionally does NOT trigger repairs, so existing behaviour is unchanged.
            res_spots = requests.get(f"{SIMULATOR_BASE_URL}/list-parking-spots", headers=get_headers(), timeout=3)
            if res_spots.status_code == 200:
                newly_down = []   # (name, occupant_or_None) collected under the lock, acted on after
                with spot_lock:
                    for sp in res_spots.json():
                        nm = sp.get("name")
                        if not nm:
                            continue
                        broken = bool(sp.get("broken", False))
                        maintenance = bool(sp.get("isUnderMaintenance", False))
                        spot_health_cache[nm] = {"broken": broken, "isUnderMaintenance": maintenance}

                        if (broken or maintenance) and not _spot_was_down.get(nm):
                            occupant = spot_state.get(nm)
                            if occupant == "PRE_OCCUPIED":
                                occupant = None
                            newly_down.append((nm, occupant, broken))
                        _spot_was_down[nm] = broken or maintenance

                for nm, occupant, broken in newly_down:
                    register_maintenance_cost("Parking bay", nm, SPOT_TO_ZONE.get(nm),
                                              "reported broken" if broken else "flagged for maintenance")
                    # A bay that broke while a real car sat in it reads as that
                    # car having exceeded the bay markings (physical damage).
                    if broken and occupant:
                        register_penalty(occupant, "BAY_EXCEEDED", PENALTY_BAY_EXCEEDED,
                                         f"Bay {nm} reported damaged while {occupant} was parked")
        except Exception as e:
            pass

        try:
            expire_stale_reservations()
        except Exception as e:
            print(f"[SPOT] reservation sweep failed: {e}")

        time.sleep(3)

def expire_stale_reservations():
    """Release any spot claim that was never confirmed by an actual
    arrival within SPOT_RESERVATION_TIMEOUT_SEC. Without this, a failed
    goto, a car that never arrives, or a dropped webhook leaves that
    spot marked occupied permanently."""
    cutoff = time.time() - SPOT_RESERVATION_TIMEOUT_SEC
    with spot_lock:
        stale = [spot for spot, r in spot_reservations.items() if r["at"] < cutoff]
        for spot in stale:
            claim = spot_reservations.pop(spot)
            # Only clear it if nothing else has since confirmed a different
            # occupant of that same spot (defensive; shouldn't normally differ).
            if spot_state.get(spot) == claim["plate"]:
                spot_state.pop(spot, None)
    for spot in stale:
        print(f"[SPOT] Released stale unconfirmed reservation on {spot} "
              f"(never confirmed within {SPOT_RESERVATION_TIMEOUT_SEC}s).")
    if stale:
        evaluate_energy_and_lighting()

def register_maintenance_cost(kind, name, zone, reason):
    """Log a repair event with its billed cost. `kind` matches MAINTENANCE_COST
    keys ('Barrier gate', 'Ventilation fan', 'Parking bay')."""
    global total_maintenance_cost
    cost = MAINTENANCE_COST.get(kind, 0.0)
    entry = {
        "name": name, "kind": kind, "zone": zone or "-",
        "cost": cost, "reason": reason,
        "at": datetime.now().strftime("%H:%M:%S"),
    }
    with maintenance_lock:
        total_maintenance_cost += cost
        maintenance_log.insert(0, entry)
        del maintenance_log[50:]
    print(f"[MAINTENANCE COST] {name} ({kind}) repaired - {CURRENCY}{cost:.2f} ({reason})")


def is_zone_entry_healthy(zone_name):
    zone = ZONES[zone_name]
    g_in = zone["gate_in"]

    with spot_lock:
        status_in = gate_status_cache.get(g_in, {})
        if status_in.get("broken") or status_in.get("isUnderMaintenance"):
            return False
            
    return True

# =========================================================
# CO LEVEL MONITORING & DYNAMIC FAN SCALING
# =========================================================

def update_co_levels(zone_name, delta):
    with spot_lock:
        current_co = CO_LEVELS.get(zone_name, 0.0)
        new_co = max(0.0, min(100.0, current_co + delta))
        CO_LEVELS[zone_name] = new_co
    
    if new_co >= 80.0:
        fans_to_turn_on = 4
    elif new_co >= 60.0:
        fans_to_turn_on = 3
    elif new_co >= 40.0:
        fans_to_turn_on = 2
    elif new_co >= 20.0:
        fans_to_turn_on = 1
    else:
        fans_to_turn_on = 0

    zone_fans = ZONES[zone_name]["fans"]
    want_on = set(zone_fans[:fans_to_turn_on])

    # This runs on EVERY car entry/exit, so only act on fans whose state
    # actually needs to change. Re-sending "on" to a fan already running
    # was double-counting its wear cycle and double-subtracting CO for
    # a physical state that never changed.
    with spot_lock:
        currently_on = {f for f in zone_fans if fan_active_state.get(f)}

    for fan in want_on - currently_on:
        threading.Thread(target=operate_fan, args=(fan, "on"), daemon=True).start()
    for fan in currently_on - want_on:
        threading.Thread(target=operate_fan, args=(fan, "off"), daemon=True).start()

# =========================================================
# SIMULATOR CLOCK
# =========================================================

def parse_server_time(server_datetime_str):
    try:
        s = str(server_datetime_str).strip().replace("T", " ").split(".")[0]
        for sep in ("+", "Z"):          
            s = s.split(sep)[0]
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception as e:
        print(f"[CLOCK] Could not parse ServerDateTime '{server_datetime_str}': {e}")
        return None

def _observe_sim_time(sim_now):
    c = sim_clock
    real = time.time()
    if c["sim"] is not None and sim_now < c["sim"] - timedelta(hours=1):     
        c.update(speed=None, cal_sim=None, cal_real=None)
    c["sim"], c["real"] = sim_now, real
    if c["cal_sim"] is None:
        c["cal_sim"], c["cal_real"] = sim_now, real
    elif real - c["cal_real"] >= 5.0:
        d_sim = (sim_now - c["cal_sim"]).total_seconds()
        d_real = real - c["cal_real"]
        if d_sim > 0:
            inst = d_sim / d_real
            c["speed"] = inst if c["speed"] is None else 0.6 * c["speed"] + 0.4 * inst
        c["cal_sim"], c["cal_real"] = sim_now, real

def _sim_now():
    c = sim_clock
    if c["sim"] is None:
        return datetime.now()  # <--- Fallback to local system time if simulator clock isn't synced yet
    if c["speed"] is None:
        return c["sim"]
    elapsed = min(time.time() - c["real"], MAX_PROJECT_REAL_SEC)
    return c["sim"] + timedelta(seconds=elapsed * c["speed"])

def current_sim_time():
    with light_lock:
        return _sim_now()

# =========================================================
# LIGHTING & ENERGY TRACKING (LIGHTS + FANS)
# =========================================================

def is_night(dt):
    return dt.hour >= LIGHT_ON_HOUR or dt.hour < LIGHT_OFF_HOUR

def init_light_inventory(api_lights=None):
    inventory = {}
    if api_lights:
        for l in api_lights:
            name = l.get("name")
            if not name or name in inventory:
                continue
            is_on = l.get("isOn")
            live_zone = l.get("zoneParent")
            if live_zone in ZONES:
                zone = live_zone                       # trust the simulator when it matches a known zone
            else:
                zone = CONFIG_LIGHT_ZONE.get(name, "UNASSIGNED")   # unknown/blank -> fall back to our map
                if live_zone and live_zone != zone:
                    print(f"[LIGHTS] {name}: simulator zoneParent '{live_zone}' unrecognised, using '{zone}'.")
            inventory[name] = {"zone": zone, "on": bool(is_on) if is_on is not None else None}
    else:
        for name, zone in CONFIG_LIGHT_ZONE.items():
            inventory[name] = {"zone": zone, "on": None}

    with light_lock:
        light_state.clear()
        for name, info in inventory.items():
            light_state[name] = {
                "zone": info["zone"],
                "on": info["on"],              
                "pending": False,
                "watts": LIGHT_WATTS_OVERRIDE.get(name, DEFAULT_LIGHT_WATTS),
                "wh": 0.0,
                "fails": 0,
                "retry_at": 0.0,
            }
        light_clock.update(last=None, total_sec=0.0, night_sec=0.0)
        fan_energy_clock.update(last=None, total_wh=0.0)
    print(f"[LIGHTS] Registered {len(inventory)} lights.")
    _rebuild_zone_light_lists()

def _rebuild_zone_light_lists():
    """Mirror ZONES[*]['lights'] to what each light actually resolved to.

    Each light already self-heals its own zone individually against the
    live API. But the roster list itself (ZONES[zone]['lights']) never
    got updated to match - so a zone could have the right lights working
    internally while its displayed/expected roster still showed the old,
    possibly-mismatched config. Fans get this same treatment in
    sync_fans(); this gives lights the identical treatment, for every
    zone uniformly, not just whichever zone happened to be checked.
    """
    grouped = {}
    with light_lock:
        for name, s in light_state.items():
            grouped.setdefault(s["zone"], []).append(name)

    for zone_name, zdata in ZONES.items():
        live_list = sorted(grouped.get(zone_name, []))
        if live_list and live_list != sorted(zdata["lights"]):
            print(f"[LIGHTS] {zone_name}: roster updated to match resolved zones "
                  f"(was {sorted(zdata['lights'])}, now {live_list}).")
            zdata["lights"] = live_list
        elif not live_list:
            print(f"[LIGHTS] {zone_name}: no lights resolved to this zone yet; "
                  f"keeping configured {zdata['lights']}.")

def sync_lights():
    try:
        r = requests.get(f"{SIMULATOR_BASE_URL}/list-lights", headers=get_headers(), timeout=5)
        if r.status_code == 200:
            init_light_inventory(r.json())
            return
    except Exception as e:
        print(f"[LIGHTS] list-lights failed ({e}); using config fallback.")
    init_light_inventory()

def motion_set(plate, zones):
    if not plate:
        return
    with light_lock:
        car_motion[plate] = {"zones": {z for z in zones if z}, "real": time.time()}
    evaluate_energy_and_lighting()

def motion_add(plate, zone):
    if not plate or not zone:
        return
    with light_lock:
        rec = car_motion.get(plate)
        if rec:
            rec["zones"].add(zone)
            rec["real"] = time.time()
        else:
            car_motion[plate] = {"zones": {zone}, "real": time.time()}
    evaluate_energy_and_lighting()

def motion_stop(plate):
    if not plate:
        return
    with light_lock:
        car_motion.pop(plate, None)
    evaluate_energy_and_lighting()

def _active_zones():
    """Zones that currently have a MOVING car in them.

    Caller must already hold light_lock (car_motion is guarded by it).
    Parked cars deliberately do NOT count: a stationary car needs no light,
    which is the whole point of the saving.
    """
    cutoff = time.time() - MOTION_TIMEOUT_REAL_SEC
    for plate in [p for p, r in car_motion.items() if r["real"] < cutoff]:
        del car_motion[plate]

    zones = set()
    for rec in car_motion.values():
        zones |= rec["zones"]
    return zones


def _zone_is_active(zone_name, active):
    return zone_name in active

def _accrue_energy(now):
    last = light_clock["last"]
    if last is None:
        light_clock["last"] = now
        fan_energy_clock["last"] = now
        return
    delta = (now - last).total_seconds()
    if delta < 0:
        if delta < -3600:                       
            light_clock["last"] = now
            fan_energy_clock["last"] = now
        return                                  
    light_clock["last"] = now
    fan_energy_clock["last"] = now
    if delta == 0:
        return
    
    light_clock["total_sec"] += delta
    if is_night(last + timedelta(seconds=delta / 2)):
        light_clock["night_sec"] += delta
    for s in light_state.values():
        if s["on"]:
            s["wh"] += s["watts"] * delta / 3600.0

    with spot_lock:
        active_fans_count = sum(1 for active in fan_active_state.values() if active)
    fan_energy_clock["total_wh"] += active_fans_count * FAN_WATTS * delta / 3600.0

def _switch_light(name, want_on):
    action = "on" if want_on else "off"
    ok = False
    try:
        r = requests.post(f"{SIMULATOR_BASE_URL}/lights/{quote(name, safe='')}/{action}",
                          headers=get_headers(), timeout=3)
        ok = r.status_code in (200, 201)
    except Exception as e:
        pass

    with light_lock:
        s = light_state.get(name)
        if s:
            s["pending"] = False
            if ok:
                s["on"] = want_on               
                s["fails"] = 0
                s["retry_at"] = 0.0
            else:
                s["fails"] += 1                 
                s["retry_at"] = time.time() + min(30, 2 * s["fails"])
    return ok

def _apply_lights(names, want_on, sim_time):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: _switch_light(n, want_on), names))

_warned_unmapped_lights = set()

def _light_wanted(s, active, name=None):
    """On only when a car is moving in THIS light's own zone.

    No park-wide fallback: a light whose zone we don't recognise stays
    off rather than reacting to traffic in every other zone, which was
    the cause of lights bleeding across zones.
    """
    zone = s["zone"]
    if zone in ZONES:
        return zone in active
    if name and name not in _warned_unmapped_lights:
        _warned_unmapped_lights.add(name)
        print(f"[LIGHTS] {name}: unmapped zone '{zone}', leaving off. Add it to ZONES config.")
    return False

def evaluate_energy_and_lighting(server_dt=None):
    on_batch, off_batch = [], []
    with light_lock:
        if server_dt is not None:
            _observe_sim_time(server_dt)
            now = server_dt
        else:
            now = _sim_now()
        if now is None:
            return
        _accrue_energy(now)
        eff = light_clock["last"]
        active = _active_zones()
        t = time.time()
        for name, s in light_state.items():
            if s["pending"] or t < s["retry_at"]:
                continue
            want = _light_wanted(s, active, name)
            if s["on"] is want:
                continue
            s["pending"] = True                 
            (on_batch if want else off_batch).append(name)

    if on_batch:
        threading.Thread(target=_apply_lights, args=(on_batch, True, eff), daemon=True).start()
    if off_batch:
        threading.Thread(target=_apply_lights, args=(off_batch, False, eff), daemon=True).start()

def light_ticker():
    while True:
        time.sleep(LIGHT_TICK_SEC)
        try:
            evaluate_energy_and_lighting()
        except Exception as e:
            pass

def get_energy_summary():
    with light_lock:
        total_wh = sum(s["wh"] for s in light_state.values())
        per_zone = {}
        for s in light_state.values():
            per_zone[s["zone"]] = per_zone.get(s["zone"], 0.0) + s["wh"]
        connected_w = sum(s["watts"] for s in light_state.values())
        always_on_wh = connected_w * light_clock["total_sec"] / 3600.0
        night_only_wh = connected_w * light_clock["night_sec"] / 3600.0
        lights_on = sum(1 for s in light_state.values() if s["on"])
        lights_total = len(light_state)
        active = sorted(_active_zones())
        speed = sim_clock["speed"]
        last = light_clock["last"]
        sim_hours = light_clock["total_sec"] / 3600.0
        fans_kwh = fan_energy_clock["total_wh"] / 1000.0
        active_fans_now = sum(1 for act in fan_active_state.values() if act)

    return {
        "sim_time": str(last) if last else None,
        "sim_speed_measured": round(speed, 2) if speed else None,
        "sim_hours_tracked": round(sim_hours, 3),
        "total_lighting_kwh": round(total_wh / 1000.0, 4),
        "total_fans_kwh": round(fans_kwh, 4),
        "active_fans_now": active_fans_now,
        "per_zone_lighting_kwh": {z: round(wh / 1000.0, 4) for z, wh in sorted(per_zone.items())},
        "baseline_always_on_kwh": round(always_on_wh / 1000.0, 4),
        "baseline_night_schedule_only_kwh": round(night_only_wh / 1000.0, 4),
        "saved_vs_always_on_kwh": round((always_on_wh - total_wh) / 1000.0, 4),
        "saved_vs_night_schedule_kwh": round((night_only_wh - total_wh) / 1000.0, 4),
        "lights_on": lights_on,
        "lights_total": lights_total,
        "zones_with_cars": active,
        "connected_load_w": connected_w,
        "default_watts_per_light": DEFAULT_LIGHT_WATTS,
    }

# =========================================================
# PENALTY ENGINE
# =========================================================

def _parse_planned_duration(data):
    """Entry webhook carries a planned parking duration; field name varies."""
    for key in ("PlannedParkingDuration", "PlannedDuration", "ParkingDuration",
                "Duration", "PlannedMinutes", "PlannedParkingTime"):
        if key in data and data[key] is not None:
            try:
                val = float(str(data[key]).strip())
                if val > 0:
                    return int(val)
            except (TypeError, ValueError):
                continue
    return None


def register_penalty(plate, code, amount, detail):
    """Queue a penalty against a plate and log it. Billed at exit."""
    if not plate or amount <= 0:
        return

    label = PENALTY_LABELS.get(code, (code, ""))[0]
    record = {
        "plate": plate,
        "code": code,
        "label": label,
        "amount": round(float(amount), 2),
        "detail": detail,
        "at": datetime.now().strftime("%H:%M:%S"),
    }

    with penalty_lock:
        pending_penalties.setdefault(plate, []).append(record)
        recent_penalties.insert(0, record)
        del recent_penalties[25:]

    try:
        conn = sqlite3.connect("parking.db")
        conn.execute(
            "INSERT INTO penalties (car_plate, code, label, amount, detail) VALUES (?, ?, ?, ?, ?)",
            (plate, code, label, record["amount"], detail),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[PENALTY] DB write failed: {e}")

    print(f"[PENALTY] {plate}: {label} {CURRENCY}{record['amount']:.2f} ({detail})")


def check_parking_penalties(plate, car_type, spot_name):
    """Run at Park/CarIn. Detects bay misuse and stolen bays."""
    if not plate or not spot_name:
        return

    with spot_lock:
        assigned_to = spot_state.get(spot_name)

    # Someone parked in a bay we had reserved for a different car.
    # "PRE_OCCUPIED" is the startup-sync marker, never a real assignment.
    if assigned_to and assigned_to != plate and assigned_to != "PRE_OCCUPIED":
        register_penalty(plate, "SPOT_TAKEN", PENALTY_SPOT_TAKEN,
                         f"Bay {spot_name} was reserved for {assigned_to}")

    category = SPOT_CATEGORY.get(spot_name)
    if category == "Electric" and car_type != "Electric":
        register_penalty(plate, "MISUSE_EV", PENALTY_MISUSE_EV,
                         f"{car_type or 'Non-EV'} car in EV bay {spot_name}")
    elif category == "Accessible" and car_type != "Accessible":
        register_penalty(plate, "MISUSE_OKU", PENALTY_MISUSE_OKU,
                         f"{car_type or 'Non-OKU'} car in OKU bay {spot_name}")


def check_overstay_penalty(plate, minutes_spent):
    """Run at exit, before billing."""
    with penalty_lock:
        planned = planned_duration.get(plate)
    if not planned:
        return

    over = minutes_spent - planned - PENALTY_OVERSTAY_GRACE_MIN
    if over <= 0:
        return

    amount = min(over * PENALTY_OVERSTAY_PER_MIN, PENALTY_OVERSTAY_CAP)
    register_penalty(plate, "OVERSTAY", amount,
                     f"{over} min over a {planned} min booking")


def settle_penalties(plate):
    """Pop and total everything owed by a plate."""
    global total_penalty_revenue
    with penalty_lock:
        items = pending_penalties.pop(plate, [])
        planned_duration.pop(plate, None)
    total = round(sum(i["amount"] for i in items), 2)
    if total:
        total_penalty_revenue += total
    return total, items


# =========================================================
# PUBLIC OCCUPANCY / STATUS AGGREGATION
# =========================================================

def _blank_counts():
    return {"total": 0, "occupied": 0, "available": 0, "out_of_service": 0}


def get_public_status():
    """Everything the public board needs, in one snapshot."""
    with spot_lock:
        state = dict(spot_state)
        health = dict(spot_health_cache)
        gates = dict(gate_status_cache)
        fans = dict(fan_status_cache)
        co = dict(CO_LEVELS)

    overall = {cat: _blank_counts() for cat in CATEGORY_LABELS}
    per_zone = {}

    for zone_name, zone_data in ZONES.items():
        zone_counts = {cat: _blank_counts() for cat in CATEGORY_LABELS}
        for cat, pool in zone_data["parking"].items():
            for spot in pool:
                h = health.get(spot, {})
                down = h.get("broken") or h.get("isUnderMaintenance")
                zone_counts[cat]["total"] += 1
                if down:
                    zone_counts[cat]["out_of_service"] += 1
                elif spot in state:
                    zone_counts[cat]["occupied"] += 1
                else:
                    zone_counts[cat]["available"] += 1
        for cat in CATEGORY_LABELS:
            for k in overall[cat]:
                overall[cat][k] += zone_counts[cat][k]
        per_zone[zone_name] = {
            "counts": zone_counts,
            "co_level": round(co.get(zone_name, 0.0), 1),
            "open": is_zone_entry_healthy(zone_name),
        }

    # Anything a driver would want warned about
    maintenance = []
    for name, st in gates.items():
        if st.get("broken") or st.get("isUnderMaintenance"):
            maintenance.append({
                "name": name, "kind": "Barrier gate",
                "zone": _zone_of_gate(name),
                "status": "Under repair" if st.get("isUnderMaintenance") else "Out of service",
            })
    for name, st in fans.items():
        if st.get("broken") or st.get("isUnderMaintenance"):
            maintenance.append({
                "name": name, "kind": "Ventilation fan",
                "zone": FAN_TO_ZONE.get(name, "-"),
                "status": "Under repair" if st.get("isUnderMaintenance") else "Out of service",
            })
    for name, st in health.items():
        if st.get("broken") or st.get("isUnderMaintenance"):
            cat = SPOT_CATEGORY.get(name)
            maintenance.append({
                "name": name,
                "kind": f"{CATEGORY_LABELS.get(cat, 'Parking')} bay",
                "zone": SPOT_TO_ZONE.get(name, "-"),
                "status": "Under repair" if st.get("isUnderMaintenance") else "Out of service",
            })
    maintenance.sort(key=lambda m: (m["zone"], m["kind"], m["name"]))

    with penalty_lock:
        recent = list(recent_penalties[:8])

    sim_t = current_sim_time()

    return {
        "park_name": PARK_DISPLAY_NAME,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "sim_time": sim_t.strftime("%a %d %b  %H:%M") if sim_t else None,
        "categories": [
            {
                "key": cat,
                "label": CATEGORY_LABELS[cat],
                **overall[cat],
            }
            for cat in ("Any", "Electric", "Accessible")
        ],
        "zones": per_zone,
        "rates": {
            "currency": CURRENCY,
            "parking_per_min": PARKING_RATE_PER_MIN,
            "ev_charging_per_min": EV_CHARGING_RATE_PER_MIN,
            "grace_minutes": FREE_GRACE_MINUTES,
            "ev_total_per_min": PARKING_RATE_PER_MIN + EV_CHARGING_RATE_PER_MIN,
        },
        "penalties": [
            {"code": c, "label": PENALTY_LABELS[c][0], "fee": PENALTY_LABELS[c][1]}
            for c in ("OVERSTAY", "MISUSE_EV", "MISUSE_OKU", "SPOT_TAKEN")
        ],
        "penalties_enforced": PENALTY_ENABLED,
        "maintenance": maintenance,
        "recent_penalties": recent,
    }


VIOLATION_CODES = ("TAILGATING", "BAY_EXCEEDED", "BARRIER_BREAK",
                    "OVERSTAY", "MISUSE_EV", "MISUSE_OKU", "SPOT_TAKEN")


def get_admin_status():
    """Everything the admin dashboard needs, in one snapshot."""
    now_sim = current_sim_time()

    # --- currently parked cars & how long they've been there ---
    with spot_lock:
        plate_to_spot = {p: s for s, p in spot_state.items() if p != "PRE_OCCUPIED"}
    active_cars = []
    for plate, entry_rec in list(car_entry_times.items()):
        spot = plate_to_spot.get(plate)
        minutes = _minutes_parked(entry_rec, now_sim if entry_rec.get("sim") is not None else None)
        active_cars.append({
            "plate": plate,
            "car_type": car_type_map.get(plate, "Standard"),
            "spot": spot or "en route",
            "zone": SPOT_TO_ZONE.get(spot, "-") if spot else "-",
            "minutes_parked": minutes,
        })
    active_cars.sort(key=lambda c: -c["minutes_parked"])

    # --- revenue ---
    with maintenance_lock:
        rev_by_type = dict(revenue_by_type)
    revenue = {
        "total": round(total_revenue, 2),
        "by_type": [
            {"type": t, "amount": round(a, 2)}
            for t, a in sorted(rev_by_type.items(), key=lambda kv: -kv[1])
        ],
    }

    # --- violations / penalties ---
    with penalty_lock:
        all_recent = list(recent_penalties)
    by_code = {c: {"count": 0, "amount": 0.0} for c in VIOLATION_CODES}
    for p in all_recent:
        if p["code"] in by_code:
            by_code[p["code"]]["count"] += 1
            by_code[p["code"]]["amount"] += p["amount"]
    violations = [
        {
            "code": c,
            "label": PENALTY_LABELS[c][0],
            "count": by_code[c]["count"],
            "amount": round(by_code[c]["amount"], 2),
        }
        for c in VIOLATION_CODES
    ]

    # --- maintenance ---
    with maintenance_lock:
        maint_total = round(total_maintenance_cost, 2)
        maint_log = list(maintenance_log[:20])
        maint_by_kind = {}
        for e in maintenance_log:
            maint_by_kind[e["kind"]] = maint_by_kind.get(e["kind"], 0.0) + e["cost"]
        maint_by_kind = [
            {"kind": k, "amount": round(v, 2)}
            for k, v in sorted(maint_by_kind.items(), key=lambda kv: -kv[1])
        ]

    return {
        "park_name": PARK_DISPLAY_NAME,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "sim_time": now_sim.strftime("%a %d %b  %H:%M") if now_sim else None,
        "currency": CURRENCY,
        "active_cars": active_cars,
        "revenue": revenue,
        "penalty_revenue": round(total_penalty_revenue, 2),
        "violations": violations,
        "maintenance": {
            "total_cost": maint_total,
            "by_kind": maint_by_kind,
            "log": maint_log,
        },
    }


def _zone_of_gate(gate_name):
    for z, d in ZONES.items():
        if gate_name in (d["gate_in"], d["gate_out"]):
            return z
    return "-"


# =========================================================
# DATABASE LOGGING
# =========================================================

def init_database():
    conn = sqlite3.connect("parking.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS webhook_events (id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, event_class TEXT, car_plate TEXT, spot_name TEXT, direction TEXT, raw_data TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS login_attempts (id INTEGER PRIMARY KEY, email TEXT, success BOOLEAN, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY, user TEXT, action TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS penalties (
                    id INTEGER PRIMARY KEY,
                    car_plate TEXT,
                    code TEXT,
                    label TEXT,
                    amount REAL,
                    detail TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
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
# DYNAMIC FLEXIBLE ROUTING LOGIC
# =========================================================

def get_zone_by_entry(entry_spot):
    for zone, data in ZONES.items():
        if data["entry_spot"] == entry_spot:
            return zone
    return None

def get_zone_by_exit(exit_spot):
    for zone, data in ZONES.items():
        if data["exit_spot"] == exit_spot:
            return zone
    return None

def find_zone_spot(zone_name, car_type):
    if car_type == "Electric":
        prefs = ["Electric", "Any"]
    elif car_type == "Accessible":
        prefs = ["Accessible", "Any"]
    else:
        prefs = ["Any"]

    zone_parking = ZONES[zone_name]["parking"]
    
    with spot_lock:
        for pref in prefs:
            for spot in zone_parking.get(pref, []):
                if spot not in spot_state:
                    return zone_name, spot
    return None, None

def release_spot(spot_name):
    with spot_lock:
        spot_state.pop(spot_name, None)
        spot_reservations.pop(spot_name, None)
    evaluate_energy_and_lighting()

def auto_park_car(plate, entry_spot, car_type):
    initial_zone = get_zone_by_entry(entry_spot)
    if not initial_zone: 
        return

    all_zones = ["ZONE1", "ZONE2", "ZONE3"]
    zone_sequence = [initial_zone] + [z for z in all_zones if z != initial_zone]

    target_zone = None
    spot = None

    for z in zone_sequence:
        if not is_zone_entry_healthy(z):
            continue

        tz, s = find_zone_spot(z, car_type)
        if s:
            target_zone = tz
            spot = s
            break

    if not spot or not target_zone:
        # No spot anywhere for this car - it's turned away. It is no longer
        # present in any zone, so its motion record must be cleared now;
        # otherwise its entry zone stays lit for the full timeout window
        # on every single rejection.
        motion_stop(plate)
        leave_url = f"{SIMULATOR_BASE_URL}/car/{plate}/goto/leavepark"
        for _ in range(15):
            try: requests.post(leave_url, headers=get_headers(), timeout=1)
            except: pass
            time.sleep(0.2)
        return

    with spot_lock:
        spot_state[spot] = plate
        spot_reservations[spot] = {"plate": plate, "at": time.time()}

    # Replace (not add): the car is now in target_zone only. Using
    # motion_add here kept its original entry zone lit for the entire
    # drive to a different zone, since zones only ever accumulated and
    # were never dropped until the whole trip ended.
    motion_set(plate, [target_zone])

    gate = ZONES[target_zone]["gate_in"]
    gate_last_plate[gate] = plate
    operate_gate(gate, "open")

    update_co_levels(target_zone, 3.0)

    url = f"{SIMULATOR_BASE_URL}/car/{plate}/goto/{spot}"
    for _ in range(15):
        try:
            requests.post(url, headers=get_headers(), timeout=1)
        except:
            pass
        time.sleep(0.2)

def _minutes_parked(entry_rec, exit_sim):
    if entry_rec is None:
        return 1
    if entry_rec.get("sim") is not None and exit_sim is not None:
        secs = (exit_sim - entry_rec["sim"]).total_seconds()
    else:
        with light_lock:
            speed = sim_clock["speed"] or 1.0
        secs = (time.time() - entry_rec["real"]) * speed
    return max(1, int(secs / 60))

def process_exit_and_charge(plate, car_type, exit_spot, exit_sim=None):
    global total_revenue
    
    zone = get_zone_by_exit(exit_spot)
    if not zone: return

    with charge_lock:                            
        if plate in charged_plates:
            return
        charged_plates.add(plate)

    minutes_spent = _minutes_parked(car_entry_times.get(plate), exit_sim)
    
    billable_minutes = max(0, minutes_spent - FREE_GRACE_MINUTES)

    check_overstay_penalty(plate, minutes_spent)
    penalty_total, penalty_items = settle_penalties(plate)

    parking_cost = billable_minutes * PARKING_RATE_PER_MIN
    charging_cost = (billable_minutes * EV_CHARGING_RATE_PER_MIN) if car_type == "Electric" else 0.0

    if PENALTY_ENABLED and penalty_total:
        # Penalties ride on the parking line; the simulator has no penalty field.
        parking_cost += penalty_total
        print(f"[BILLING] {plate}: {CURRENCY}{penalty_total:.2f} in penalties added "
              f"({', '.join(i['label'] for i in penalty_items)})")

    total_bill = parking_cost + charging_cost
    
    try:
        charge_url = f"{SIMULATOR_BASE_URL}/car/{plate}/charge?parkingCost={parking_cost}&chargingCost={charging_cost}"
        requests.post(charge_url, headers=get_headers(), timeout=2)
        total_revenue += total_bill
    except Exception as e:
        with charge_lock:
            charged_plates.discard(plate)

    gate = ZONES[zone]["gate_out"]
    gate_last_plate[gate] = plate
    operate_gate(gate, "open")
    
    update_co_levels(zone, -3.0)

    with maintenance_lock:
        revenue_by_type[car_type or "Standard"] = revenue_by_type.get(car_type or "Standard", 0.0) + total_bill

    leave_url = f"{SIMULATOR_BASE_URL}/car/{plate}/goto/leavepark"
    
    for _ in range(15):
        try:
            requests.post(leave_url, headers=get_headers(), timeout=1)
        except:
            pass
        time.sleep(0.2)
    
    if plate in car_entry_times:
        del car_entry_times[plate]

# =========================================================
# WEBHOOK ENDPOINT
# =========================================================

def verify_signature(req):
    signature = req.headers.get("X-Webhook-Signature")
    if not signature:
        return False
    payload = req.get_data()
    expected = hmac.new(WEBHOOK_SECRET, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

@app.route("/webhook", methods=["POST"])
def receive_webhook():
    if not verify_signature(request):
        log_audit("System", "Received unsigned webhook payload (bypassed)")

    data = request.get_json(silent=True)
    if not data: return jsonify({"status": "invalid json"}), 400

    server_time = data.get("ServerDateTime")
    sim_dt = parse_server_time(server_time) if server_time else None
    if sim_dt:
        evaluate_energy_and_lighting(sim_dt)      

    event_class = data.get("EventClass")
    plate = data.get("CarPlateNumber")
    car_type = data.get("CarType")
    spot_name = data.get("SpotName")
    spot_type = data.get("SpotType")
    direction = data.get("Direction")

    if event_class == "car_spot_action":
        if spot_type == "EntrySpot" and direction == "CarIn":
            car_entry_times[plate] = {"sim": sim_dt, "real": time.time()}
            car_type_map[plate] = car_type
            with charge_lock:
                charged_plates.discard(plate)
            planned = _parse_planned_duration(data)
            with penalty_lock:
                pending_penalties.pop(plate, None)
                if planned:
                    planned_duration[plate] = planned
                else:
                    planned_duration.pop(plate, None)

            # Tailgating: a car entering while the same entry gate is still
            # open for a DIFFERENT plate that hasn't yet cleared the entry
            # spot (no EntrySpot/CarOut + gate-close in between).
            entry_zone = get_zone_by_entry(spot_name)
            if entry_zone:
                gate_in = ZONES[entry_zone]["gate_in"]
                holder = gate_open_for.get(gate_in)
                if holder and holder != plate:
                    register_penalty(plate, "TAILGATING", PENALTY_TAILGATING,
                                     f"Followed {holder} through {gate_in} without its own gate cycle")
                gate_open_for[gate_in] = plate

            motion_set(plate, [entry_zone])
            threading.Thread(target=auto_park_car, args=(plate, spot_name, car_type), daemon=True).start()
            
        elif spot_type == "EntrySpot" and direction == "CarOut":
            zone = get_zone_by_entry(spot_name)
            if zone:
                gate_in = ZONES[zone]["gate_in"]
                gate_open_for.pop(gate_in, None)
                threading.Thread(target=operate_gate, args=(gate_in, "close"), daemon=True).start()

        elif spot_type == "Park" and direction == "CarIn":
            check_parking_penalties(plate, car_type, spot_name)
            # Record the real occupant so the public board reflects reality
            # even for cars that parked without our assignment.
            if spot_name:
                with spot_lock:
                    spot_state[spot_name] = plate
                    spot_reservations.pop(spot_name, None)   # arrival confirmed
            motion_stop(plate)                       
            evaluate_energy_and_lighting()

        elif spot_type == "Park" and direction == "CarOut":
            release_spot(spot_name)
            motion_set(plate, [SPOT_TO_ZONE.get(spot_name)])

        elif spot_type == "ExitSpot" and direction == "CarIn":
            # motion_stop() already ran at Park/CarOut, so this creates a
            # fresh record - motion_set here (not motion_add) for
            # consistency: a car is in exactly one current zone.
            motion_set(plate, [get_zone_by_exit(spot_name)])
            threading.Thread(target=process_exit_and_charge, args=(plate, car_type, spot_name, sim_dt), daemon=True).start()

        elif spot_type == "ExitSpot" and direction == "CarOut":
            motion_stop(plate)
            zone = get_zone_by_exit(spot_name)
            if zone:
                threading.Thread(target=operate_gate, args=(ZONES[zone]["gate_out"], "close"), daemon=True).start()

    elif event_class == "co_level_change":
        zone = data.get("ZoneName")
        level = data.get("Level", 0)
        with spot_lock:
            CO_LEVELS[zone] = float(level)

    return jsonify({"status": "received"}), 200

# =========================================================
# DASHBOARD ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return ("<h1>Level 2 Parking Server Active</h1>"
            "<a href='/live'>Public Board</a> | "
            "<a href='/admin'>Admin Dashboard</a> | "
            "<a href='/events'>Operator Dashboard</a> | "
            "<a href='/energy'>Energy (JSON)</a> | "
            "<a href='/api/public/status'>Public Status (JSON)</a>")


@app.route("/api/public/status", methods=["GET"])
def public_status_api():
    return jsonify(get_public_status())


PUBLIC_BOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{{ park_name }} - Live Parking</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2430; --line:#2a3340;
    --txt:#e6edf3; --muted:#8b949e;
    --ok:#3fb950; --warn:#d29922; --bad:#f85149; --ev:#2f81f7; --oku:#a371f7;
    --pad-t: env(safe-area-inset-top, 0px);
    --pad-b: env(safe-area-inset-bottom, 0px);
    box-sizing:border-box; padding-top:var(--pad-t); padding-bottom:var(--pad-b);
  }
  *,*::before,*::after{box-sizing:inherit}
  html{scroll-padding-top:var(--pad-t)}
  body{margin:0;background:var(--bg);color:var(--txt);
       font-family:"Segoe UI",system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif;
       -webkit-font-smoothing:antialiased}
  .wrap{max-width:1180px;margin:0 auto;padding:20px 16px 48px}

  header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;
         justify-content:space-between;padding-bottom:16px;border-bottom:1px solid var(--line)}
  h1{font-size:26px;margin:0;letter-spacing:.3px}
  .clock{font-variant-numeric:tabular-nums;color:var(--muted);font-size:14px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);
       margin-right:6px;vertical-align:middle}

  h2{font-size:13px;text-transform:uppercase;letter-spacing:1.4px;
     color:var(--muted);margin:30px 0 12px;font-weight:600}

  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(250px,1fr))}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px}

  .cat-name{font-size:14px;font-weight:600;letter-spacing:.4px}
  .cat-name .tag{font-size:11px;padding:2px 8px;border-radius:99px;margin-left:8px;
                 background:var(--panel2);color:var(--muted);vertical-align:middle}
  .big{font-size:56px;line-height:1;font-weight:700;font-variant-numeric:tabular-nums;
       margin:14px 0 2px}
  .of{font-size:15px;color:var(--muted);font-weight:400}
  .sub{font-size:12px;color:var(--muted);margin-top:8px}
  .bar{height:7px;border-radius:99px;background:#22282f;margin-top:14px;overflow:hidden;display:flex}
  .bar i{display:block;height:100%}
  .full .big{color:var(--bad)}
  .low .big{color:var(--warn)}

  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line)}
  th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.8px}
  td.num{font-variant-numeric:tabular-nums;text-align:right}
  tbody tr:last-child td{border-bottom:none}
  .scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}

  .row{display:flex;justify-content:space-between;gap:16px;padding:11px 0;
       border-bottom:1px solid var(--line);font-size:14px}
  .row:last-child{border-bottom:none}
  .row .v{font-variant-numeric:tabular-nums;color:var(--txt);font-weight:600;
          text-align:right;white-space:nowrap}
  .row .k{color:var(--muted)}

  .pill{font-size:11px;padding:3px 9px;border-radius:99px;white-space:nowrap}
  .pill.repair{background:rgba(210,153,34,.15);color:var(--warn)}
  .pill.down{background:rgba(248,81,73,.15);color:var(--bad)}
  .empty{color:var(--muted);font-size:14px;padding:6px 0}
  footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);
         color:var(--muted);font-size:12px;line-height:1.6}
  .banner{background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.35);
          color:var(--warn);border-radius:10px;padding:11px 14px;font-size:13px;margin-top:16px}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1 id="park">Loading…</h1>
    <div class="clock"><span class="dot"></span><span id="clock">connecting</span></div>
  </header>

  <div id="banner"></div>

  <h2>Spaces available now</h2>
  <div class="grid" id="cats"></div>

  <h2>By zone</h2>
  <div class="card scroll">
    <table>
      <thead><tr>
        <th>Zone</th><th class="num">Standard</th><th class="num">EV</th>
        <th class="num">OKU</th><th class="num">Air quality</th><th>Entry</th>
      </tr></thead>
      <tbody id="zones"></tbody>
    </table>
  </div>

  <div class="grid" style="margin-top:30px;grid-template-columns:repeat(auto-fit,minmax(310px,1fr))">
    <div>
      <h2 style="margin-top:0">Parking rates</h2>
      <div class="card" id="rates"></div>
    </div>
    <div>
      <h2 style="margin-top:0">Penalty fees</h2>
      <div class="card" id="pens"></div>
    </div>
  </div>

  <h2>Maintenance in progress</h2>
  <div class="card scroll" id="maint"></div>

  <footer>
    Rates are charged per minute from entry to exit. EV bays bill parking plus charging.
    Please park only in a bay matching your vehicle type.<br>
    Board refreshes every {{ refresh }} seconds.
  </footer>

</div>

<script>
const CATC = {"Standard":"var(--ok)","EV":"var(--ev)","OKU":"var(--oku)"};
const esc = s => String(s==null?"":s).replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function catCard(c){
  const colour = CATC[c.label] || "var(--ok)";
  const usable = Math.max(c.total - c.out_of_service, 0);
  const pctOcc = usable ? (c.occupied/usable*100) : 0;
  const pctOos = c.total ? (c.out_of_service/c.total*100) : 0;
  let cls = "";
  if (c.available === 0) cls = "full";
  else if (usable && c.available/usable <= 0.15) cls = "low";
  const word = c.available === 0 ? "FULL" : "free";
  return `<div class="card ${cls}">
    <div class="cat-name">${esc(c.label)} parking<span class="tag">${esc(c.key)}</span></div>
    <div class="big">${c.available === 0 ? "FULL" : c.available}</div>
    <div class="of">${c.available === 0 ? "no spaces" : word + " of " + usable + " usable bays"}</div>
    <div class="bar">
      <i style="width:${pctOcc.toFixed(1)}%;background:${colour}"></i>
      <i style="width:${pctOos.toFixed(1)}%;background:var(--bad);opacity:.55"></i>
    </div>
    <div class="sub">${c.occupied} occupied · ${c.out_of_service} out of service · ${c.total} total</div>
  </div>`;
}

function zoneRow(name, z){
  const n = k => {
    const c = z.counts[k];
    return `<td class="num">${c.available}<span style="color:var(--muted)"> / ${c.total}</span></td>`;
  };
  const co = z.co_level;
  const col = co >= 60 ? "var(--bad)" : co >= 25 ? "var(--warn)" : "var(--ok)";
  const air = co >= 60 ? "Poor" : co >= 25 ? "Moderate" : "Good";
  return `<tr>
    <td><b>${esc(name)}</b></td>
    ${n("Any")}${n("Electric")}${n("Accessible")}
    <td class="num" style="color:${col}">${air}</td>
    <td>${z.open ? '<span class="pill" style="background:rgba(63,185,80,.15);color:var(--ok)">Open</span>'
                 : '<span class="pill down">Closed</span>'}</td>
  </tr>`;
}

function render(d){
  document.getElementById("park").textContent = d.park_name;
  document.title = d.park_name + " - Live Parking";
  document.getElementById("clock").textContent =
    (d.sim_time ? d.sim_time + "  ·  " : "") + "updated " + d.updated_at;

  document.getElementById("cats").innerHTML = d.categories.map(catCard).join("");

  document.getElementById("zones").innerHTML =
    Object.entries(d.zones).map(([n,z]) => zoneRow(n,z)).join("");

  const r = d.rates, cur = r.currency;
  const rows = [
    ["Standard parking", `${cur} ${r.parking_per_min.toFixed(2)} / min`],
    ["OKU parking", `${cur} ${r.parking_per_min.toFixed(2)} / min`],
    ["EV bay — parking", `${cur} ${r.parking_per_min.toFixed(2)} / min`],
    ["EV bay — charging", `${cur} ${r.ev_charging_per_min.toFixed(2)} / min`],
    ["EV bay — total", `${cur} ${r.ev_total_per_min.toFixed(2)} / min`],
    ["Free grace period", r.grace_minutes > 0 ? `${r.grace_minutes} min` : "None"],
  ];
  document.getElementById("rates").innerHTML = rows.map(
    ([k,v]) => `<div class="row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`
  ).join("");

  document.getElementById("pens").innerHTML = d.penalties.map(
    p => `<div class="row"><span class="k">${esc(p.label)}</span><span class="v">${esc(p.fee)}</span></div>`
  ).join("") + (d.penalties_enforced ? "" :
    `<div class="sub" style="margin-top:12px">Penalties are currently being recorded but not charged.</div>`);

  const m = d.maintenance;
  document.getElementById("maint").innerHTML = m.length ? `<table>
      <thead><tr><th>Component</th><th>Type</th><th>Zone</th><th>Status</th></tr></thead>
      <tbody>${m.map(x => `<tr>
        <td><b>${esc(x.name)}</b></td><td>${esc(x.kind)}</td><td>${esc(x.zone)}</td>
        <td><span class="pill ${x.status === "Under repair" ? "repair" : "down"}">${esc(x.status)}</span></td>
      </tr>`).join("")}</tbody></table>`
    : `<div class="empty">All equipment is operating normally.</div>`;

  const closed = Object.entries(d.zones).filter(([,z]) => !z.open).map(([n]) => n);
  document.getElementById("banner").innerHTML = closed.length
    ? `<div class="banner"><b>Notice:</b> entry to ${closed.join(", ")} is temporarily
       closed for maintenance. Please follow signs to another zone.</div>` : "";
}

async function tick(){
  try{
    const res = await fetch("/api/public/status", {cache:"no-store"});
    if(!res.ok) throw new Error(res.status);
    render(await res.json());
    document.querySelector(".dot").style.background = "var(--ok)";
  }catch(e){
    document.querySelector(".dot").style.background = "var(--bad)";
    document.getElementById("clock").textContent = "reconnecting…";
  }
}
tick();
setInterval(tick, {{ refresh }} * 1000);
</script>
</body>
</html>
"""


@app.route("/live", methods=["GET"])
def public_board():
    return render_template_string(
        PUBLIC_BOARD_HTML,
        park_name=PARK_DISPLAY_NAME,
        refresh=PUBLIC_REFRESH_SEC,
    )

def _admin_authorized(req):
    auth = req.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else None
    return bool(token) and token in ADMIN_SESSIONS


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))

    # Reuses the same operator credentials the server itself uses to
    # authenticate against the simulator - see ADMIN_EMAIL / ADMIN_PASSWORD
    # at the top of this file.
    if hmac.compare_digest(username, ADMIN_EMAIL) and hmac.compare_digest(password, ADMIN_PASSWORD):
        token = secrets.token_hex(24)
        ADMIN_SESSIONS.add(token)
        log_audit(username, "Admin dashboard login")
        return jsonify({"status": "ok", "token": token})

    log_login_attempt(username, False)
    return jsonify({"status": "invalid credentials"}), 401


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else None
    ADMIN_SESSIONS.discard(token)
    return jsonify({"status": "ok"})


@app.route("/api/admin/status", methods=["GET"])
def admin_status_api():
    if not _admin_authorized(request):
        return jsonify({"status": "unauthorized"}), 401
    return jsonify(get_admin_status())


ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{{ park_name }} - Admin</title>
<style>
  :root{
    --bg:#0b0d12; --panel:#151920; --panel2:#1b212b; --line:#262e3a;
    --txt:#eef2f7; --muted:#8b95a5;
    --ok:#3fb950; --warn:#e3a008; --bad:#f0564d; --acc:#5b8cff; --acc2:#a371f7;
    --pad-t: env(safe-area-inset-top, 0px); --pad-b: env(safe-area-inset-bottom, 0px);
    box-sizing:border-box; padding-top:var(--pad-t); padding-bottom:var(--pad-b);
  }
  *,*::before,*::after{box-sizing:inherit}
  body{margin:0;background:var(--bg);color:var(--txt);
       font-family:"Segoe UI",system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif;
       -webkit-font-smoothing:antialiased;min-height:100vh}

  /* ---------- login screen ---------- */
  #loginScreen{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
  .loginCard{width:100%;max-width:340px;background:var(--panel);border:1px solid var(--line);
             border-radius:16px;padding:32px 28px;text-align:center}
  .loginCard .lock{width:46px;height:46px;border-radius:50%;background:var(--panel2);
                    display:flex;align-items:center;justify-content:center;margin:0 auto 16px;font-size:20px}
  .loginCard h1{font-size:19px;margin:0 0 6px}
  .loginCard p{color:var(--muted);font-size:13px;margin:0 0 22px}
  .field{text-align:left;margin-bottom:14px}
  .field label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
  .field input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid var(--line);
               background:var(--bg);color:var(--txt);font-size:14px}
  .field input:focus{outline:none;border-color:var(--acc)}
  #loginBtn{width:100%;padding:12px;border:none;border-radius:9px;background:var(--acc);
            color:#fff;font-size:14px;font-weight:600;cursor:pointer;margin-top:6px}
  #loginBtn:hover{filter:brightness(1.08)}
  #loginErr{color:var(--bad);font-size:12.5px;margin-top:12px;min-height:16px}

  /* ---------- dashboard ---------- */
  #dash{display:none}
  .wrap{max-width:1240px;margin:0 auto;padding:20px 16px 56px}
  header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;
         padding-bottom:16px;border-bottom:1px solid var(--line)}
  header h1{font-size:22px;margin:0}
  header .sub{color:var(--muted);font-size:13px}
  .headRight{display:flex;align-items:center;gap:14px}
  .clock{font-variant-numeric:tabular-nums;color:var(--muted);font-size:13px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px}
  #logoutBtn{background:none;border:1px solid var(--line);color:var(--muted);padding:7px 14px;
             border-radius:8px;font-size:12.5px;cursor:pointer}
  #logoutBtn:hover{color:var(--txt);border-color:var(--acc)}

  h2{font-size:12.5px;text-transform:uppercase;letter-spacing:1.3px;color:var(--muted);
     margin:32px 0 12px;font-weight:700}
  .kpis{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
  .kpi .lbl{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
  .kpi .val{font-size:32px;font-weight:700;margin-top:8px;font-variant-numeric:tabular-nums}
  .kpi .val.warn{color:var(--warn)} .kpi .val.bad{color:var(--bad)} .kpi .val.ok{color:var(--ok)}
  .kpi .foot{font-size:12px;color:var(--muted);margin-top:6px}

  .grid2{display:grid;gap:16px;grid-template-columns:2fr 1fr}
  @media(max-width:880px){.grid2{grid-template-columns:1fr}}

  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}

  .boxGrid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(168px,1fr))}
  .carBox{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px}
  .carBox .plate{font-weight:700;font-size:15px;letter-spacing:.4px}
  .carBox .tag{display:inline-block;font-size:10.5px;padding:2px 8px;border-radius:99px;
               margin-top:6px;background:rgba(91,140,255,.15);color:var(--acc)}
  .carBox .tag.ev{background:rgba(91,140,255,.15);color:var(--acc)}
  .carBox .tag.acc{background:rgba(163,113,247,.15);color:var(--acc2)}
  .carBox .dur{font-size:22px;font-weight:700;margin-top:12px;font-variant-numeric:tabular-nums}
  .carBox .loc{font-size:11.5px;color:var(--muted);margin-top:4px}
  .carBox.over .dur{color:var(--warn)}
  .empty{color:var(--muted);font-size:13.5px;padding:10px 0}

  table{width:100%;border-collapse:collapse;font-size:13.5px}
  th,td{padding:10px 10px;text-align:left;border-bottom:1px solid var(--line)}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.7px}
  td.num{font-variant-numeric:tabular-nums;text-align:right}
  tbody tr:last-child td{border-bottom:none}
  .scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}

  .barRow{margin-bottom:14px}
  .barRow:last-child{margin-bottom:0}
  .barRow .top{display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px}
  .barRow .top .amt{font-weight:600;font-variant-numeric:tabular-nums}
  .barTrack{height:8px;border-radius:99px;background:var(--panel2);overflow:hidden}
  .barTrack i{display:block;height:100%;background:var(--acc);border-radius:99px}

  .pill{font-size:10.5px;padding:3px 9px;border-radius:99px;white-space:nowrap;font-weight:600}
  .pill.v-bad{background:rgba(240,86,77,.15);color:var(--bad)}
  .pill.v-warn{background:rgba(227,160,8,.15);color:var(--warn)}

  footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);
         color:var(--muted);font-size:12px}
</style>
</head>
<body>

<div id="loginScreen">
  <div class="loginCard">
    <div class="lock">&#128274;</div>
    <h1>{{ park_name }} Admin</h1>
    <p>Sign in to view operations, revenue and maintenance data.</p>
    <form id="loginForm">
      <div class="field">
        <label for="user">Username</label>
        <input id="user" type="text" autocomplete="username" required>
      </div>
      <div class="field">
        <label for="pass">Password</label>
        <input id="pass" type="password" autocomplete="current-password" required>
      </div>
      <button id="loginBtn" type="submit">Sign in</button>
      <div id="loginErr"></div>
    </form>
  </div>
</div>

<div id="dash">
  <div class="wrap">
    <header>
      <div>
        <h1 id="parkName">{{ park_name }} — Admin</h1>
        <div class="sub" id="simClock"></div>
      </div>
      <div class="headRight">
        <div class="clock"><span class="dot"></span><span id="updated">connecting</span></div>
        <button id="logoutBtn">Sign out</button>
      </div>
    </header>

    <h2>Overview</h2>
    <div class="kpis" id="kpis"></div>

    <h2>Currently parked — duration</h2>
    <div class="card">
      <div class="boxGrid" id="carBoxes"></div>
    </div>

    <div class="grid2">
      <div>
        <h2>Violations &amp; penalties</h2>
        <div class="card scroll">
          <table>
            <thead><tr><th>Violation</th><th class="num">Count</th><th class="num">Total fined</th></tr></thead>
            <tbody id="violTable"></tbody>
          </table>
        </div>

        <h2>Maintenance repair log</h2>
        <div class="card scroll">
          <table>
            <thead><tr><th>Component</th><th>Type</th><th>Zone</th><th>Reason</th><th class="num">Cost</th><th>Time</th></tr></thead>
            <tbody id="maintTable"></tbody>
          </table>
        </div>
      </div>

      <div>
        <h2>Revenue by vehicle type</h2>
        <div class="card" id="revByType"></div>

        <h2 style="margin-top:24px">Maintenance cost by equipment</h2>
        <div class="card" id="maintByKind"></div>
      </div>
    </div>

    <footer>Dashboard refreshes every {{ refresh }} seconds. Figures reset when the server restarts.</footer>
  </div>
</div>

<script>
const esc = s => String(s==null?"":s).replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = (cur, n) => `${cur} ${Number(n||0).toFixed(2)}`;

let TOKEN = null;   // held only in memory for this page load - never persisted
let poller = null;

function showDash(show){
  document.getElementById("loginScreen").style.display = show ? "none" : "flex";
  document.getElementById("dash").style.display = show ? "block" : "none";
}

document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = document.getElementById("user").value;
  const password = document.getElementById("pass").value;
  const errEl = document.getElementById("loginErr");
  errEl.textContent = "";
  try {
    const res = await fetch("/api/admin/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({username, password})
    });
    const data = await res.json();
    if (res.ok && data.token) {
      TOKEN = data.token;
      showDash(true);
      tick();
      poller = setInterval(tick, {{ refresh }} * 1000);
    } else {
      errEl.textContent = "Incorrect username or password.";
    }
  } catch (err) {
    errEl.textContent = "Could not reach the server. Is it running?";
  }
});

document.getElementById("logoutBtn").addEventListener("click", async () => {
  if (poller) clearInterval(poller);
  try {
    await fetch("/api/admin/logout", {method: "POST", headers: {"Authorization": "Bearer " + TOKEN}});
  } catch(e) {}
  TOKEN = null;
  document.getElementById("pass").value = "";
  showDash(false);
});

function kpiCard(lbl, val, foot, cls){
  return `<div class="kpi"><div class="lbl">${esc(lbl)}</div>
    <div class="val ${cls||''}">${val}</div>
    <div class="foot">${esc(foot||'')}</div></div>`;
}

function fmtDur(min){
  if (min < 60) return min + " min";
  const h = Math.floor(min/60), m = min % 60;
  return h + "h " + m + "m";
}

function carBox(c){
  const over = c.minutes_parked >= 180;
  const tagCls = c.car_type === "Electric" ? "ev" : c.car_type === "Accessible" ? "acc" : "";
  return `<div class="carBox ${over ? 'over' : ''}">
    <div class="plate">${esc(c.plate)}</div>
    <span class="tag ${tagCls}">${esc(c.car_type || 'Standard')}</span>
    <div class="dur">${fmtDur(c.minutes_parked)}</div>
    <div class="loc">${esc(c.spot)} · ${esc(c.zone)}</div>
  </div>`;
}

function render(d){
  document.getElementById("updated").textContent = "updated " + d.updated_at;
  document.getElementById("simClock").textContent = d.sim_time ? "Sim time: " + d.sim_time : "";
  const cur = d.currency;

  const violTotal = d.violations.reduce((s,v)=>s+v.amount,0);
  document.getElementById("kpis").innerHTML = [
    kpiCard("Total revenue", money(cur, d.revenue.total), "Parking + charging + penalties billed", "ok"),
    kpiCard("Cars parked now", d.active_cars.length, "Live occupancy"),
    kpiCard("Penalty revenue", money(cur, d.penalty_revenue), "From all violations", violTotal>0?"warn":""),
    kpiCard("Maintenance cost", money(cur, d.maintenance.total_cost), "Repairs billed to date", d.maintenance.total_cost>0?"bad":""),
  ].join("");

  document.getElementById("carBoxes").innerHTML = d.active_cars.length
    ? d.active_cars.map(carBox).join("")
    : `<div class="empty">No cars currently parked.</div>`;

  document.getElementById("violTable").innerHTML = d.violations.map(v => `<tr>
      <td>${v.count > 0 ? `<span class="pill v-bad">${esc(v.label)}</span>` : esc(v.label)}</td>
      <td class="num">${v.count}</td>
      <td class="num">${money(cur, v.amount)}</td>
    </tr>`).join("");

  const m = d.maintenance.log;
  document.getElementById("maintTable").innerHTML = m.length ? m.map(x => `<tr>
      <td><b>${esc(x.name)}</b></td><td>${esc(x.kind)}</td><td>${esc(x.zone)}</td>
      <td>${esc(x.reason)}</td><td class="num">${money(cur, x.cost)}</td><td>${esc(x.at)}</td>
    </tr>`).join("") : `<tr><td colspan="6" class="empty">No repairs logged yet.</td></tr>`;

  const rt = d.revenue.by_type;
  const maxRt = Math.max(1, ...rt.map(x=>x.amount));
  document.getElementById("revByType").innerHTML = rt.length ? rt.map(x => `
    <div class="barRow">
      <div class="top"><span>${esc(x.type)}</span><span class="amt">${money(cur, x.amount)}</span></div>
      <div class="barTrack"><i style="width:${(x.amount/maxRt*100).toFixed(1)}%"></i></div>
    </div>`).join("") : `<div class="empty">No revenue billed yet.</div>`;

  const mk = d.maintenance.by_kind;
  const maxMk = Math.max(1, ...mk.map(x=>x.amount));
  document.getElementById("maintByKind").innerHTML = mk.length ? mk.map(x => `
    <div class="barRow">
      <div class="top"><span>${esc(x.kind)}</span><span class="amt">${money(cur, x.amount)}</span></div>
      <div class="barTrack"><i style="width:${(x.amount/maxMk*100).toFixed(1)}%;background:var(--acc2)"></i></div>
    </div>`).join("") : `<div class="empty">No repairs logged yet.</div>`;
}

async function tick(){
  if (!TOKEN) return;
  try{
    const res = await fetch("/api/admin/status", {cache:"no-store", headers:{"Authorization":"Bearer "+TOKEN}});
    if (res.status === 401) {
      if (poller) clearInterval(poller);
      TOKEN = null;
      showDash(false);
      document.getElementById("loginErr").textContent = "Session expired, please sign in again.";
      return;
    }
    if(!res.ok) throw new Error(res.status);
    render(await res.json());
    document.querySelector("#dash .dot").style.background = "var(--ok)";
  }catch(e){
    document.querySelector("#dash .dot").style.background = "var(--bad)";
    document.getElementById("updated").textContent = "reconnecting…";
  }
}
</script>
</body>
</html>
"""


@app.route("/admin", methods=["GET"])
def admin_dashboard():
    return render_template_string(
        ADMIN_HTML,
        park_name=PARK_DISPLAY_NAME,
        refresh=PUBLIC_REFRESH_SEC,
    )


@app.route("/energy", methods=["GET"])
def energy_endpoint():
    return jsonify(get_energy_summary())

@app.route("/events", methods=["GET"])
def events_page():
    conn = sqlite3.connect("parking.db")
    conn.row_factory = sqlite3.Row
    logins = conn.execute("SELECT * FROM login_attempts ORDER BY id DESC LIMIT 3").fetchall()
    conn.close()

    html = """
    <html><head><title>Dashboard</title></head><body>
    <h1>Level 2 Dashboard</h1>
    <h3>Current Total Revenue: ${{ total_revenue }}</h3>

    <h3>Energy & Environment Metrics</h3>
    <ul>
      <li>Sim time: {{ e.sim_time }} (measured speed: {{ e.sim_speed_measured }}x)</li>
      <li>Lighting Energy Consumed: <b>{{ e.total_lighting_kwh }} kWh</b></li>
      <li>Exhaust Fans Energy Consumed: <b>{{ e.total_fans_kwh }} kWh</b> (Active fans now: {{ e.active_fans_now }})</li>
      <li>Lights on now: {{ e.lights_on }} / {{ e.lights_total }}</li>
    </ul>

    <h3>Current Zone CO Levels</h3>
    <ul>
      {% for z, lvl in co_levels.items() %}
      <li>{{ z }}: {{ "%.1f"|format(lvl) }}%</li>
      {% endfor %}
    </ul>

    <h3>Recent Logins</h3>
    <ul>{% for l in logins %}<li>{{ l['email'] }} - {{ 'Success' if l['success'] else 'Failed' }}</li>{% endfor %}</ul>
    </body></html>
    """
    return render_template_string(html, logins=logins, total_revenue=total_revenue, e=get_energy_summary(), co_levels=CO_LEVELS)

if __name__ == "__main__":
    init_database()
    init_light_inventory()      
    threading.Thread(target=light_ticker, daemon=True).start()
    authenticate_and_sync()
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)