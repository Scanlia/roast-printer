"""Printer client — sends to Windows GDI bridge (JSON) and/or Android Termux bridge (ESC/POS).

Both targets use plain TCP on port 9100:
- Windows bridge: receives JSON, renders via GDI driver
- Android bridge: receives raw ESC/POS bytes, forwards to USB printer
"""

import base64
import io
import json
import logging
import socket
import threading

log = logging.getLogger(__name__)

# ---- ESC/POS constants ----
ESC = b"\x1b"
GS  = b"\x1d"

ESC_INIT      = ESC + b"@"
ESC_ALIGN_CTR = ESC + b"a\x01"
GS_CUT        = GS  + b"V\x41\x03"   # partial cut + feed 3mm


def _render_receipt_image(payload: dict, paper_width_dots: int = 512):
    """Render the receipt as a PIL Image (grayscale). Reusable for all methods."""
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np

    image_b64 = payload.get("image_b64", "")
    roast_text = payload.get("roast_text", "")
    timestamp  = payload.get("timestamp", "")

    W = paper_width_dots
    MARGIN = 10

    def load_font(size: int, bold: bool = False):
        candidates = (["consolab.ttf", "consola.ttf"] if bold else ["consola.ttf"]) + \
                     ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
        for name in candidates:
            try:
                return ImageFont.truetype(name, size)
            except (OSError, IOError):
                pass
        return ImageFont.load_default()

    def word_wrap(text, font, max_w):
        words = text.split()
        lines, cur = [], ""
        tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
        for w in words:
            test = f"{cur} {w}" if cur else w
            if tmp.textbbox((0, 0), test, font=font)[2] > max_w and cur:
                lines.append(cur); cur = w
            else:
                cur = test
        if cur:
            lines.append(cur)
        return lines

    def draw_centred(draw, text, y, font):
        bbox = draw.textbbox((0, 0), text, font=font)
        x = (W - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), text, fill=0, font=font)

    font_title  = load_font(32, bold=True)
    font_roast  = load_font(18, bold=True)
    font_footer = load_font(14)

    if image_b64:
        pimg = Image.open(io.BytesIO(base64.b64decode(image_b64)))
        aspect = pimg.height / pimg.width
        iw, ih = W, int(W * aspect)
        pimg = pimg.resize((iw, ih), Image.Resampling.LANCZOS).convert("L")
        arr = np.array(pimg, dtype=np.float32) / 255.0
        arr = np.power(arr, 0.45)
        arr = arr * 0.65 + 0.35
        pimg = Image.fromarray((np.clip(arr, 0, 1) * 255).astype("uint8"), mode="L")
    else:
        pimg = None
        ih = 0

    roast_lines = word_wrap(roast_text, font_roast, W - 2 * MARGIN)
    LINE_H = 24

    height = (MARGIN + 40 + 6 + 8 + ih + 8 + 6 +
              len(roast_lines) * LINE_H + 14 + 4 + 22 + 20 + 6)

    img = Image.new("L", (W, height), 255)
    draw = ImageDraw.Draw(img)
    cy = MARGIN

    draw_centred(draw, "HUMAN DETECTED", cy, font_title); cy += 40
    draw.line([(MARGIN, cy), (W - MARGIN, cy)], fill=0, width=2); cy += 8

    if pimg:
        img.paste(pimg, (0, cy)); cy += ih

    draw.line([(MARGIN, cy), (W - MARGIN, cy)], fill=0, width=2); cy += 10

    for line in roast_lines:
        draw_centred(draw, line, cy, font_roast); cy += LINE_H

    cy += 10
    draw.line([(MARGIN, cy), (W - MARGIN, cy)], fill=0, width=1); cy += 6
    draw_centred(draw, "ROAST-O-MATIC 3000", cy, font_footer); cy += 20
    draw_centred(draw, timestamp, cy, font_footer)

    return img


