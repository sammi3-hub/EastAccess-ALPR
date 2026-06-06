import os
import cv2
import easyocr
import serial
import time
import re
import sqlite3
import threading
from datetime import datetime, timedelta

from flask import Flask, render_template, Response, request, redirect, url_for, jsonify

# =========================================================
# EASTACCESS ALPR WEB DASHBOARD
# 2 cameras + 1 ESP32 + 2 servo boom barriers
# No sample registered vehicles
# Faster but stable OCR confirmation
# =========================================================

# Change this if your ESP32 is on a different COM port
SERIAL_PORT = "COM5"
BAUD_RATE = 115200

# Your tested camera indexes
ENTRY_CAMERA_INDEX = 0
EXIT_CAMERA_INDEX = 1

FRAME_WIDTH = 640
FRAME_HEIGHT = 480

# Recommended faster verification settings:
# 3 stable reads, but faster OCR interval.
OCR_INTERVAL_SECONDS = 0.35
# Faster OCR response:
# - idle interval: normal scanning when no plate candidate yet
# - active interval: faster re-scan once a possible plate is seen
IDLE_OCR_INTERVAL_SECONDS = 0.35
ACTIVE_OCR_INTERVAL_SECONDS = 0.12
ACTION_COOLDOWN_SECONDS = 12

MIN_OCR_CONFIDENCE = 0.45
REQUIRED_STABLE_READS = 1
SAME_PLATE_REPEAT_BLOCK_SECONDS = 12
DETECTION_DETAILS_HOLD_SECONDS = 10

DATABASE_FILE = "eastaccess_gate.db"

app = Flask(__name__)

# =========================================================
# GLOBALS
# =========================================================

reader = None
esp32 = None

latest_frames = {
    "ENTRY": None,
    "EXIT": None
}

latest_raw_frames = {
    "ENTRY": None,
    "EXIT": None
}

latest_jpegs = {
    "ENTRY": None,
    "EXIT": None
}

latest_state = {
    "ENTRY": {
        "plate": "None",
        "status": "Waiting for plate...",
        "owner": "-",
        "vehicle_type": "-",
        "color": "-",
        "access_type": "-",
        "time": "-"
    },
    "EXIT": {
        "plate": "None",
        "status": "Waiting for plate...",
        "owner": "-",
        "vehicle_type": "-",
        "color": "-",
        "access_type": "-",
        "time": "-"
    }
}

last_action_time = {
    "ENTRY": 0,
    "EXIT": 0
}

plate_tracker = {
    "ENTRY": {
        "candidate": "",
        "count": 0,
        "last_processed": "",
        "last_processed_time": 0
    },
    "EXIT": {
        "candidate": "",
        "count": 0,
        "last_processed": "",
        "last_processed_time": 0
    }
}

# Keeps the granted vehicle details visible while the boom barrier is still open.
state_hold_until = {
    "ENTRY": 0,
    "EXIT": 0
}

camera_lock = threading.Lock()
db_lock = threading.Lock()
serial_lock = threading.Lock()
ocr_lock = threading.Lock()

# =========================================================
# DATABASE SETUP
# =========================================================

