"""
Roast Printer - Windows Print Bridge (Multi-Tone GDI)

Receives JSON payloads from the Docker container over TCP :9100,
renders the receipt as a high-resolution grayscale image, and prints
through the Windows GDI printer driver.

The driver's multi-tone mode (set in Printer Preferences) handles
all halftoning — we send a clean grayscale image at 2x native
resolution (1152px wide) for maximum detail.

Usage:
  1.  pip install pywin32 Pillow numpy
  2.  python print_bridge.py              (auto-detects TM-T88V)
  3.  python print_bridge.py "EPSON TM-T88V Receipt"   (explicit name)
"""

import base64
import io
import json
import socket
import sys
import threading
import time
from datetime import datetime

try:
    import win32print
    import win32ui
    import win32con
except ImportError:
    print("ERROR: pywin32 is required.  Install with:")
    print("  pip install pywin32")
    sys.exit(1)

try:
    from PIL import Image, ImageDraw, ImageFont, ImageWin
except ImportError:
    print("ERROR: Pillow is required.  Install with:")
    print("  pip install Pillow")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    print("ERROR: NumPy is required.  Install with:")
    print("  pip install numpy")
    sys.exit(1)


# ---- Receipt layout constants ----
# Render at 2x native resolution (1152px) for extra detail.
# GDI scales this to the printer's 576-dot width; the driver's
# multi-tone mode gets more grayscale data to work with.
SCALE = 2
RECEIPT_WIDTH = 576 * SCALE   # 1152px
MARGIN = 10 * SCALE
TEXT_AREA = RECEIPT_WIDTH - 2 * MARGIN


def find_epson_printer() -> str:
    """Auto-detect the TM-T88V from installed printers."""
    printers = [p[2] for p in win32print.EnumPrinters(
        win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    )]
    print(f"Installed printers ({len(printers)}):")
    for p in printers:
        print(f"  - {p}")
        if "tm-t88" in p.lower() or "epson" in p.lower():
            print(f"  >>> Auto-selected: {p}")
            return p
    return ""


