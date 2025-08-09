Approach
1. Overall Architecture
A Flask backend with REST APIs.

SQLite as a relational database for simplicity and quick prototyping.

Two external API data sources: FedEx tracking POST API and Sensor GET API.

Data ingestion endpoints or background jobs to pull/process external data.

Unified data model to normalize shipment and sensor info.

REST API to serve shipment data + history + alerts.

Temperature excursion monitoring logic integrated.


2. Data Modeling
Tables

shipments ->	Stores shipment metadata -> shipment_id (PK), tracking_number, origin, destination, current_status, created_at, updated_at	
status_history ->	Stores status/location events -> id (PK), shipment_id (FK), status, location, timestamp	
sensor_data	-> Stores sensor readings-> id (PK), shipment_id (FK), timestamp, temperature, humidity, location	
temperature_alerts	-> Stores alerts on temp excursions -> id (PK), shipment_id (FK), timestamp, temperature, alert_type	

3. Data Ingestion & Processing
FedEx API:

POST request with tracking number to get status updates.

Parse JSON: extract shipment status, location, timestamps.

Update shipment record & append to status_history.

Sensor API:

GET request with shipment sensor ID and time window.

Extract temperature, humidity, timestamp.

Insert into sensor_data table.

Check temperature ranges → if out-of-range → insert into temperature_alerts and append status_history.

Normalization:

Define a unified schema for status, location, and sensor data.

Use UTC timestamps for consistency.

Location can be lat/lon or city/state depending on source data.

4. Temperature Monitoring Logic
Configurable min and max temperature range (e.g., via config file or API).

On ingesting sensor data:

If temperature < min or > max → create temperature excursion event.

Append this event to status history with timestamp.


6. Flask Implementation Highlights
Use Flask blueprints to organize API routes.

Use SQLAlchemy ORM or plain sqlite3 for DB operations.

Background scheduler (like APScheduler) or cron to periodically fetch external APIs.

Simple config file/env vars for API keys and temperature thresholds.