def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS registered_vehicles (
                plate_number TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                vehicle_type TEXT NOT NULL,
                color TEXT NOT NULL,
                address TEXT,
                status TEXT DEFAULT 'ACTIVE'
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS temporary_visitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plate_number TEXT NOT NULL,
                driver_name TEXT NOT NULL,
                vehicle_type TEXT NOT NULL,
                color TEXT,
                purpose TEXT,
                destination TEXT,
                valid_until TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'ACTIVE'
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS gate_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                plate_number TEXT NOT NULL,
                owner_or_driver TEXT,
                vehicle_type TEXT,
                color TEXT,
                gate TEXT NOT NULL,
                status TEXT NOT NULL,
                access_type TEXT,
                remarks TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS currently_inside (
                plate_number TEXT PRIMARY KEY,
                owner_or_driver TEXT,
                vehicle_type TEXT,
                color TEXT,
                access_type TEXT,
                entry_time TEXT
            )
        """)

        # No sample vehicles are inserted here.
        # Add resident vehicles using the website form only.

        conn.commit()
        conn.close()

# =========================================================
# HELPERS
# =========================================================

def now_string():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def wants_json_response():
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )


def normalize_plate_ocr_errors(text):
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]", "", text)

    # Common OCR mistakes in numeric parts of plates.
    # Example: QFH4O9 becomes QFH409.
    digit_corrections = {
        "O": "0",
        "Q": "0",
        "D": "0",
        "I": "1",
        "L": "1",
        "S": "5",
        "B": "8",
        "Z": "2",
        "G": "6"
    }

    def fix_digit_part(part):
        return "".join(digit_corrections.get(char, char) for char in part)

    # Format: letters first then numbers, example QFH4O9 -> QFH409
    match_letters_first = re.match(r"^([A-Z]+)([A-Z0-9]*[0-9][A-Z0-9]*)$", text)
    if match_letters_first:
        letters = match_letters_first.group(1)
        possible_numbers = match_letters_first.group(2)
        return letters + fix_digit_part(possible_numbers)

    # Format: numbers first then letters, example 4O9QFH -> 409QFH
    match_numbers_first = re.match(r"^([A-Z0-9]*[0-9][A-Z0-9]*)([A-Z]+)$", text)
    if match_numbers_first:
        possible_numbers = match_numbers_first.group(1)
        letters = match_numbers_first.group(2)
        return fix_digit_part(possible_numbers) + letters

    return text


def clean_plate_text(text):
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]", "", text)
    text = normalize_plate_ocr_errors(text)
    return text


def get_plate_variants(plate_number):
    """
    Returns possible plate formats for matching.
    Example:
    409QFH -> ["409QFH", "QFH409"]
    QFH409 -> ["QFH409", "409QFH"]
    """
    plate_number = clean_plate_text(plate_number)
    variants = [plate_number]

    match_letters_first = re.match(r"^([A-Z]+)([0-9]+)$", plate_number)
    match_numbers_first = re.match(r"^([0-9]+)([A-Z]+)$", plate_number)

    if match_letters_first:
        letters = match_letters_first.group(1)
        numbers = match_letters_first.group(2)
        variants.append(numbers + letters)

    if match_numbers_first:
        numbers = match_numbers_first.group(1)
        letters = match_numbers_first.group(2)
        variants.append(letters + numbers)

    unique_variants = []
    for variant in variants:
        if variant not in unique_variants:
            unique_variants.append(variant)

    return unique_variants


def looks_like_plate(text):
    # Philippine-style demo plates should be at least 6 alphanumeric characters.
    # This prevents random short OCR text from being processed/logged.
    if len(text) < 6 or len(text) > 8:
        return False

    has_letter = any(char.isalpha() for char in text)
    has_number = any(char.isdigit() for char in text)

    return has_letter and has_number

# =========================================================
# DATABASE HELPERS
# =========================================================

def get_registered_vehicle(plate_number):
    variants = get_plate_variants(plate_number)
    placeholders = ",".join(["?"] * len(variants))

    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT * FROM registered_vehicles
            WHERE plate_number IN ({placeholders})
            AND status = 'ACTIVE'
            LIMIT 1
        """, variants)
        row = cur.fetchone()
        conn.close()

    return row


