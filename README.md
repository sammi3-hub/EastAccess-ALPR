# EastAccess-ALPR

## Overview
EastAccess-ALPR is a Flask-based smart vehicle access control system that uses Automatic License Plate Recognition (ALPR) and ESP32-powered boom barriers to manage vehicle entry and exit.

This repository contains the guard dashboard part of the system, including gate logs, dashboard interface, ALPR processing code, and ESP32 boom barrier control code.

## Features
- Automatic License Plate Recognition (ALPR)
- Vehicle entry and exit monitoring
- Guard dashboard for gate activity logs
- ESP32-based boom barrier control
- SQLite database for gate records
- Web-based interface using Flask

## Technologies Used
- Python
- Flask
- SQLite
- OpenCV
- EasyOCR
- HTML
- CSS
- JavaScript
- ESP32 / Arduino

## Project Structure

```text
EastAccess-ALPR/
├── app.py
├── requirements.txt
├── Procfile
├── render.yaml
├── eastaccess_gate.db
├── templates/
│   └── dashboard.html
├── static/
│   ├── style.css
│   ├── eastaccess_logo.png
│   └── eastaccess_icon_only.png
└── arduino/
    └── eastaccess_two_servo_esp32/
        └── eastaccess_two_servo_esp32.ino
```

## How to Run Locally

1. Install the required packages:

```bash
pip install -r requirements.txt
```

2. Run the Flask application:

```bash
python app.py
```

3. Open your browser and go to:

```text
http://127.0.0.1:5000
```

## Local Hardware Mode

By default, local mode will try to use the camera and ESP32 connection.

If your ESP32 uses a different port, update this line in `app.py`:

```python
SERIAL_PORT = "COM5"
```

## Deployment Note

This project includes files for deployment on Render:

- `Procfile`
- `render.yaml`
- `gunicorn` in `requirements.txt`

On Render, the project runs in dashboard/demo mode by default because cloud hosting cannot access your local camera, COM port, or ESP32 hardware.

## Purpose

This project was developed as an academic project to demonstrate the integration of web development, computer vision, database management, and IoT-based gate automation.

## Author

**Samantha**  
BS Information Technology  
National University Fairview
