import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, current_app
from flask_sqlalchemy import SQLAlchemy
import requests
from functools import wraps
from dotenv import load_dotenv

load_dotenv()
fedex_bearer_token = os.getenv("FEDEX_BEARER_TOKEN")
onasset_bearer_token = os.getenv("ONASSET_BEARER_TOKEN")
# Setup app and config with absolute path to avoid SQLite errors
basedir = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(basedir, "instance")
os.makedirs(db_path, exist_ok=True)

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"sqlite:///{os.path.join(db_path, 'zoomlogi.db')}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["BEARER_TOKEN"] = os.getenv("BEARER_TOKEN", "default_bearer_token")

db = SQLAlchemy(app)


# -------------------- Models -------------------- #


class Shipment(db.Model):
    __tablename__ = "shipments"
    id = db.Column(db.Integer, primary_key=True)
    tracking_number = db.Column(db.String, unique=True, nullable=False)
    origin = db.Column(db.String)
    destination = db.Column(db.String)
    current_status = db.Column(db.String)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    status_history = db.relationship("StatusHistory", backref="shipment", lazy=True)
    sensor_data = db.relationship("SensorData", backref="shipment", lazy=True)
    temperature_alerts = db.relationship(
        "TemperatureAlert", backref="shipment", lazy=True
    )


class StatusHistory(db.Model):
    __tablename__ = "status_history"
    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipments.id"), nullable=False)
    status = db.Column(db.String)
    location = db.Column(db.String)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class SensorData(db.Model):
    __tablename__ = "sensor_data"
    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipments.id"), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    temperature = db.Column(db.Float)
    humidity = db.Column(db.Float)
    location = db.Column(db.String)


class TemperatureAlert(db.Model):
    __tablename__ = "temperature_alerts"
    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipments.id"), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    temperature = db.Column(db.Float)
    alert_type = db.Column(db.String)  # e.g. 'below_min' or 'above_max'


class Config(db.Model):
    __tablename__ = "config"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String, unique=True, nullable=False)
    value = db.Column(db.String)


# -------------------- Helper functions -------------------- #


def get_temp_range():
    min_temp = Config.query.filter_by(key="min_temperature").first()
    max_temp = Config.query.filter_by(key="max_temperature").first()
    return (
        float(min_temp.value) if min_temp else None,
        float(max_temp.value) if max_temp else None,
    )


def create_temp_alert_if_needed(shipment_id, temperature):
    min_temp, max_temp = get_temp_range()
    alert = None

    if min_temp is not None and temperature < min_temp:
        alert = TemperatureAlert(
            shipment_id=shipment_id, temperature=temperature, alert_type="below_min"
        )
    elif max_temp is not None and temperature > max_temp:
        alert = TemperatureAlert(
            shipment_id=shipment_id, temperature=temperature, alert_type="above_max"
        )

    if alert:
        db.session.add(alert)
        status_event = StatusHistory(
            shipment_id=shipment_id,
            status=f"Temperature excursion: {alert.alert_type}",
            location=None,
            timestamp=datetime.utcnow(),
        )
        db.session.add(status_event)
        db.session.commit()


# -------------------- External API functions -------------------- #


def get_fedex_tracking(fedex_bearer_token, tracking_number):
    url = "https://apis.fedex.com/track/v1/trackingnumbers"
    headers = {
        "Authorization": f"Bearer {fedex_bearer_token}",
        "Content-Type": "application/json",
    }
    data = {
        "includeDetailedScans": True,
        "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": tracking_number}}],
    }
    response = requests.post(url, json=data, headers=headers)
    response.raise_for_status()
    return response.json()