def get_active_temporary_visitor(plate_number):
    variants = get_plate_variants(plate_number)
    placeholders = ",".join(["?"] * len(variants))
    params = variants + [now_string()]

    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT * FROM temporary_visitors
            WHERE plate_number IN ({placeholders})
            AND status = 'ACTIVE'
            AND valid_until >= ?
            ORDER BY valid_until DESC
            LIMIT 1
        """, params)
        row = cur.fetchone()
        conn.close()

    return row


def get_plate_access_details(plate_number):
    plate_number = clean_plate_text(plate_number)

    registered = get_registered_vehicle(plate_number)

    if registered:
        return {
            "authorized": True,
            "access_type": "Resident",
            "owner_or_driver": registered["owner"],
            "vehicle_type": registered["vehicle_type"],
            "color": registered["color"],
            "remarks": "Registered resident vehicle"
        }

    temporary = get_active_temporary_visitor(plate_number)

    if temporary:
        return {
            "authorized": True,
            "access_type": "Temporary Visitor",
            "owner_or_driver": temporary["driver_name"],
            "vehicle_type": temporary["vehicle_type"],
            "color": temporary["color"] or "-",
            "remarks": f"Temporary access valid until {temporary['valid_until']}"
        }

    return {
        "authorized": False,
        "access_type": "Unknown",
        "owner_or_driver": "Unknown",
        "vehicle_type": "Unknown",
        "color": "Unknown",
        "remarks": "Plate is not registered and has no active temporary access"
    }


def get_inside_vehicle(plate_number):
    variants = get_plate_variants(plate_number)
    placeholders = ",".join(["?"] * len(variants))

    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM currently_inside WHERE plate_number IN ({placeholders})", variants)
        row = cur.fetchone()
        conn.close()

    return row


def is_vehicle_inside(plate_number):
    return get_inside_vehicle(plate_number) is not None


def add_vehicle_inside(plate_number, owner_or_driver, vehicle_type, color, access_type):
    plate_number = clean_plate_text(plate_number)

    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO currently_inside
            (plate_number, owner_or_driver, vehicle_type, color, access_type, entry_time)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (plate_number, owner_or_driver, vehicle_type, color, access_type, now_string()))
        conn.commit()
        conn.close()


def remove_vehicle_inside(plate_number):
    variants = get_plate_variants(plate_number)
    placeholders = ",".join(["?"] * len(variants))

    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM currently_inside WHERE plate_number IN ({placeholders})", variants)
        conn.commit()
        conn.close()


def log_gate_event(plate_number, owner_or_driver, vehicle_type, color, gate, status, access_type, remarks):
    plate_number = clean_plate_text(plate_number)

    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO gate_logs
            (timestamp, plate_number, owner_or_driver, vehicle_type, color, gate, status, access_type, remarks)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now_string(),
            plate_number,
            owner_or_driver,
            vehicle_type,
            color,
            gate,
            status,
            access_type,
            remarks
        ))
        conn.commit()
        conn.close()

    print(f"LOGGED: {plate_number} | {gate} | {status} | {remarks}")

# =========================================================
# ESP32 SERIAL
# =========================================================

def connect_esp32():
    global esp32

    try:
        esp32 = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        time.sleep(2)
        print(f"ESP32 connected on {SERIAL_PORT}")
    except Exception as e:
        esp32 = None
        print("WARNING: Cannot connect to ESP32.")
        print("Reason:", e)
        print("Close Arduino Serial Monitor and check COM port.")


def send_to_esp32(command):
    global esp32

    if esp32 is None:
        print(f"ESP32 not connected. Command skipped: {command}")
        return False

    try:
        with serial_lock:
            esp32.write((command + "\n").encode("utf-8"))
            esp32.flush()
        print(f"Sent to ESP32: {command}")
        return True
    except Exception as e:
        print("ERROR sending command to ESP32:", e)
        return False

# =========================================================
# OCR AND CAMERA
# =========================================================

def preprocess_roi(roi):
    """Fast OCR preprocessing for printed/demo plates."""
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

    # Smaller upscale is faster while still readable for printed plates.
    gray = cv2.resize(gray, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_LINEAR)

    # CLAHE improves contrast without the heavy bilateral filter delay.
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    _, thresh = cv2.threshold(
        gray,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )

    return thresh


def read_plate_from_roi(roi):
    global reader

    processed = preprocess_roi(roi)

    with ocr_lock:
        results = reader.readtext(
            processed,
            allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            detail=1,
            paragraph=False,
            decoder="greedy",
            beamWidth=1,
            batch_size=1,
            workers=0
        )

    best_text = ""
    best_confidence = 0

    for bbox, text, confidence in results:
        cleaned = clean_plate_text(text)

        if looks_like_plate(cleaned) and confidence > best_confidence:
            best_text = cleaned
            best_confidence = confidence

    return best_text, best_confidence


def is_detection_state_holding(gate_name):
    return time.time() < state_hold_until.get(gate_name, 0)


def get_confirmed_plate(gate_name, plate_text, confidence):
    plate_text = clean_plate_text(plate_text)
    tracker = plate_tracker[gate_name]

    # If a vehicle was just granted access, keep its details visible until
    # the barrier's auto-close period is done.
    if is_detection_state_holding(gate_name):
        return None

    if not plate_text or confidence < MIN_OCR_CONFIDENCE:
        tracker["candidate"] = ""
        tracker["count"] = 0
        return None

    if not looks_like_plate(plate_text):
        tracker["candidate"] = ""
        tracker["count"] = 0
        return None

    if tracker["candidate"] == plate_text:
        tracker["count"] += 1
    else:
        tracker["candidate"] = plate_text
        tracker["count"] = 1

    latest_state[gate_name]["plate"] = plate_text
    latest_state[gate_name]["status"] = f"Verifying {plate_text} ({tracker['count']}/{REQUIRED_STABLE_READS})"
    latest_state[gate_name]["owner"] = "-"
    latest_state[gate_name]["vehicle_type"] = "-"
    latest_state[gate_name]["color"] = "-"
    latest_state[gate_name]["access_type"] = "-"
    latest_state[gate_name]["time"] = now_string()

    print(
        f"{gate_name} verifying: {plate_text} "
        f"({tracker['count']}/{REQUIRED_STABLE_READS}) "
        f"confidence: {confidence:.2f}"
    )

    if tracker["count"] < REQUIRED_STABLE_READS:
        return None

    current_time = time.time()

    # Repeat block prevents repeated logging while the same plate remains in view.
    # Exception for EXIT: if the car is still inside, allow it to exit.
    if (
        tracker["last_processed"] == plate_text
        and current_time - tracker["last_processed_time"] < SAME_PLATE_REPEAT_BLOCK_SECONDS
    ):
        if gate_name == "EXIT" and is_vehicle_inside(plate_text):
            print(f"{gate_name} override repeat block because {plate_text} is still inside.")
        else:
            latest_state[gate_name]["status"] = f"Already processed recently: {plate_text}"
            return None

    tracker["last_processed"] = plate_text
    tracker["last_processed_time"] = current_time
    tracker["candidate"] = ""
    tracker["count"] = 0

    return plate_text


def draw_overlay(frame, gate_name):
    clear_expired_detection_holds()
    height, width, _ = frame.shape

    x1 = int(width * 0.18)
    y1 = int(height * 0.38)
    x2 = int(width * 0.82)
    y2 = int(height * 0.63)

    state = latest_state[gate_name]

    cv2.rectangle(frame, (x1, y1), (x2, y2), (50, 255, 50), 2)

    cv2.putText(
        frame,
        f"{gate_name} CAMERA",
        (25, 35),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (255, 255, 255),
        2
    )

    cv2.putText(
        frame,
        f"Plate: {state['plate']}",
        (25, 75),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (50, 255, 50),
        2
    )

    status = state["status"]
    if "DENIED" in status:
        color = (0, 0, 255)
    elif "GRANTED" in status or "Verifying" in status:
        color = (0, 255, 0)
    else:
        color = (255, 255, 255)

    cv2.putText(
        frame,
        status[:45],
        (25, 115),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        color,
        2
    )

    return frame, (x1, y1, x2, y2)


def update_latest_state(gate, plate, status, details):
    latest_state[gate] = {
        "plate": plate,
        "status": status,
        "owner": details.get("owner_or_driver", "-"),
        "vehicle_type": details.get("vehicle_type", "-"),
        "color": details.get("color", "-"),
        "access_type": details.get("access_type", "-"),
        "time": now_string()
    }

    # Keep details visible while the boom barrier is open after successful access.
    if "GRANTED" in status:
        state_hold_until[gate] = time.time() + DETECTION_DETAILS_HOLD_SECONDS


def reset_latest_state(gate):
    latest_state[gate] = {
        "plate": "None",
        "status": "Waiting for plate...",
        "owner": "-",
        "vehicle_type": "-",
        "color": "-",
        "access_type": "-",
        "time": "-"
    }
    state_hold_until[gate] = 0


def clear_expired_detection_holds():
    current_time = time.time()

    for gate in ["ENTRY", "EXIT"]:
        status = latest_state[gate].get("status", "")

        if (
            state_hold_until.get(gate, 0) > 0
            and current_time >= state_hold_until[gate]
            and "GRANTED" in status
        ):
            reset_latest_state(gate)



def process_entry(plate_number):
    current_time = time.time()

    if current_time - last_action_time["ENTRY"] < ACTION_COOLDOWN_SECONDS:
        return

    details = get_plate_access_details(plate_number)

    if not details["authorized"]:
        status = f"ENTRY DENIED: {plate_number}"
        update_latest_state("ENTRY", plate_number, status, details)
        log_gate_event(
            plate_number,
            details["owner_or_driver"],
            details["vehicle_type"],
            details["color"],
            "ENTRY",
            "ACCESS DENIED",
            details["access_type"],
            details["remarks"]
        )
        send_to_esp32("DENIED_ENTRY")
        last_action_time["ENTRY"] = current_time
        return

    if is_vehicle_inside(plate_number):
        status = f"ENTRY DENIED: {plate_number} ALREADY INSIDE"
        update_latest_state("ENTRY", plate_number, status, details)
        log_gate_event(
            plate_number,
            details["owner_or_driver"],
            details["vehicle_type"],
            details["color"],
            "ENTRY",
            "ACCESS DENIED",
            details["access_type"],
            "Vehicle is already recorded inside"
        )
        send_to_esp32("DENIED_ENTRY")
        last_action_time["ENTRY"] = current_time
        return

    add_vehicle_inside(
        plate_number,
        details["owner_or_driver"],
        details["vehicle_type"],
        details["color"],
        details["access_type"]
    )

    status = f"ENTRY GRANTED: {plate_number}"
    update_latest_state("ENTRY", plate_number, status, details)

    log_gate_event(
        plate_number,
        details["owner_or_driver"],
        details["vehicle_type"],
        details["color"],
        "ENTRY",
        "ACCESS GRANTED",
        details["access_type"],
        "Vehicle entered subdivision"
    )

    send_to_esp32("AUTO_OPEN_ENTRY")
    last_action_time["ENTRY"] = current_time


def process_exit(plate_number):
    current_time = time.time()
    plate_number = clean_plate_text(plate_number)

    # For EXIT, Currently Inside is priority.
    # Even if a temporary visitor pass expired, the vehicle should still be allowed to exit.
    inside_record = get_inside_vehicle(plate_number)

    if inside_record:
        exit_plate = inside_record["plate_number"]

        remove_vehicle_inside(exit_plate)

        details = {
            "owner_or_driver": inside_record["owner_or_driver"],
            "vehicle_type": inside_record["vehicle_type"],
            "color": inside_record["color"],
            "access_type": inside_record["access_type"]
        }

        status = f"EXIT GRANTED: {exit_plate}"
        update_latest_state("EXIT", exit_plate, status, details)

        log_gate_event(
            exit_plate,
            inside_record["owner_or_driver"],
            inside_record["vehicle_type"],
            inside_record["color"],
            "EXIT",
            "ACCESS GRANTED",
            inside_record["access_type"],
            "Vehicle exited subdivision"
        )

        send_to_esp32("AUTO_OPEN_EXIT")
        last_action_time["EXIT"] = current_time

        plate_tracker["EXIT"]["candidate"] = ""
        plate_tracker["EXIT"]["count"] = 0
        plate_tracker["EXIT"]["last_processed"] = ""
        plate_tracker["EXIT"]["last_processed_time"] = 0

        return

    if current_time - last_action_time["EXIT"] < ACTION_COOLDOWN_SECONDS:
        return

    details = get_plate_access_details(plate_number)

    if details["authorized"]:
        status = f"EXIT DENIED: {plate_number} NO ENTRY RECORD"
        remarks = "Vehicle has no entry record"
    else:
        status = f"EXIT DENIED: {plate_number}"
        remarks = details["remarks"]

    update_latest_state("EXIT", plate_number, status, details)

    log_gate_event(
        plate_number,
        details["owner_or_driver"],
        details["vehicle_type"],
        details["color"],
        "EXIT",
        "ACCESS DENIED",
        details["access_type"],
        remarks
    )

    send_to_esp32("DENIED_EXIT")
    last_action_time["EXIT"] = current_time


def camera_worker(gate_name, camera_index):
    """Fast camera thread: capture frames only, so live feed does not freeze during OCR."""
    global latest_frames, latest_raw_frames, latest_jpegs

    print(f"Opening {gate_name} camera index {camera_index}...")

    # DirectShow is more stable for multiple webcams on Windows.
    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)

    if not cap.isOpened():
        print(f"ERROR: {gate_name} camera not found.")
        return

    # MJPG + 30 FPS usually makes USB webcams smoother on Windows.
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    # Try to drop stale frames when supported.
    try:
        cap.grab()
    except Exception:
        pass

    while True:
        ret, frame = cap.read()

        if not ret:
            print(f"ERROR: Cannot read {gate_name} camera.")
            time.sleep(0.25)
            continue

        frame = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT))

        # Draw UI overlay for streaming only.
        overlay_frame, _ = draw_overlay(frame.copy(), gate_name)

        # Pre-encode once here so every browser client does not re-encode the same frame.
        ret_jpg, buffer = cv2.imencode(
            ".jpg",
            overlay_frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 72]
        )

        with camera_lock:
            latest_raw_frames[gate_name] = frame.copy()
            latest_frames[gate_name] = overlay_frame
            if ret_jpg:
                latest_jpegs[gate_name] = buffer.tobytes()

        # Lower sleep = smoother live view. OCR now runs in separate thread.
        time.sleep(0.015)


def ocr_worker(gate_name):
    """OCR thread: responsive OCR without blocking live video."""
    last_ocr_time = 0

    while True:
        current_time = time.time()

        tracker = plate_tracker[gate_name]
        target_interval = (
            ACTIVE_OCR_INTERVAL_SECONDS
            if tracker.get("candidate")
            else IDLE_OCR_INTERVAL_SECONDS
        )

        if current_time - last_ocr_time < target_interval:
            time.sleep(0.03)
            continue

        with camera_lock:
            frame = latest_raw_frames.get(gate_name)

        if frame is None:
            time.sleep(0.08)
            continue

        height, width, _ = frame.shape

        # Slightly tighter ROI = less pixels for OCR = faster processing.
        x1 = int(width * 0.22)
        y1 = int(height * 0.41)
        x2 = int(width * 0.78)
        y2 = int(height * 0.61)
        roi = frame[y1:y2, x1:x2]

        plate_text, confidence = read_plate_from_roi(roi)

        if plate_text:
            confirmed_plate = get_confirmed_plate(gate_name, plate_text, confidence)

            if confirmed_plate:
                print(f"{gate_name} CONFIRMED PLATE: {confirmed_plate}")

                if gate_name == "ENTRY":
                    process_entry(confirmed_plate)
                else:
                    process_exit(confirmed_plate)

        last_ocr_time = time.time()


def create_placeholder_frame(gate_name):
    frame = 255 * cv2.UMat(FRAME_HEIGHT, FRAME_WIDTH, cv2.CV_8UC3).get()
    cv2.putText(
        frame,
        f"{gate_name} CAMERA LOADING...",
        (70, 240),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (0, 0, 0),
        2
    )
    return frame


def generate_video_feed(gate_name):
    """Stream latest pre-encoded JPEG. Much lighter than encoding per request."""
    placeholder_jpeg = None

    while True:
        with camera_lock:
            jpeg_bytes = latest_jpegs.get(gate_name)

        if jpeg_bytes is None:
            if placeholder_jpeg is None:
                frame = create_placeholder_frame(gate_name)
                ret, buffer = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 72]
                )
                if ret:
                    placeholder_jpeg = buffer.tobytes()

            jpeg_bytes = placeholder_jpeg

        if jpeg_bytes:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-store\r\n\r\n" +
                jpeg_bytes +
                b"\r\n"
            )

        time.sleep(0.025)

# =========================================================
# DASHBOARD DATA
# =========================================================

def get_dashboard_stats():
    today = datetime.now().strftime("%Y-%m-%d")

    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT COUNT(*) AS count FROM gate_logs
            WHERE DATE(timestamp) = ? AND gate = 'ENTRY' AND status = 'ACCESS GRANTED'
        """, (today,))
        entries_today = cur.fetchone()["count"]

        cur.execute("""
            SELECT COUNT(*) AS count FROM gate_logs
            WHERE DATE(timestamp) = ? AND gate = 'EXIT' AND status = 'ACCESS GRANTED'
        """, (today,))
        exits_today = cur.fetchone()["count"]

        cur.execute("SELECT COUNT(*) AS count FROM currently_inside")
        currently_inside = cur.fetchone()["count"]

        cur.execute("""
            SELECT COUNT(*) AS count FROM gate_logs
            WHERE DATE(timestamp) = ? AND status = 'ACCESS DENIED'
        """, (today,))
        denied_today = cur.fetchone()["count"]

        cur.execute("""
            SELECT COUNT(DISTINCT tv.id) AS count
            FROM temporary_visitors tv
            LEFT JOIN currently_inside ci
                ON ci.plate_number = tv.plate_number
                AND ci.access_type = 'Temporary Visitor'
            WHERE tv.status = 'ACTIVE'
              AND (
                    tv.valid_until >= ?
                    OR ci.plate_number IS NOT NULL
                  )
        """, (now_string(),))
        active_temp = cur.fetchone()["count"]

        conn.close()

    return {
        "entries_today": entries_today,
        "exits_today": exits_today,
        "currently_inside": currently_inside,
        "denied_today": denied_today,
        "active_temp": active_temp
    }