def load_font(size: int, bold: bool = False):
    """Try to load a monospace TrueType font, fall back to default."""
    names = ["consolab.ttf", "consola.ttf"] if bold else ["consola.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    for name in names:
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            pass
    return ImageFont.load_default()


def brighten_for_thermal(img: Image.Image) -> Image.Image:
    """Aggressively brighten a grayscale image for thermal printing.

    Thermal printers crush shadows — a value of 80 on screen looks
    near-black on paper.  This applies gamma correction + linear lift
    so midtones stay visible after thermal transfer.
    """
    arr = np.array(img, dtype=np.float32) / 255.0
    arr = np.power(arr, 0.45)  # gamma lifts midtones/shadows without clipping highlights
    arr = arr * 0.65 + 0.35  # whites → ~93%, blacks → ~35% (uniform brightness lift)
    arr = np.clip(arr, 0, 1)
    return Image.fromarray((arr * 255).astype(np.uint8), mode="L")


def render_receipt(payload: dict) -> Image.Image:
    """Render a receipt as a high-res grayscale PIL image.

    Rendered at 2x native resolution (1152px wide) in grayscale.
    The Windows driver's multi-tone mode handles all halftoning.
    """
    image_b64 = payload.get("image_b64", "")
    roast_text = payload.get("roast_text", "")
    timestamp = payload.get("timestamp", "")

    # Load fonts (scaled up for 2x rendering)
    font_title = load_font(42 * SCALE, bold=True)
    font_roast = load_font(24 * SCALE, bold=True)
    font_footer = load_font(16 * SCALE)

    # Decode the person image
    if image_b64:
        person_img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
        aspect = person_img.height / person_img.width
        img_w = RECEIPT_WIDTH
        img_h = int(img_w * aspect)
        person_img = person_img.resize((img_w, img_h), Image.Resampling.LANCZOS)
        person_img = person_img.convert("L")
        person_img = brighten_for_thermal(person_img)
        print(f"  Image: {img_w}x{img_h} grayscale (no dithering, driver multi-tone)")
    else:
        person_img = None
        img_h = 0

    # Word-wrap the roast text
    roast_lines = word_wrap(roast_text, font_roast, TEXT_AREA)

    # Calculate total receipt height
    y = MARGIN
    y += 50 * SCALE   # title
    y += 4 * SCALE    # separator
    y += 8 * SCALE    # padding
    if person_img:
        y += img_h
    y += 4 * SCALE    # separator
    y += 14 * SCALE   # padding
    y += len(roast_lines) * 30 * SCALE  # roast text lines
    y += 18 * SCALE   # padding
    y += 2 * SCALE    # separator
    y += 24 * SCALE   # "ROAST-O-MATIC 3000"
    y += 22 * SCALE   # timestamp
    y += 80 * SCALE   # bottom margin

    # Create grayscale receipt
    receipt = Image.new("L", (RECEIPT_WIDTH, y), 255)
    draw = ImageDraw.Draw(receipt)
    cy = MARGIN

    # ---- Title ----
    draw_centred(draw, "HUMAN DETECTED", cy, font_title, RECEIPT_WIDTH)
    cy += 50 * SCALE

    # ---- Separator ----
    draw.line([(MARGIN, cy), (RECEIPT_WIDTH - MARGIN, cy)], fill=0, width=2 * SCALE)
    cy += 8 * SCALE

    # ---- Person image (grayscale, NOT dithered) ----
    if person_img:
        receipt.paste(person_img, (0, cy))
        cy += img_h

    # ---- Separator ----
    draw.line([(MARGIN, cy), (RECEIPT_WIDTH - MARGIN, cy)], fill=0, width=2 * SCALE)
    cy += 14 * SCALE

    # ---- Roast text ----
    for line in roast_lines:
        draw_centred(draw, line, cy, font_roast, RECEIPT_WIDTH)
        cy += 30 * SCALE

    cy += 14 * SCALE

    # ---- Footer separator ----
    draw.line([(MARGIN, cy), (RECEIPT_WIDTH - MARGIN, cy)], fill=0, width=1 * SCALE)
    cy += 8 * SCALE

    draw_centred(draw, "ROAST-O-MATIC 3000", cy, font_footer, RECEIPT_WIDTH)
    cy += 22 * SCALE

    draw_centred(draw, timestamp, cy, font_footer, RECEIPT_WIDTH)

    print(f"  Receipt: {receipt.width}x{receipt.height}px grayscale "
          f"({receipt.height / (203.0 * SCALE) * 25.4:.0f}mm tall, "
          f"{len(roast_lines)} text lines)")

    # Return as grayscale — driver multi-tone handles all halftoning
    return receipt


def draw_centred(draw: ImageDraw.Draw, text: str, y: int,
                 font, width: int) -> None:
    """Draw text centred horizontally."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    x = (width - tw) // 2
    draw.text((x, y), text, fill=0, font=font)


def word_wrap(text: str, font, max_width: int) -> list:
    """Word-wrap text to fit within max_width pixels."""
    words = text.split()
    lines = []
    cur = ""
    tmp = Image.new("L", (1, 1))
    d = ImageDraw.Draw(tmp)
    for w in words:
        test = f"{cur} {w}" if cur else w
        bbox = d.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


def print_via_gdi(printer_name: str, receipt: Image.Image) -> bool:
    """Print a grayscale PIL image through GDI.

    The driver's multi-tone mode (configured in Printer Preferences)
    handles all halftoning.  We just send a clean grayscale image.
    """
    try:
        hDC = win32ui.CreateDC()
        hDC.CreatePrinterDC(printer_name)

        printer_dpi_x = hDC.GetDeviceCaps(win32con.LOGPIXELSX)
        printer_dpi_y = hDC.GetDeviceCaps(win32con.LOGPIXELSY)
        print(f"  Printer DPI: {printer_dpi_x}x{printer_dpi_y}")

        # Our image is at 203*SCALE DPI (406 DPI at SCALE=2)
        # Scale to printer device units
        render_dpi = 203.0 * SCALE
        scale_x = printer_dpi_x / render_dpi
        scale_y = printer_dpi_y / render_dpi
        img_w = int(receipt.width * scale_x)
        img_h = int(receipt.height * scale_y)

        hDC.StartDoc("RoastPrint")
        hDC.StartPage()

        dib = ImageWin.Dib(receipt)
        dib.draw(hDC.GetHandleOutput(), (0, 0, img_w, img_h))

        hDC.EndPage()
        hDC.EndDoc()
        hDC.DeleteDC()
        return True

    except Exception as exc:
        print(f"[ERROR] GDI print failed: {exc}")
        import traceback
        traceback.print_exc()
        return False


def handle_client(conn: socket.socket, addr, printer_name: str):
    """Receive JSON payload from Docker and print via GDI."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] Client connected: {addr[0]}:{addr[1]}")

    data = bytearray()
    conn.settimeout(5.0)
    try:
        while True:
            try:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
            except socket.timeout:
                break
    finally:
        conn.close()

    if not data:
        print(f"[{ts}] Empty connection from {addr[0]}")
        return

    print(f"[{ts}] Received {len(data)} bytes")

    try:
        payload = json.loads(data.decode("utf-8"))
        print(f"[{ts}] JSON payload: image={len(payload.get('image_b64',''))} chars, "
              f"roast={len(payload.get('roast_text',''))} chars")

        receipt = render_receipt(payload)
        print(f"[{ts}] Rendered: {receipt.width}x{receipt.height} (mode={receipt.mode})")

        ok = print_via_gdi(printer_name, receipt)
        status = "OK" if ok else "FAILED"
        print(f"[{ts}] GDI print {status}")

    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"[{ts}] Not JSON - sending as raw ESC/POS ({len(data)} bytes)")
        ok = send_raw(printer_name, bytes(data))
        status = "OK" if ok else "FAILED"
        print(f"[{ts}] RAW print {status}")


