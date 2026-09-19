from flask import Flask, request, jsonify, render_template_string
import sqlite3
import json


app = Flask(__name__)

last_sequence_id = None


# =========================================================
# DATABASE SETUP
# =========================================================

def init_database():
    connection = sqlite3.connect("parking.db")
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS webhook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE,
            sequence_id INTEGER,
            event_class TEXT,
            car_plate TEXT,
            car_type TEXT,
            spot_name TEXT,
            spot_type TEXT,
            direction TEXT,
            amount TEXT,
            reason TEXT,
            server_datetime TEXT,
            raw_data TEXT
        )
    """)

    connection.commit()
    connection.close()


# =========================================================
# CHECK FOR DUPLICATE EVENT
# =========================================================

def event_already_exists(event_id):

    if event_id is None:
        return False

    connection = sqlite3.connect("parking.db")
    cursor = connection.cursor()

    cursor.execute(
        "SELECT event_id FROM webhook_events WHERE event_id = ?",
        (event_id,)
    )

    result = cursor.fetchone()

    connection.close()

    return result is not None


# =========================================================
# SAVE EVENT
# =========================================================

def save_event(data):

    connection = sqlite3.connect("parking.db")
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO webhook_events (
            event_id,
            sequence_id,
            event_class,
            car_plate,
            car_type,
            spot_name,
            spot_type,
            direction,
            amount,
            reason,
            server_datetime,
            raw_data
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("EventId"),
        data.get("SequenceId"),
        data.get("EventClass"),
        data.get("CarPlateNumber"),
        data.get("CarType"),
        data.get("SpotName"),
        data.get("SpotType"),
        data.get("Direction"),
        data.get("Amount"),
        data.get("Reason"),
        data.get("ServerDateTime"),
        json.dumps(data)
    ))

    connection.commit()
    connection.close()


# =========================================================
# READ RECENT EVENTS
# =========================================================

def get_recent_events():

    connection = sqlite3.connect("parking.db")

    connection.row_factory = sqlite3.Row

    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            id,
            sequence_id,
            event_class,
            car_plate,
            car_type,
            spot_name,
            spot_type,
            direction,
            amount,
            reason,
            server_datetime
        FROM webhook_events
        ORDER BY id DESC
        LIMIT 100
    """)

    events = cursor.fetchall()

    connection.close()

    return events


# =========================================================
# HOME PAGE
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return """
        <h1>NextGen Parking Webhook</h1>
        <p>Webhook server is ONLINE.</p>
        <p><a href="/events">View Stored Events</a></p>
    """


# =========================================================
# EVENTS PAGE
# =========================================================

@app.route("/events", methods=["GET"])
def events_page():

    events = get_recent_events()

    page = """
    <!DOCTYPE html>

    <html>

    <meta http-equiv="refresh" content="1">

    <head>

        <title>NextGen Parking Events</title>

        <style>

            body {
                font-family: Arial, sans-serif;
                margin: 30px;
                background-color: #f5f5f5;
            }

            h1 {
                margin-bottom: 5px;
            }

            table {
                width: 100%;
                border-collapse: collapse;
                background-color: white;
                margin-top: 20px;
            }

            th, td {
                border: 1px solid #dddddd;
                padding: 10px;
                text-align: left;
            }

            th {
                background-color: #222222;
                color: white;
            }

            tr:nth-child(even) {
                background-color: #eeeeee;
            }

        </style>

    </head>


    <body>

        <h1>NextGen Parking Event Log</h1>

        <p>
            Latest webhook events received from the simulator.
        </p>


        <table>

            <tr>
                <th>ID</th>
                <th>Sequence</th>
                <th>Event</th>
                <th>Car Plate</th>
                <th>Car Type</th>
                <th>Spot</th>
                <th>Spot Type</th>
                <th>Direction</th>
                <th>Amount</th>
                <th>Reason</th>
                <th>Time</th>
            </tr>


            {% for event in events %}

            <tr>

                <td>{{ event["id"] }}</td>

                <td>
                    {{ event["sequence_id"] or "-" }}
                </td>

                <td>
                    {{ event["event_class"] or "-" }}
                </td>

                <td>
                    {{ event["car_plate"] or "-" }}
                </td>

                <td>
                    {{ event["car_type"] or "-" }}
                </td>

                <td>
                    {{ event["spot_name"] or "-" }}
                </td>

                <td>
                    {{ event["spot_type"] or "-" }}
                </td>

                <td>
                    {{ event["direction"] or "-" }}
                </td>

                <td>
                    {{ event["amount"] or "-" }}
                </td>

                <td>
                    {{ event["reason"] or "-" }}
                </td>

                <td>
                    {{ event["server_datetime"] or "-" }}
                </td>

            </tr>

            {% endfor %}


        </table>

    </body>

    </html>
    """

    return render_template_string(
        page,
        events=events
    )


# =========================================================
# WEBHOOK RECEIVER
# =========================================================