def get_recent_logs(limit=100):
    """Recent Gate Activity excludes dashboard-only registration/access creation logs."""
    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM gate_logs
            WHERE status NOT IN ('TEMP ACCESS CREATED', 'REGISTERED VEHICLE SAVED')
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
    return rows


def get_dashboard_activity_logs(limit=100):
    """Dashboard Activity Logs shows temp access creation and registered vehicle saves."""
    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM gate_logs
            WHERE status IN ('TEMP ACCESS CREATED', 'REGISTERED VEHICLE SAVED')
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
    return rows


def get_currently_inside_list():
    """Return current inside list with overstay status for temporary visitors."""
    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                ci.*,
                (
                    SELECT MAX(tv.valid_until)
                    FROM temporary_visitors tv
                    WHERE tv.plate_number = ci.plate_number
                ) AS temp_valid_until
            FROM currently_inside ci
            ORDER BY ci.entry_time DESC
        """)
        rows = cur.fetchall()
        conn.close()

    current_time = now_string()
    result = []

    for row in rows:
        item = dict(row)
        item["inside_status"] = "Inside"
        item["status_class"] = "normal"
        item["temp_valid_until"] = item.get("temp_valid_until") or ""

        if item.get("access_type") == "Temporary Visitor":
            valid_until = item.get("temp_valid_until") or ""

            if valid_until and valid_until < current_time:
                item["inside_status"] = "OVERSTAYED"
                item["status_class"] = "overstayed"
            else:
                item["inside_status"] = "Temporary"
                item["status_class"] = "temporary"

        result.append(item)

    return result


def get_active_visitors_list():
    """Show active temp passes and temp visitors currently inside, including OVERSTAYED."""
    current_time = now_string()

    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT
                tv.*,
                CASE
                    WHEN ci.plate_number IS NOT NULL AND tv.valid_until < ? THEN 'OVERSTAYED'
                    WHEN ci.plate_number IS NOT NULL THEN 'INSIDE'
                    ELSE 'ACTIVE'
                END AS visitor_display_status
            FROM temporary_visitors tv
            LEFT JOIN currently_inside ci
                ON ci.plate_number = tv.plate_number
                AND ci.access_type = 'Temporary Visitor'
            WHERE tv.status = 'ACTIVE'
              AND (
                    tv.valid_until >= ?
                    OR ci.plate_number IS NOT NULL
                  )
            ORDER BY tv.valid_until ASC
        """, (current_time, current_time))
        rows = cur.fetchall()
        conn.close()
    return rows


