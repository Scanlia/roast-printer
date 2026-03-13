"""Image processing: person cropping, dithering & ESC/POS receipt building."""

import io
import logging
from datetime import datetime
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


class ImageProcessor:
    """Prepare images for the Epson TM-T88V thermal receipt printer."""

    def __init__(self, receipt_width: int = 576):
        self.receipt_width = receipt_width

    # ---- cropping ----------------------------------------------------

    def _crop_around_person(
        self,
        image_bytes: bytes,
        bbox: Optional[list],
        aspect_ratio: float,
        padding: float,
        fallback_width_frac: float,
        fallback_cx_frac: float,
    ) -> bytes:
        """Generic crop centred on the YOLO bbox with target aspect ratio.

        *aspect_ratio*: height / width (e.g. 1.0 = square, 0.67 = 3:2 landscape)
        *padding*: multiplier around the detection box (1.1 = 10 % padding)
        """
        img = Image.open(io.BytesIO(image_bytes))
        iw, ih = img.size

        if bbox and len(bbox) >= 4:
            bx1, by1, bx2, by2 = (
                int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
            )
            bw = bx2 - bx1
            bh = by2 - by1
            cx = (bx1 + bx2) // 2
            cy = (by1 + by2) // 2

            # Size the crop to contain the person with padding
            crop_w = int(max(bw, bh / aspect_ratio) * padding)
            crop_h = int(crop_w * aspect_ratio)
            # If person taller than crop box, grow from height
            if bh * padding > crop_h:
                crop_h = int(bh * padding)
                crop_w = int(crop_h / aspect_ratio)
        else:
            # Fallback: left-biased crop
            crop_w = int(iw * fallback_width_frac)
            crop_h = int(crop_w * aspect_ratio)
            cx = int(iw * fallback_cx_frac)
            cy = ih // 2

        # Clamp to image bounds
        x1 = max(0, cx - crop_w // 2)
        y1 = max(0, cy - crop_h // 2)
        x2 = min(iw, x1 + crop_w)
        y2 = min(ih, y1 + crop_h)
        x1 = max(0, x2 - crop_w)
        y1 = max(0, y2 - crop_h)

        cropped = img.crop((x1, y1, x2, y2))
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    def crop_for_llm(
        self, image_bytes: bytes, bbox: Optional[list] = None
    ) -> bytes:
        """Square crop with 15 % padding — gives the LLM outfit context."""
        result = self._crop_around_person(
            image_bytes, bbox,
            aspect_ratio=1.0,
            padding=1.15,
            fallback_width_frac=0.5,
            fallback_cx_frac=0.30,
        )
        img = Image.open(io.BytesIO(result))
        log.info("LLM crop: %dx%d (square)", img.width, img.height)
        return result

    def crop_for_receipt(
        self, image_bytes: bytes, bbox: Optional[list] = None
    ) -> bytes:
        """3:2 landscape crop, tight — compact on receipt paper."""
        result = self._crop_around_person(
            image_bytes, bbox,
            aspect_ratio=2.0 / 3.0,  # landscape 3:2
            padding=1.1,
            fallback_width_frac=0.55,
            fallback_cx_frac=0.30,
        )
        img = Image.open(io.BytesIO(result))
        log.info("Receipt crop: %dx%d (3:2)", img.width, img.height)
        return result

    # ---- dithering ---------------------------------------------------

    def prepare_for_receipt(self, image_bytes: bytes) -> Image.Image:
        """JPEG bytes -> 1-bit dithered PIL Image sized for receipt."""
        img = Image.open(io.BytesIO(image_bytes))

        # Resize to receipt width, preserving aspect
        aspect = img.height / img.width
        new_h = int(self.receipt_width * aspect)
        img = img.resize(
            (self.receipt_width, new_h), Image.Resampling.LANCZOS
        )

        # Grayscale with contrast + brightness boost for thermal
        img = img.convert("L")
        arr = np.array(img, dtype=np.float32)
        mean = arr.mean()
        arr = np.clip((arr - mean) * 1.2 + mean + 30, 0, 255)
        img = Image.fromarray(arr.astype(np.uint8), mode="L")

        # Floyd-Steinberg dither to 1-bit
        img = img.convert("1")

        log.info("Dithered: %dx%d", img.width, img.height)
        return img

    # ---- receipt building --------------------------------------------

    def build_receipt(
        self,
        image: Image.Image,
        roast_text: str,
        timestamp: datetime,
    ) -> bytes:
        """Assemble a complete ESC/POS byte stream for one receipt."""
        buf = bytearray()

        # ---- initialise printer ----
        buf += b"\x1B\x40"  # ESC @

        # ---- header ----
        buf += b"\x1B\x61\x01"  # centre
        buf += b"\x1D\x21\x11"  # double width + height
        buf += b"\x1B\x45\x01"  # bold on
        buf += b"OUTFIT ROAST\n"
        buf += b"\x1D\x21\x00"  # normal size
        buf += b"\x1B\x45\x00"  # bold off
        buf += self._line("=") + b"\n"

        # ---- dithered image ----
        buf += self._image_to_raster(image)
        buf += self._line("=") + b"\n\n"

        # ---- roast text ----
        buf += b"\x1B\x61\x01"  # centre
        buf += b"\x1B\x45\x01"  # bold
        for line in self._word_wrap(roast_text, 42):
            buf += line.encode("ascii", errors="replace") + b"\n"
        buf += b"\x1B\x45\x00"  # bold off
        buf += b"\n"

        # ---- footer ----
        buf += self._line("-") + b"\n"
        buf += b"ROAST-O-MATIC 3000\n"
        buf += timestamp.strftime("%Y-%m-%d %H:%M:%S").encode() + b"\n"
        buf += b"\n"

        # ---- feed to cutter + partial cut ----
        buf += b"\x1D\x56\x42\x00"

        return bytes(buf)

    # ---- raster encoding (ESC * column mode) ------------------------

    def _image_to_raster(self, img: Image.Image) -> bytes:
        """Convert 1-bit PIL image to a single GS v 0 raster command.

        Sends the entire image as ONE command — no strips, no seams,
        no timing-dependent gaps.  The printer client handles throttled
        delivery (1 KB chunks with 20 ms pauses) to prevent the
        printer's receive buffer from overflowing.
        """
        if img.mode != "1":
            img = img.convert("1")

        w, h = img.size

        # Pad width to multiple of 8
        if w % 8:
            new_w = (w // 8 + 1) * 8
            padded = Image.new("1", (new_w, h), 1)
            padded.paste(img, (0, 0))
            img = padded
            w = new_w

        w_bytes = w // 8

        # PIL "1" -> numpy; 0=black, 255=white.  ESC/POS: 1=black.
        arr = np.array(img, dtype=np.uint8)
        arr = (arr == 0).astype(np.uint8)

        # Pack 8 pixels per byte, MSB first
        arr_reshaped = arr.reshape(h, w_bytes, 8)
        weights = np.array([128, 64, 32, 16, 8, 4, 2, 1], dtype=np.uint8)
        packed = np.sum(arr_reshaped * weights, axis=2).astype(np.uint8)
        raster_data = packed.tobytes()

        # Centre image
        buf = b"\x1B\x61\x01"

        # GS v 0: 1D 76 30 m xL xH yL yH data
        buf += bytes([
            0x1D, 0x76, 0x30, 0x00,
            w_bytes & 0xFF, (w_bytes >> 8) & 0xFF,
            h & 0xFF, (h >> 8) & 0xFF,
        ])
        buf += raster_data

        log.info("Raster: %dx%d single GS v 0 (%d bytes)",
                 w, h, len(raster_data))
        return buf

    @staticmethod
    def _word_wrap(text: str, width: int) -> list[str]:
        words = text.split()
        lines: list[str] = []
        cur = ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > width:
                lines.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}" if cur else w
        if cur:
            lines.append(cur)
        return lines

    @staticmethod
    def _line(char: str = "=", width: int = 42) -> bytes:
        return (char * width).encode("ascii")