def _img_to_1bit_bytes(img, W):
    """Convert PIL image to 1-bit packed bytes using Floyd-Steinberg dithering."""
    import numpy as np
    # PIL's .convert("1") uses Floyd-Steinberg by default — good
    bw = img.convert("1")
    px = np.array(bw, dtype=np.uint8)  # 0=black, 255=white
    w_bytes = (W + 7) // 8
    h_rows = px.shape[0]

    rows_bytes = bytearray()
    for row in range(h_rows):
        for byte_idx in range(w_bytes):
            val = 0
            for bit in range(8):
                col = byte_idx * 8 + bit
                if col < W and px[row, col] == 0:
                    val |= (0x80 >> bit)
            rows_bytes.append(val)
    return rows_bytes, w_bytes, h_rows


def _img_to_escpos_gsv0(img, W):
    """Method A: GS v 0 — single raster command, simple threshold."""
    rows_bytes, w_bytes, h_rows = _img_to_1bit_bytes(img, W)
    xL, xH = w_bytes & 0xFF, (w_bytes >> 8) & 0xFF
    yL, yH = h_rows & 0xFF, (h_rows >> 8) & 0xFF

    out = bytearray()
    out += ESC_INIT
    out += GS + b"v0\x00" + bytes([xL, xH, yL, yH]) + bytes(rows_bytes)
    out += b"\n" * 3
    out += GS_CUT
    return bytes(out)


def _img_to_escpos_gsv0_dithered(img, W):
    """Method E: GS v 0 with explicit Floyd-Steinberg dithering.

    The key difference: we apply dithering ourselves at higher quality
    (not just a 50% threshold) to distribute dots more evenly, which
    can help eliminate banding artifacts from uneven dot density.
    """
    import numpy as np
    # Work at grayscale level, apply our own dithering
    gray = img.convert("L")
    arr = np.array(gray, dtype=np.float32)

    # Floyd-Steinberg dithering
    h, w = arr.shape
    for y in range(h):
        for x in range(w):
            old = arr[y, x]
            new = 255.0 if old > 127 else 0.0
            arr[y, x] = new
            err = old - new
            if x + 1 < w:
                arr[y, x + 1] += err * 7 / 16
            if y + 1 < h:
                if x - 1 >= 0:
                    arr[y + 1, x - 1] += err * 3 / 16
                arr[y + 1, x] += err * 5 / 16
                if x + 1 < w:
                    arr[y + 1, x + 1] += err * 1 / 16

    # Convert to packed bytes
    w_bytes = (W + 7) // 8
    h_rows = h
    rows_bytes = bytearray()
    for row in range(h_rows):
        for byte_idx in range(w_bytes):
            val = 0
            for bit in range(8):
                col = byte_idx * 8 + bit
                if col < W and arr[row, col] < 128:
                    val |= (0x80 >> bit)
            rows_bytes.append(val)

    xL, xH = w_bytes & 0xFF, (w_bytes >> 8) & 0xFF
    yL, yH = h_rows & 0xFF, (h_rows >> 8) & 0xFF

    out = bytearray()
    out += ESC_INIT
    out += GS + b"v0\x00" + bytes([xL, xH, yL, yH]) + bytes(rows_bytes)
    out += b"\n" * 3
    out += GS_CUT
    return bytes(out)


def _img_to_escpos_gs_paren_L(img, W):
    """Method F: GS ( L — modern raster graphics command.

    Uses 'Store raster image' + 'Print stored image' which is the
    newer ESC/POS graphics API supported by TM-T88V and later.
    This bypasses the line-feed mechanics entirely.
    """
    import numpy as np
    bw = img.convert("1")
    px = np.array(bw, dtype=np.uint8)

    w_bytes = (W + 7) // 8
    h_rows = px.shape[0]

    # Build raster data
    raster = bytearray()
    for row in range(h_rows):
        for byte_idx in range(w_bytes):
            val = 0
            for bit in range(8):
                col = byte_idx * 8 + bit
                if col < W and px[row, col] == 0:
                    val |= (0x80 >> bit)
            raster.append(val)

    # GS ( L: Store raster image in print buffer
    # Format: GS ( L pL pH m fn a bx by c xL xH yL yH d1...dk
    # m=48 (store to buffer), fn=112 (store raster)
    # a=48 (normal), bx=1, by=1 (scale 1x), c=49 (1-bit)
    data_len = len(raster) + 10  # 10 bytes of parameters before pixel data
    pL = data_len & 0xFF
    pH = (data_len >> 8) & 0xFF
    # For very large images, pL/pH might overflow (max 65535)
    # Use 4-byte length variant if needed
    if data_len > 65535:
        # Fall back to GS v 0 for huge images
        return _img_to_escpos_gsv0(img, W)

    xL = W & 0xFF
    xH = (W >> 8) & 0xFF
    yL = h_rows & 0xFF
    yH = (h_rows >> 8) & 0xFF

    out = bytearray()
    out += ESC_INIT

    # Store image: GS ( L pL pH 48 112 48 1 1 49 xL xH yL yH <data>
    out += GS + b"(L" + bytes([pL, pH])
    out += bytes([48, 112, 48, 1, 1, 49])
    out += bytes([xL, xH, yL, yH])
    out += bytes(raster)

    # Print stored image: GS ( L 2 0 48 50
    out += GS + b"(L\x02\x00\x30\x32"

    out += b"\n" * 3
    out += GS_CUT
    return bytes(out)