def get_onasset_reports(onasset_bearer_token, sensor_id, from_time, to_time):
    url = f"https://oainsightapi.onasset.com/rest/2/sentry500s/{sensor_id}/reports?from={from_time}&to={to_time}"
    headers = {"Authorization": f"Bearer {onasset_bearer_token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


# Bearer Token Validation for api
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify(
                {"error": "Authorization header with Bearer token required"}
            ), 401

        token = auth_header.split(" ")[1]
        if token != app.config["BEARER_TOKEN"]:
            return jsonify({"error": "Invalid or missing bearer token"}), 403

        return f(*args, **kwargs)

    return decorated


# -------------------- Routes -------------------- #


@app.route("/ingest", methods=["POST"])
# @token_required
def ingest_data():
    data = request.json
    required = [
        "fedex_bearer_token",
        "onasset_token",
        "tracking_number",
        "sensor_id",
    ]
    if not data or any(k not in data for k in required):
        return jsonify({"error": f"Missing one or more required keys: {required}"}), 400

    fedex_bearer_token = data["fedex_bearer_token"]
    onasset_token = data["onasset_token"]
    tracking_number = data["tracking_number"]
    sensor_id = data["sensor_id"]

    temp_min = data.get("temp_min")
    temp_max = data.get("temp_max")
    if temp_min is None or temp_max is None:
        config_min, config_max = get_temp_range()
        temp_min = temp_min if temp_min is not None else config_min
        temp_max = temp_max if temp_max is not None else config_max

    try:
        # Use bearer tokens directly to call external APIs
        fedex_data = get_fedex_tracking(fedex_bearer_token, tracking_number)
        results = fedex_data.get("output", {}).get("completeTrackResults", [])
        if not results:
            return jsonify({"error": "No tracking results from FedEx"}), 404
        track_info = results[0]

        origin = track_info.get("originLocation", {}).get("address", {}).get("city")
        destination = (
            track_info.get("destinationLocation", {}).get("address", {}).get("city")
        )
        current_status = track_info.get("latestStatusDetail", {}).get("statusByLocale")

        # Shipment record create/update
        shipment = Shipment.query.filter_by(tracking_number=tracking_number).first()
        if not shipment:
            shipment = Shipment(
                tracking_number=tracking_number,
                origin=origin,
                destination=destination,
                current_status=current_status,
            )
            db.session.add(shipment)
            db.session.commit()
        else:
            shipment.origin = origin
            shipment.destination = destination
            shipment.current_status = current_status
            db.session.commit()

        # Add status history
        scan_events = track_info.get("scanEvents", [])
        for event in scan_events:
            status = event.get("status", "Unknown")
            location = event.get("scanLocation", {}).get("city")
            timestamp_str = event.get("dateScan")
            timestamp = (
                datetime.fromisoformat(timestamp_str.rstrip("Z"))
                if timestamp_str
                else datetime.utcnow()
            )

            exists = StatusHistory.query.filter_by(
                shipment_id=shipment.id,
                status=status,
                location=location,
                timestamp=timestamp,
            ).first()
            if not exists:
                db.session.add(
                    StatusHistory(
                        shipment_id=shipment.id,
                        status=status,
                        location=location,
                        timestamp=timestamp,
                    )
                )
        db.session.commit()

        # OnAsset API calls
        from_time = (datetime.utcnow() - timedelta(days=7)).isoformat() + "Z"
        to_time = datetime.utcnow().isoformat() + "Z"
        onasset_data = get_onasset_reports(onasset_token, sensor_id, from_time, to_time)

        reports = onasset_data.get("reports", [])
        for rec in reports:
            timestamp_str = rec.get("timestamp")
            location = rec.get("location")
            temperature = rec.get("temperature")
            humidity = rec.get("humidity")

            if not timestamp_str or temperature is None:
                continue

            timestamp = datetime.fromisoformat(timestamp_str.rstrip("Z"))

            exists = SensorData.query.filter_by(
                shipment_id=shipment.id,
                timestamp=timestamp,
                temperature=temperature,
                humidity=humidity,
                location=location,
            ).first()
            if not exists:
                sensor_record = SensorData(
                    shipment_id=shipment.id,
                    timestamp=timestamp,
                    temperature=temperature,
                    humidity=humidity,
                    location=location,
                )
                db.session.add(sensor_record)

                # Temperature alert check
                if temp_min is not None and temperature < temp_min:
                    db.session.add(
                        TemperatureAlert(
                            shipment_id=shipment.id,
                            timestamp=timestamp,
                            temperature=temperature,
                            alert_type="below_min",
                        )
                    )
                    db.session.add(
                        StatusHistory(
                            shipment_id=shipment.id,
                            status=f"Temperature excursion below minimum: {temperature}°C",
                            location=location,
                            timestamp=timestamp,
                        )
                    )
                elif temp_max is not None and temperature > temp_max:
                    db.session.add(
                        TemperatureAlert(
                            shipment_id=shipment.id,
                            timestamp=timestamp,
                            temperature=temperature,
                            alert_type="above_max",
                        )
                    )
                    db.session.add(
                        StatusHistory(
                            shipment_id=shipment.id,
                            status=f"Temperature excursion above maximum: {temperature}°C",
                            location=location,
                            timestamp=timestamp,
                        )
                    )
        db.session.commit()

        return jsonify({"message": "Data ingested successfully"}), 200

    except requests.HTTPError as e:
        return jsonify({"error": f"HTTP error from external API: {e}"}), 502
    except Exception as e:
        current_app.logger.error(f"Ingestion error: {e}")
        return jsonify({"error": f"Internal server error: {e}"}), 500


# Apply @token_required to other routes as needed


@app.route("/fetch-shipments", methods=["POST"])
def fetch_shipments():
    api_url = request.json.get("api_url")
    if not api_url:
        return jsonify({"error": "API URL is required"}), 400

    try:
        fetch_shipment_data_from_api(api_url)
        return jsonify(
            {"message": "Shipment data fetched and added to the database."}
        ), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/shipments", methods=["POST"])
def create_shipment():
    data = request.get_json()
    if not data or "tracking_number" not in data:
        return jsonify({"error": "Tracking number required"}), 400
    if Shipment.query.filter_by(tracking_number=data["tracking_number"]).first():
        return jsonify({"error": "Shipment already exists"}), 400

    shipment = Shipment(
        tracking_number=data["tracking_number"],
        origin=data.get("origin"),
        destination=data.get("destination"),
        current_status=data.get("current_status"),
    )
    db.session.add(shipment)
    db.session.commit()
    return jsonify(
        {"id": shipment.id, "tracking_number": shipment.tracking_number}
    ), 201


@app.route("/shipments/<tracking_number>", methods=["GET"])
def get_shipment(tracking_number):
    shipment = Shipment.query.filter_by(tracking_number=tracking_number).first_or_404()
    return jsonify(
        {
            "id": shipment.id,
            "tracking_number": shipment.tracking_number,
            "origin": shipment.origin,
            "destination": shipment.destination,
            "current_status": shipment.current_status,
            "created_at": shipment.created_at.isoformat(),
            "updated_at": shipment.updated_at.isoformat(),
        }
    )


@app.route("/shipments/<tracking_number>/status", methods=["GET"])
def get_status_history(tracking_number):
    shipment = Shipment.query.filter_by(tracking_number=tracking_number).first_or_404()
    statuses = (
        StatusHistory.query.filter_by(shipment_id=shipment.id)
        .order_by(StatusHistory.timestamp)
        .all()
    )
    return jsonify(
        [
            {
                "status": s.status,
                "location": s.location,
                "timestamp": s.timestamp.isoformat(),
            }
            for s in statuses
        ]
    )


@app.route("/shipments/<tracking_number>/status", methods=["POST"])
def add_status(tracking_number):
    shipment = Shipment.query.filter_by(tracking_number=tracking_number).first_or_404()
    data = request.get_json()
    if not data or "status" not in data:
        return jsonify({"error": "Status required"}), 400

    timestamp = (
        datetime.fromisoformat(data.get("timestamp"))
        if data.get("timestamp")
        else datetime.utcnow()
    )
    status_event = StatusHistory(
        shipment_id=shipment.id,
        status=data["status"],
        location=data.get("location"),
        timestamp=timestamp,
    )
    db.session.add(status_event)
    shipment.current_status = data["status"]
    db.session.commit()
    return jsonify({"message": "Status added"}), 201


@app.route("/shipments/<tracking_number>/sensor", methods=["GET"])
def get_sensor_data(tracking_number):
    shipment = Shipment.query.filter_by(tracking_number=tracking_number).first_or_404()
    sensors = (
        SensorData.query.filter_by(shipment_id=shipment.id)
        .order_by(SensorData.timestamp)
        .all()
    )
    return jsonify(
        [
            {
                "timestamp": s.timestamp.isoformat(),
                "temperature": s.temperature,
                "humidity": s.humidity,
                "location": s.location,
            }
            for s in sensors
        ]
    )


@app.route("/shipments/<tracking_number>/sensor", methods=["POST"])
def add_sensor_data(tracking_number):
    shipment = Shipment.query.filter_by(tracking_number=tracking_number).first_or_404()
    data = request.get_json()
    if not data or "temperature" not in data:
        return jsonify({"error": "Temperature required"}), 400

    timestamp = (
        datetime.fromisoformat(data.get("timestamp"))
        if data.get("timestamp")
        else datetime.utcnow()
    )
    sensor = SensorData(
        shipment_id=shipment.id,
        timestamp=timestamp,
        temperature=data["temperature"],
        humidity=data.get("humidity"),
        location=data.get("location"),
    )
    db.session.add(sensor)
    db.session.commit()

    create_temp_alert_if_needed(shipment.id, sensor.temperature)

    return jsonify({"message": "Sensor data added"}), 201


@app.route("/shipments/<tracking_number>/alerts", methods=["GET"])
def get_temperature_alerts(tracking_number):
    shipment = Shipment.query.filter_by(tracking_number=tracking_number).first_or_404()
    alerts = (
        TemperatureAlert.query.filter_by(shipment_id=shipment.id)
        .order_by(TemperatureAlert.timestamp)
        .all()
    )
    return jsonify(
        [
            {
                "temperature": a.temperature,
                "alert_type": a.alert_type,
                "timestamp": a.timestamp.isoformat(),
            }
            for a in alerts
        ]
    )


@app.route("/config/temperature-range", methods=["GET"])
def get_temperature_range():
    min_temp = Config.query.filter_by(key="min_temperature").first()
    max_temp = Config.query.filter_by(key="max_temperature").first()
    return jsonify(
        {
            "min_temperature": float(min_temp.value) if min_temp else None,
            "max_temperature": float(max_temp.value) if max_temp else None,
        }
    )


@app.route("/config/temperature-range", methods=["PUT"])
def set_temperature_range():
    data = request.get_json()
    if not data or "min_temperature" not in data or "max_temperature" not in data:
        return jsonify({"error": "min_temperature and max_temperature required"}), 400

    min_temp = Config.query.filter_by(key="min_temperature").first()
    if not min_temp:
        min_temp = Config(key="min_temperature", value=str(data["min_temperature"]))
        db.session.add(min_temp)
    else:
        min_temp.value = str(data["min_temperature"])

    max_temp = Config.query.filter_by(key="max_temperature").first()
    if not max_temp:
        max_temp = Config(key="max_temperature", value=str(data["max_temperature"]))
        db.session.add(max_temp)
    else:
        max_temp.value = str(data["max_temperature"])

    db.session.commit()
    return jsonify({"message": "Temperature range updated"})


# -------------------- Misc -------------------- #


def fetch_shipment_data_from_api(api_url):
    try:
        response = requests.get(api_url)
        response.raise_for_status()
        data = response.json()
        for shipment_data in data:
            if not Shipment.query.filter_by(
                tracking_number=shipment_data["tracking_number"]
            ).first():
                shipment = Shipment(
                    tracking_number=shipment_data["tracking_number"],
                    origin=shipment_data.get("origin"),
                    destination=shipment_data.get("destination"),
                    current_status=shipment_data.get("current_status"),
                )
                db.session.add(shipment)
        db.session.commit()
        print("Shipment data successfully fetched and added to the database.")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching shipment data: {e}")


# Create DB tables if they don't exist
with app.app_context():
    db.create_all()


if __name__ == "__main__":
    print(f"Running app with DB file at: {os.path.join(db_path, 'zoomlogi.db')}")
    app.run(debug=True)
