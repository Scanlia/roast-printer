#!/usr/bin/env python3
"""
Roast Printer — main event loop.

Polls UniFi Protect for person detections on the configured camera,
sends the thumbnail to Gemini for an outfit roast, dithers the image,
and prints the result on an Epson TM-T88V via the ESP32 bridge.

Dashboard: http://<host>:8899
"""

import base64
import signal
import sys
import time
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import Config
from unifi_protect import UniFiProtectClient
from gemini_roast import GeminiRoaster
from image_processor import ImageProcessor
from printer_client import PrinterClient
from person_detector import PersonDetector
from web_dashboard import (
    WebLogHandler, set_latest_roast, set_latest_payload,
    set_printer_client, set_cooldown, get_cooldown, start_dashboard,
)

# ---- logging: stdout + file + web dashboard ----
LOG_DIR = Path("/data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# stdout
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(fmt)
root_logger.addHandler(_sh)

# rotating file (5 MB x 3)
_fh = logging.handlers.RotatingFileHandler(
    LOG_DIR / "roast-printer.log", maxBytes=5_000_000, backupCount=3
)
_fh.setFormatter(fmt)
root_logger.addHandler(_fh)

# web dashboard ring buffer
_wh = WebLogHandler()
_wh.setFormatter(logging.Formatter("%(name)s: %(message)s"))
root_logger.addHandler(_wh)

log = logging.getLogger("roast-printer")

STATE_FILE = Path("/data/last_event_ts")


def load_last_ts() -> int:
    """Load the timestamp (ms) of the last processed event."""
    try:
        return int(STATE_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_last_ts(ts: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(str(ts))


def main() -> None:
    cfg = Config.from_env()

    # ---- start web dashboard ----
    start_dashboard(port=8899)

    protect = UniFiProtectClient(
        host=cfg.protect_host,
        username=cfg.protect_username,
        password=cfg.protect_password,
        api_key=cfg.protect_api_key,
        verify_ssl=cfg.protect_verify_ssl,
    )
    roaster = GeminiRoaster(api_key=cfg.gemini_api_key, model=cfg.gemini_model)
    processor = ImageProcessor(receipt_width=cfg.receipt_width_px)
    printer = PrinterClient(
        host=cfg.esp32_host,
        port=cfg.esp32_port,
        android_host=cfg.android_host,
        android_port=cfg.android_port,
        android_enabled=cfg.android_enabled,
        paper_width_dots=cfg.receipt_width_px,
    )
    set_printer_client(printer)  # enable dashboard reprint button
    set_cooldown(cfg.cooldown_seconds)  # seed dashboard with .env value
    detector = PersonDetector(confidence=0.35)

    # ---- authenticate & find camera ----
    log.info("Connecting to UniFi Protect...")
    while not protect.login():
        log.warning("Login failed — retrying in 30s")
        time.sleep(30)

    camera_id = None
    while not camera_id:
        camera_id = protect.find_camera(cfg.camera_name)
        if not camera_id:
            log.error("Camera '%s' not found — retrying in 30s. Available:", cfg.camera_name)
            for c in protect.list_cameras():
                log.error("  - %s  (%s)", c["name"], c["id"])
            time.sleep(30)

    # ---- state ----
    processed: set[str] = set()
    last_roast_time: float = 0
    last_event_ts = load_last_ts()

    # ---- graceful shutdown ----
    running = True

    def _shutdown(sig, _frame):
        nonlocal running
        log.info("Caught %s — shutting down", signal.Signals(sig).name)
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("Roast Printer started — watching '%s'", cfg.camera_name)
    log.info(
        "ESP32 printer at %s:%d  |  cooldown %ds  |  poll every %ds",
        cfg.esp32_host, cfg.esp32_port,
        cfg.cooldown_seconds, cfg.poll_interval_seconds,
    )

    # ---- main loop ----
    while running:
        try:
            events = protect.get_smart_detections(
                camera_id=camera_id,
                detection_type="person",
                lookback_seconds=30,
            )

            for event in events:
                eid = event.get("id", "")
                ets = event.get("start", 0)

                if eid in processed or ets <= last_event_ts:
                    continue

                # cooldown (read live so dashboard changes take effect immediately)
                now = time.time()
                if now - last_roast_time < get_cooldown():
                    log.debug("Cooldown active — skipping %s", eid)
                    processed.add(eid)
                    continue

                log.info("🔥 Person detected — event %s", eid)

                try:
                    # 1. Grab 3 frames 1s apart, pick the one with the
                    #    largest person bounding box (best-framed shot)
                    best_image: Optional[bytes] = None
                    best_bbox: Optional[list] = None
                    best_area: float = -1.0

                    for frame_idx in range(3):
                        if frame_idx > 0:
                            time.sleep(1.0)

                        frame = protect.get_rtsp_frame(camera_id)
                        if frame is None:
                            frame = protect.get_camera_snapshot(camera_id)
                        if frame is None:
                            frame = protect.get_event_thumbnail(eid, width=1024)
                        if frame is None:
                            log.debug("Frame %d: no image", frame_idx)
                            continue

                        bbox = detector.get_best_person_bbox(frame)
                        if bbox:
                            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                            log.info("Frame %d: person bbox area=%.0f", frame_idx, area)
                            if area > best_area:
                                best_area = area
                                best_image = frame
                                best_bbox = bbox
                        else:
                            log.info("Frame %d: no person detected", frame_idx)
                            # Keep as fallback if we never find a person
                            if best_image is None:
                                best_image = frame

                    if best_image is None:
                        log.warning("No image available for event %s", eid)
                        processed.add(eid)
                        continue

                    raw_image = best_image
                    bbox = best_bbox
                    log.info("Best frame: bbox area=%.0f", best_area)

                    # Square crop for the LLM (more context for roasting)
                    llm_crop = processor.crop_for_llm(raw_image, bbox=bbox)

                    # 3:2 landscape crop for the receipt (compact on paper)
                    receipt_crop = processor.crop_for_receipt(raw_image, bbox=bbox)

                    # 2. Generate roast (send square crop to LLM)
                    roast_text = roaster.roast_outfit(llm_crop)
                    log.info("Roast: %s", roast_text)

                    # Push to web dashboard (show LLM crop)
                    set_latest_roast(
                        roast_text,
                        image_b64=base64.b64encode(llm_crop).decode(),
                    )

                    # 3. Build JSON payload for the Windows print bridge
                    #    (bridge renders through the driver with multi-tone)
                    ts = datetime.now()
                    payload = {
                        "image_b64": base64.b64encode(receipt_crop).decode(),
                        "roast_text": roast_text,
                        "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    }

                    # Store for reprint
                    set_latest_payload(payload)

                    # 4. Print
                    if printer.print_receipt(payload):
                        log.info("✅ Receipt sent to bridge (%d bytes image)",
                                 len(receipt_crop))
                        last_roast_time = time.time()
                    else:
                        log.error("Printing failed for event %s", eid)

                except Exception:
                    log.exception("Error processing event %s", eid)

                processed.add(eid)
                if ets > last_event_ts:
                    last_event_ts = ets
                    save_last_ts(last_event_ts)

            # Keep the set from growing forever
            if len(processed) > 1000:
                processed = set(list(processed)[-500:])

        except Exception:
            log.exception("Error in main loop — retrying in 10 s")
            time.sleep(10)
            continue

        time.sleep(cfg.poll_interval_seconds)


if __name__ == "__main__":
    main()
