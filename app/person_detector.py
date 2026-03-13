"""YOLOv8-nano person detector — runs on CPU, ~6 MB model."""

import io
import logging
from typing import Optional

import numpy as np
from PIL import Image
from ultralytics import YOLO

log = logging.getLogger(__name__)

# COCO class 0 = person
_PERSON_CLASS = 0


class PersonDetector:
    """Lightweight YOLOv8n person detector."""

    def __init__(self, confidence: float = 0.35):
        self.confidence = confidence
        log.info("Loading YOLOv8n model...")
        self.model = YOLO("yolov8n.pt")
        log.info("YOLOv8n ready (conf=%.2f)", confidence)

    def detect_persons(self, image_bytes: bytes) -> list[dict]:
        """Return list of person detections with bounding boxes.

        Each result: {"bbox": [x1, y1, x2, y2], "confidence": float}
        where coords are absolute pixels (xyxy format).
        """
        img = Image.open(io.BytesIO(image_bytes))
        results = self.model(img, conf=self.confidence, classes=[_PERSON_CLASS],
                             verbose=False)

        persons = []
        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                conf = float(box.conf[0])
                persons.append({
                    "bbox": [x1, y1, x2, y2],
                    "confidence": conf,
                })

        # Sort by area descending (largest = closest person)
        persons.sort(key=lambda p: (p["bbox"][2] - p["bbox"][0]) *
                                    (p["bbox"][3] - p["bbox"][1]),
                      reverse=True)

        if persons:
            log.info("Detected %d person(s), best conf=%.2f",
                     len(persons), persons[0]["confidence"])
        else:
            log.info("No persons detected by YOLO")

        return persons

    def get_best_person_bbox(self, image_bytes: bytes) -> Optional[list]:
        """Return [x1, y1, x2, y2] of the largest detected person, or None."""
        persons = self.detect_persons(image_bytes)
        if persons:
            return persons[0]["bbox"]
        return None