def send_raw(printer_name: str, data: bytes) -> bool:
    """Fallback: send raw bytes (ESC/POS) to printer."""
    CHUNK = 1024
    DELAY = 0.025
    try:
        handle = win32print.OpenPrinter(printer_name)
        try:
            win32print.StartDocPrinter(handle, 1, ("RoastPrint", None, "RAW"))
            try:
                win32print.StartPagePrinter(handle)
                offset = 0
                while offset < len(data):
                    end = min(offset + CHUNK, len(data))
                    win32print.WritePrinter(handle, data[offset:end])
                    offset = end
                    if offset < len(data):
                        time.sleep(DELAY)
                win32print.EndPagePrinter(handle)
            finally:
                win32print.EndDocPrinter(handle)
        finally:
            win32print.ClosePrinter(handle)
        return True
    except Exception as exc:
        print(f"[ERROR] RAW print failed: {exc}")
        return False


def main():
    if len(sys.argv) > 1:
        printer_name = sys.argv[1]
    else:
        printer_name = find_epson_printer()
        if not printer_name:
            print("\nCould not auto-detect Epson printer.")
            print('Run with explicit name:  python print_bridge.py "Your Printer Name"')
            sys.exit(1)

    print(f"\n{'='*50}")
    print(f"  Roast Printer - Windows Print Bridge")
    print(f"  Mode    : Grayscale GDI (driver multi-tone)")
    print(f"  Printer : {printer_name}")
    print(f"  Listen  : 0.0.0.0:9100")
    print(f"{'='*50}\n")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 9100))
    server.listen(5)
    print("Waiting for print jobs...\n")

    try:
        while True:
            conn, addr = server.accept()
            thread = threading.Thread(
                target=handle_client,
                args=(conn, addr, printer_name),
                daemon=True,
            )
            thread.start()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.close()


if __name__ == "__main__":
    main()