def _img_to_escpos_esc_star(img, W):
    """Method B: ESC * 33 — 24-dot band mode with ESC 3 line spacing."""
    import numpy as np
    bw = img.convert("1")
    px = np.array(bw, dtype=np.uint8)

    BAND_H = 24
    nL = W & 0xFF
    nH = (W >> 8) & 0xFF

    out = bytearray()
    out += ESC_INIT
    out += ESC + b"\x33\x18"  # ESC 3 24: line spacing = 24 dots

    for band_top in range(0, bw.height, BAND_H):
        band = px[band_top:band_top + BAND_H]
        if band.shape[0] < BAND_H:
            pad = np.ones((BAND_H - band.shape[0], W), dtype=np.uint8) * 255
            band = np.concatenate([band, pad])

        out += ESC + b"*\x21" + bytes([nL, nH])
        for col in range(W):
            for byte_row in range(3):
                val = 0
                for bit in range(8):
                    row = byte_row * 8 + bit
                    if band[row, col] == 0:
                        val |= (0x80 >> bit)
                out.append(val)
        out += b"\n"

    out += ESC + b"\x32"  # restore default line spacing
    out += b"\n" * 4
    out += GS_CUT
    return bytes(out)


def _make_esc_star_8(feed_n):
    """Factory: ESC * mode 1 (8-dot bands) with ESC J n direct dot feed."""
    def _render(img, W):
        import numpy as np
        bw = img.convert("1")
        px = np.array(bw, dtype=np.uint8)

        BAND_H = 8
        nL = W & 0xFF
        nH = (W >> 8) & 0xFF

        out = bytearray()
        out += ESC_INIT

        for band_top in range(0, bw.height, BAND_H):
            band = px[band_top:band_top + BAND_H]
            if band.shape[0] < BAND_H:
                pad = np.ones((BAND_H - band.shape[0], W), dtype=np.uint8) * 255
                band = np.concatenate([band, pad])

            out += ESC + b"*\x01" + bytes([nL, nH])
            for col in range(W):
                val = 0
                for bit in range(8):
                    if band[bit, col] == 0:
                        val |= (0x80 >> bit)
                out.append(val)
            # ESC J n: feed exactly n dots — NOT \n which uses default spacing
            out += ESC + b"J" + bytes([feed_n])

        out += b"\n" * 3
        out += GS_CUT
        return bytes(out)
    return _render


def _img_to_escpos_gsv0_striped(img, W):
    """Method D: GS v 0 in small row-group chunks (256 rows at a time)."""
    import numpy as np
    bw = img.convert("1")
    w_bytes = (W + 7) // 8
    px = np.array(bw, dtype=np.uint8)

    out = bytearray()
    out += ESC_INIT

    STRIPE = 256
    for stripe_top in range(0, bw.height, STRIPE):
        stripe = px[stripe_top:stripe_top + STRIPE]
        h = stripe.shape[0]
        rows_bytes = bytearray()
        for row in range(h):
            for byte_idx in range(w_bytes):
                val = 0
                for bit in range(8):
                    col = byte_idx * 8 + bit
                    if col < W and stripe[row, col] == 0:
                        val |= (0x80 >> bit)
                rows_bytes.append(val)

        xL, xH = w_bytes & 0xFF, (w_bytes >> 8) & 0xFF
        yL, yH = h & 0xFF, (h >> 8) & 0xFF
        out += GS + b"v0\x00" + bytes([xL, xH, yL, yH]) + bytes(rows_bytes)

    out += b"\n" * 4
    out += GS_CUT
    return bytes(out)