def get_registered_vehicle_list():
    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM registered_vehicles ORDER BY plate_number ASC")
        rows = cur.fetchall()
        conn.close()
    return rows

# =========================================================
# WEB ROUTES
# =========================================================

@app.route("/")
def dashboard():
    return render_template(
        "dashboard.html",
        stats=get_dashboard_stats(),
        logs=get_recent_logs(),
        dashboard_logs=get_dashboard_activity_logs(),
        inside=get_currently_inside_list(),
        visitors=get_active_visitors_list(),
        vehicles=get_registered_vehicle_list(),
        state=latest_state
    )


@app.route("/video/<gate_name>")
def video(gate_name):
    gate_name = gate_name.upper()

    if gate_name not in ["ENTRY", "EXIT"]:
        return "Invalid camera", 404

    return Response(
        generate_video_feed(gate_name),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@app.route("/api/state")
def api_state():
    clear_expired_detection_holds()

    response = jsonify({
        "entry": latest_state["ENTRY"],
        "exit": latest_state["EXIT"],
        "stats": get_dashboard_stats(),
        "logs": [dict(row) for row in get_recent_logs(limit=100)],
        "dashboard_logs": [dict(row) for row in get_dashboard_activity_logs(limit=100)],
        "inside": [dict(row) for row in get_currently_inside_list()],
        "visitors": [dict(row) for row in get_active_visitors_list()]
    })

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    return response


@app.route("/manual/<action>", methods=["POST"])
def manual_action(action):
    action = action.upper()

    if action == "OPEN_ENTRY":
        send_to_esp32("OPEN_ENTRY")
        log_gate_event(
            "MANUAL",
            "Guard",
            "-",
            "-",
            "ENTRY",
            "MANUAL OPEN",
            "Manual",
            "Opened entry barrier from dashboard"
        )

    elif action == "CLOSE_ENTRY":
        send_to_esp32("CLOSE_ENTRY")
        reset_latest_state("ENTRY")
        log_gate_event(
            "MANUAL",
            "Guard",
            "-",
            "-",
            "ENTRY",
            "MANUAL CLOSE",
            "Manual",
            "Closed entry barrier from dashboard"
        )

    elif action == "OPEN_EXIT":
        send_to_esp32("OPEN_EXIT")
        log_gate_event(
            "MANUAL",
            "Guard",
            "-",
            "-",
            "EXIT",
            "MANUAL OPEN",
            "Manual",
            "Opened exit barrier from dashboard"
        )

    elif action == "CLOSE_EXIT":
        send_to_esp32("CLOSE_EXIT")
        reset_latest_state("EXIT")
        log_gate_event(
            "MANUAL",
            "Guard",
            "-",
            "-",
            "EXIT",
            "MANUAL CLOSE",
            "Manual",
            "Closed exit barrier from dashboard"
        )

    elif action == "CLOSE":
        # Legacy fallback. Closes both barriers if an old button still calls CLOSE.
        send_to_esp32("CLOSE_ENTRY")
        send_to_esp32("CLOSE_EXIT")
        reset_latest_state("ENTRY")
        reset_latest_state("EXIT")
        log_gate_event(
            "MANUAL",
            "Guard",
            "-",
            "-",
            "BARRIER",
            "MANUAL CLOSE",
            "Manual",
            "Closed both barriers from dashboard"
        )

    else:
        return jsonify({"success": False, "message": "Invalid action"}), 400

    return jsonify({"success": True, "message": f"{action} sent"})


@app.route("/add_temp_visitor", methods=["POST"])
def add_temp_visitor():
    plate_number = clean_plate_text(request.form.get("plate_number", ""))
    driver_name = request.form.get("driver_name", "").strip()
    vehicle_type = request.form.get("vehicle_type", "").strip()
    vehicle_type_other = request.form.get("vehicle_type_other", "").strip()
    color = request.form.get("color", "").strip()
    purpose = request.form.get("purpose", "").strip()
    purpose_other = request.form.get("purpose_other", "").strip()
    destination = request.form.get("destination", "").strip()

    if vehicle_type == "Others" and vehicle_type_other:
        vehicle_type = vehicle_type_other

    if purpose == "Others" and purpose_other:
        purpose = purpose_other

    try:
        valid_minutes = int(request.form.get("valid_minutes", "15"))
    except ValueError:
        valid_minutes = 15

    if not plate_number or not driver_name or not vehicle_type:
        if wants_json_response():
            return jsonify({"ok": False, "message": "Missing required temporary visitor fields"}), 400
        return redirect(url_for("dashboard"))

    valid_until = datetime.now() + timedelta(minutes=valid_minutes)

    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO temporary_visitors
            (plate_number, driver_name, vehicle_type, color, purpose, destination, valid_until, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
        """, (
            plate_number,
            driver_name,
            vehicle_type,
            color,
            purpose,
            destination,
            valid_until.strftime("%Y-%m-%d %H:%M:%S"),
            now_string()
        ))
        conn.commit()
        conn.close()

    log_gate_event(
        plate_number,
        driver_name,
        vehicle_type,
        color,
        "DASHBOARD",
        "TEMP ACCESS CREATED",
        "Temporary Visitor",
        f"{purpose} | Destination: {destination} | Valid for {valid_minutes} minutes"
    )

    if wants_json_response():
        return jsonify({"ok": True, "message": "Temporary visitor added"})

    return redirect(url_for("dashboard"))


@app.route("/add_registered_vehicle", methods=["POST"])
def add_registered_vehicle():
    plate_number = clean_plate_text(request.form.get("plate_number", ""))
    owner = request.form.get("owner", "").strip()
    vehicle_type = request.form.get("vehicle_type", "").strip()
    vehicle_type_other = request.form.get("vehicle_type_other", "").strip()
    color = request.form.get("color", "").strip()
    address = request.form.get("address", "").strip()

    if vehicle_type == "Others" and vehicle_type_other:
        vehicle_type = vehicle_type_other

    if not plate_number or not owner or not vehicle_type or not color:
        if wants_json_response():
            return jsonify({"ok": False, "message": "Missing required registered vehicle fields"}), 400
        return redirect(url_for("dashboard"))

    with db_lock:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO registered_vehicles
            (plate_number, owner, vehicle_type, color, address, status)
            VALUES (?, ?, ?, ?, ?, 'ACTIVE')
        """, (plate_number, owner, vehicle_type, color, address))
        conn.commit()
        conn.close()

    log_gate_event(
        plate_number,
        owner,
        vehicle_type,
        color,
        "DASHBOARD",
        "REGISTERED VEHICLE SAVED",
        "Resident",
        f"Address: {address or '-'}"
    )

    if wants_json_response():
        return jsonify({"ok": True, "message": "Registered vehicle saved"})

    return redirect(url_for("dashboard"))

# =========================================================
# STARTUP
# =========================================================

def start_background_threads():
    entry_thread = threading.Thread(
        target=camera_worker,
        args=("ENTRY", ENTRY_CAMERA_INDEX),
        daemon=True
    )

    exit_thread = threading.Thread(
        target=camera_worker,
        args=("EXIT", EXIT_CAMERA_INDEX),
        daemon=True
    )

    entry_ocr_thread = threading.Thread(
        target=ocr_worker,
        args=("ENTRY",),
        daemon=True
    )

    exit_ocr_thread = threading.Thread(
        target=ocr_worker,
        args=("EXIT",),
        daemon=True
    )

    entry_thread.start()
    exit_thread.start()
    entry_ocr_thread.start()
    exit_ocr_thread.start()


# =========================================================
# APP INITIALIZATION
# =========================================================

_initialized = False


def hardware_enabled():
    """Enable cameras/ESP32 locally, but keep them off on Render by default."""
    value = os.environ.get("RUN_HARDWARE")

    if value is not None:
        return value == "1"

    # Render sets the RENDER environment variable.
    # On Render, we only show the dashboard/demo pages by default.
    return not bool(os.environ.get("RENDER"))


def initialize_app():
    global _initialized, reader

    if _initialized:
        return

    init_database()

    if hardware_enabled():
        print("Loading EasyOCR...")
        # Keep CPU mode because Intel Arc is not CUDA. This is stable for prototype.
        reader = easyocr.Reader(["en"], gpu=False)
        print("EasyOCR loaded.")

        connect_esp32()
        start_background_threads()
    else:
        print("Hardware mode is disabled. Running dashboard/demo mode only.")

    _initialized = True


initialize_app()


if __name__ == "__main__":
    print("Starting web dashboard...")
    print("Open this in browser: http://127.0.0.1:5000")

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
