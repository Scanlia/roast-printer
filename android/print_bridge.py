#!/usr/bin/env python3
"""
Termux Print Bridge — receives raw ESC/POS bytes over TCP and
forwards them to a USB receipt printer via termux-usb + libusb.

Run on Android tablet via Termux:
    pkg install python termux-api libusb
    python print_bridge.py

Also install the Termux:API app from F-Droid.

How it works:
  - TCP server listens on port 9100
  - When data arrives, saves to temp file
  - Calls `termux-usb -e` which opens the device through Android's
    USB API and passes us the raw file descriptor
  - We use libusb's wrap_sys_device() via ctypes to do a proper
    USB bulk transfer to the printer's OUT endpoint
"""

import ctypes
import ctypes.util
import json
import os
import socket
import subprocess
import sys
import time

HOST = "0.0.0.0"
PORT = 9100
TEMP_FILE = "/data/data/com.termux/files/home/.print_job.bin"

# libusb constants
LIBUSB_ENDPOINT_OUT = 0x00
LIBUSB_TRANSFER_TYPE_BULK = 0x02
LIBUSB_SUCCESS = 0


def _load_libusb():
    """Load libusb shared library."""
    paths = [
        "/data/data/com.termux/files/usr/lib/libusb-1.0.so",
        "/data/data/com.termux/files/usr/lib/libusb-1.0.so.0",
    ]
    for p in paths:
        if os.path.exists(p):
            return ctypes.CDLL(p)
    # Fallback
    name = ctypes.util.find_library("usb-1.0")
    if name:
        return ctypes.CDLL(name)
    return None


def _usb_write_via_libusb(fd):
    """Use libusb wrap_sys_device to write data to printer via fd."""
    if not os.path.exists(TEMP_FILE):
        print("  No print job file found", file=sys.stderr)
        return False

    with open(TEMP_FILE, "rb") as f:
        data = f.read()

    lib = _load_libusb()
    if not lib:
        print("  ERROR: Cannot load libusb", file=sys.stderr)
        return False

    # Types
    ctx_p = ctypes.c_void_p()
    handle_p = ctypes.c_void_p()
    transferred = ctypes.c_int(0)

    # Disable device discovery (requires root on Android) — MUST be before init
    for opt in (5, 2):
        rc = lib.libusb_set_option(ctypes.c_void_p(None), ctypes.c_int(opt))
        if rc == 0:
            break

    rc = lib.libusb_init(ctypes.byref(ctx_p))
    if rc != 0:
        print(f"  libusb_init failed: {rc}", file=sys.stderr)
        return False

    # Wrap the Android USB fd
    rc = lib.libusb_wrap_sys_device(ctx_p, fd, ctypes.byref(handle_p))
    if rc != 0:
        print(f"  libusb_wrap_sys_device failed: {rc}", file=sys.stderr)
        lib.libusb_exit(ctx_p)
        return False

    # Claim interface 0
    rc = lib.libusb_claim_interface(handle_p, 0)
    if rc != 0:
        print(f"  libusb_claim_interface failed: {rc}", file=sys.stderr)

    # Endpoint 0x01 — standard bulk OUT for most receipt printers
    ep_out = 0x01

    # Send data in small chunks with delays for flow control
    CHUNK = 512
    offset = 0
    ok = True
    while offset < len(data):
        chunk = data[offset:offset + CHUNK]
        buf = ctypes.create_string_buffer(chunk)
        transferred.value = 0
        rc = lib.libusb_bulk_transfer(
            handle_p, ep_out,
            buf, len(chunk),
            ctypes.byref(transferred),
            5000,  # 5s timeout
        )
        if rc != 0:
            print(f"  bulk_transfer failed at offset {offset}/{len(data)}: rc={rc}",
                  file=sys.stderr)
            ok = False
            break
        offset += transferred.value
        # Small delay every few KB to let the printer process
        if offset % 4096 < CHUNK:
            time.sleep(0.02)

    lib.libusb_release_interface(handle_p, 0)
    lib.libusb_close(handle_p)
    lib.libusb_exit(ctx_p)

    if ok:
        print(f"  USB: wrote {offset} bytes")
    return ok


# =============================================
#  USB write mode — called by `termux-usb -e`
# =============================================
def _usb_mode(fd_str):
    fd = int(fd_str)
    success = _usb_write_via_libusb(fd)
    sys.exit(0 if success else 1)


# =============================================
#  TCP server mode — normal startup
# =============================================
def _get_usb_device():
    """Get the first USB device path via termux-usb."""
    try:
        r = subprocess.run(["termux-usb", "-l"],
                           capture_output=True, text=True, timeout=10)
        if r.stdout.strip():
            devices = json.loads(r.stdout.strip())
            if devices:
                return devices[0]
    except Exception as e:
        print(f"  termux-usb -l failed: {e}")
    return None


def _request_permission(dev_path):
    """Request Android USB permission."""
    try:
        print(f"Requesting USB permission for {dev_path} ...")
        subprocess.run(["termux-usb", "-r", dev_path], timeout=30)
        print("Permission granted")
        return True
    except Exception as e:
        print(f"Permission request failed: {e}")
        return False


def _send_to_printer(dev_path, data):
    """Write data to USB printer via termux-usb -e."""
    with open(TEMP_FILE, "wb") as f:
        f.write(data)

    try:
        script_path = os.path.abspath(sys.argv[0])
        result = subprocess.run(
            ["termux-usb", "-e", f"python {script_path}", dev_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.stderr.strip():
            print(f"  {result.stderr.strip()}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("  ERROR: termux-usb timed out")
        return False
    except Exception as e:
        print(f"  ERROR: {e}")
        return False
    finally:
        try:
            os.unlink(TEMP_FILE)
        except OSError:
            pass


def main():
    # If called with a numeric fd argument by termux-usb -e
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        _usb_mode(sys.argv[1])
        return

    print("=" * 40)
    print("  Termux Print Bridge")
    print(f"  Listening on port {PORT}")
    print("=" * 40)

    dev_path = _get_usb_device()
    if dev_path:
        print(f"USB device: {dev_path}")
        _request_permission(dev_path)
    else:
        print("WARNING: No USB device found. Is the printer plugged in?")
        print("Will scan again when a print job arrives.\n")

    # Test print
    if dev_path:
        ESC = b'\x1b'
        GS = b'\x1d'
        test = (ESC + b'@' + ESC + b'a\x01' +
                b'Print bridge ready!\n\n' + GS + b'V\x41\x03')
        print("Sending test print...")
        if _send_to_printer(dev_path, test):
            print("Test print OK!\n")
        else:
            print("Test print failed — check USB connection\n")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(2)
    print(f"Waiting for connections on {HOST}:{PORT} ...\n")

    while True:
        conn, addr = srv.accept()
        print(f"--- Connection from {addr[0]}:{addr[1]} ---")

        chunks = []
        conn.settimeout(5)
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        conn.close()

        data = b"".join(chunks)
        if not data:
            print("  Empty payload, skipping")
            continue

        print(f"  Received {len(data)} bytes")

        if not dev_path:
            dev_path = _get_usb_device()
            if dev_path:
                _request_permission(dev_path)

        if not dev_path:
            print("  ERROR: No USB printer found!")
            continue

        if _send_to_printer(dev_path, data):
            print(f"  Printed OK")
        else:
            print(f"  Print FAILED — rescanning USB...")
            dev_path = _get_usb_device()
            if dev_path:
                _request_permission(dev_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