# Map of test method names
ESCPOS_METHODS = {
    "A_gsv0":         _img_to_escpos_gsv0,
    "E_gsv0_dither":  lambda img, W: _img_to_escpos_gsv0_dithered(img, W),
    "F_gs_paren_L":   lambda img, W: _img_to_escpos_gs_paren_L(img, W),
}
DEFAULT_METHOD = "F_gs_paren_L"


def _render_escpos(payload: dict, paper_width_dots: int = 512,
                   method: str = "") -> bytes:
    """Render a receipt payload as ESC/POS bytes using the specified method."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError as e:
        log.error("PIL/numpy not available for ESC/POS rendering: %s", e)
        return b""

    method = method or DEFAULT_METHOD
    W = paper_width_dots
    img = _render_receipt_image(payload, W)
    fn = ESCPOS_METHODS.get(method, _img_to_escpos_esc_star)
    return fn(img, W)
    out += b"\n" * 4
    out += GS_CUT
    return bytes(out)


def _tcp_send(host: str, port: int, data: bytes, timeout: int = 30,
              label: str = "") -> bool:
    """Send raw bytes over a TCP socket."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            s.sendall(data)
        log.info("Sent %d bytes to %s %s:%d", len(data), label, host, port)
        return True
    except socket.timeout:
        log.error("Timeout sending to %s %s:%d", label, host, port)
    except ConnectionRefusedError:
        log.error("Connection refused by %s %s:%d", label, host, port)
    except OSError as exc:
        log.error("Send error to %s %s:%d — %s", label, host, port, exc)
    return False


class PrinterClient:
    """Sends receipt payloads to one or both print targets in parallel.

    - Windows bridge (host/port): receives JSON, renders via GDI multi-tone
    - Android Termux bridge (android_host/android_port): receives raw ESC/POS
    """

    def __init__(
        self,
        host: str = "",
        port: int = 9100,
        timeout: int = 30,
        android_host: str = "",
        android_port: int = 9100,
        android_enabled: bool = False,
        paper_width_dots: int = 576,
    ):
        self.host             = host
        self.port             = port
        self.timeout          = timeout
        self.android_host     = android_host
        self.android_port     = android_port
        self.android_enabled  = android_enabled
        self.paper_width_dots = paper_width_dots

        targets = []
        if host:
            targets.append(f"Windows bridge {host}:{port}")
        if android_enabled and android_host:
            targets.append(f"Android bridge {android_host}:{android_port}")
        log.info("PrinterClient targets: %s", ", ".join(targets) or "none")

    def print_receipt(self, payload: dict) -> bool:
        """Send to all enabled targets in parallel. Returns True if at least one succeeds."""
        results: list[bool] = []
        lock = threading.Lock()

        def send_windows():
            data = json.dumps(payload).encode("utf-8")
            ok = _tcp_send(self.host, self.port, data, self.timeout, "Windows")
            with lock:
                results.append(ok)

        def send_android():
            log.info("Rendering ESC/POS for Android bridge (%dpx wide)...",
                     self.paper_width_dots)
            escpos = _render_escpos(payload, self.paper_width_dots)
            if not escpos:
                with lock:
                    results.append(False)
                return
            log.info("ESC/POS rendered: %d bytes", len(escpos))
            ok = _tcp_send(self.android_host, self.android_port, escpos,
                           self.timeout, "Android")
            with lock:
                results.append(ok)

        threads = []
        if self.host:
            threads.append(threading.Thread(target=send_windows, daemon=True))
        if self.android_enabled and self.android_host:
            threads.append(threading.Thread(target=send_android, daemon=True))

        if not threads:
            log.warning("No print targets configured")
            return False

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=self.timeout + 5)

        return any(results)

    def check_health(self) -> bool:
        """Quick TCP connect test against the primary target."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(3)
                s.connect((self.host, self.port))
            return True
        except OSError:
            return False