@app.route("/webhook", methods=["POST"])
def receive_webhook():

    global last_sequence_id

    data = request.get_json(silent=True)


    # -----------------------------------------------------
    # INVALID JSON CHECK
    # -----------------------------------------------------

    if data is None:

        print("INVALID WEBHOOK DATA")

        return jsonify({
            "status": "invalid json"
        }), 400


    event_id = data.get("EventId")

    sequence_id = data.get("SequenceId")

    event_class = data.get("EventClass")


    # -----------------------------------------------------
    # DUPLICATE PROTECTION
    # -----------------------------------------------------

    if event_already_exists(event_id):

        print("\n============================")
        print("DUPLICATE EVENT IGNORED")
        print("Event ID:", event_id)
        print("============================\n")

        return jsonify({
            "status": "duplicate ignored"
        }), 200


    # -----------------------------------------------------
    # SEQUENCE CHECK
    # -----------------------------------------------------

    if sequence_id is not None:

        if last_sequence_id is not None:

            expected_sequence = last_sequence_id + 1

            if sequence_id != expected_sequence:

                print("\nWARNING: EVENT SEQUENCE PROBLEM")

                print(
                    "Expected:",
                    expected_sequence
                )

                print(
                    "Received:",
                    sequence_id
                )


        last_sequence_id = sequence_id


    # -----------------------------------------------------
    # SAVE EVENT
    # -----------------------------------------------------

    try:

        save_event(data)

        print("Event saved to database.")

    except Exception as error:

        print(
            "DATABASE ERROR:",
            error
        )


    # -----------------------------------------------------
    # DISPLAY EVENT
    # -----------------------------------------------------

    print("\n============================")
    print("WEBHOOK RECEIVED!")
    print("============================")


    # =====================================================
    # CAR EVENTS
    # =====================================================

    if event_class == "car_spot_action":

        plate = data.get("CarPlateNumber")

        car_type = data.get("CarType")

        spot_name = data.get("SpotName")

        spot_type = data.get("SpotType")

        direction = data.get("Direction")


        if (
            spot_type == "EntrySpot"
            and direction == "CarIn"
        ):

            print("NEW CAR ARRIVED")

            print(
                "Plate    :",
                plate
            )

            print(
                "Car Type :",
                car_type
            )


        elif (
            spot_type == "EntrySpot"
            and direction == "CarOut"
        ):

            print("CAR LEFT ENTRY")

            print(
                "Plate :",
                plate
            )


        elif (
            spot_type == "Park"
            and direction == "CarIn"
        ):

            print("CAR PARKED")

            print(
                "Plate :",
                plate
            )

            print(
                "Spot  :",
                spot_name
            )


        elif (
            spot_type == "Park"
            and direction == "CarOut"
        ):

            print("CAR LEFT PARKING SPOT")

            print(
                "Plate :",
                plate
            )

            print(
                "Spot  :",
                spot_name
            )


        elif (
            spot_type == "ExitSpot"
            and direction == "CarIn"
        ):

            print("CAR ARRIVED AT EXIT")

            print(
                "Plate :",
                plate
            )


        elif (
            spot_type == "ExitSpot"
            and direction == "CarOut"
        ):

            print("CAR LEFT THE CAR PARK")

            print(
                "Plate :",
                plate
            )


        else:

            print("UNKNOWN CAR EVENT")

            print(data)


    # =====================================================
    # PAYMENT EVENT
    # =====================================================

    elif event_class == "payment_made":

        print("PAYMENT RECEIVED")

        print(
            "Plate  :",
            data.get("CarPlateNumber")
        )

        print(
            "Amount :",
            data.get("Amount")
        )

        print(
            "Reason :",
            data.get("Reason")
        )


    # =====================================================
    # GATE EVENT
    # =====================================================

    elif event_class == "gate_action":

        print("GATE STATUS CHANGED")

        print(
            "Gate   :",
            data.get("Name")
        )

        print(
            "Action :",
            data.get("Action")
        )


    # =====================================================
    # PENALTY
    # =====================================================

    elif event_class == "penalty":

        print("PENALTY RECEIVED!")

        print(
            "Reason :",
            data.get("Reason")
        )

        print(
            "Fine   :",
            data.get("FineAmount")
        )


    # =====================================================
    # COMPONENT BROKEN
    # =====================================================

    elif event_class == "component_broken":

        print("COMPONENT BROKEN")

        print(
            "Type :",
            data.get("Type")
        )

        print(
            "Name :",
            data.get("Name")
        )


    # =====================================================
    # COMPONENT FIXED
    # =====================================================

    elif event_class == "component_fixed":

        print("COMPONENT FIXED")

        print(
            "Type :",
            data.get("Type")
        )

        print(
            "Name :",
            data.get("Name")
        )


    # =====================================================
    # CARBON MONOXIDE
    # =====================================================

    elif event_class == "carbon_monoxide_event":

        print("CARBON MONOXIDE ALERT")

        print(
            "Zone   :",
            data.get("ZoneName")
        )

        print(
            "CO     :",
            data.get("CarbonMonoxideLevel")
        )

        print(
            "Danger :",
            data.get("DangerLevel")
        )


    # =====================================================
    # TEST
    # =====================================================

    elif event_class == "test_webhook":

        print("TEST WEBHOOK SUCCESS")


    # =====================================================
    # UNKNOWN
    # =====================================================

    else:

        print("OTHER EVENT")

        print(data)


    print("============================\n")


    return jsonify({
        "status": "received"
    }), 200


# =========================================================
# START SERVER
# =========================================================

if __name__ == "__main__":

    init_database()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )