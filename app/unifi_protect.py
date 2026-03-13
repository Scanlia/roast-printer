"""UniFi Protect API client for smart-detection events."""

import os
import time
import logging
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)


class UniFiProtectClient:
    """Lightweight client for the UniFi Protect REST API (UDM / UNVR).

    Supports two auth modes:
    - **API key** (preferred): set *api_key* — no login needed, no 403s.
      Create at: UniFi OS → Settings → Admins & Users → (user) → API Keys
    - **Username/password**: uses session cookie with auto-refresh.
    """

    def __init__(
        self,
        host: str,
        username: str = "",
        password: str = "",
        api_key: str = "",
        verify_ssl: bool = False,
    ):
        self.base_url = f"https://{host}"
        self.username = username
        self.password = password
        self.api_key = api_key
        self.session = requests.Session()
        self.session.verify = verify_ssl
        # Note: API keys don't work with Protect proxy endpoints —
        # don't set X-API-Key header, it interferes with password auth
        self._auth_time: float = 0
        self._auth_lifetime = 43200  # re-auth every 12 hours (was 1 hour)
        self._last_login_attempt: float = 0
        self._login_cooldown = 30   # starts at 30s, doubles after each 403
        self._login_failures = 0    # consecutive 403/failure count

    # ---- auth --------------------------------------------------------

    def login(self) -> bool:
        now = time.time()
        # Prevent hammering the login endpoint (UniFi OS rate-limits → 403)
        if now - self._last_login_attempt < self._login_cooldown:
            log.debug("Login cooldown active (%ds remaining)",
                      int(self._login_cooldown - (now - self._last_login_attempt)))
            return self.is_authenticated()
        self._last_login_attempt = now

        try:
            resp = self.session.post(
                f"{self.base_url}/api/auth/login",
                json={"username": self.username, "password": self.password},
                timeout=10,
            )
            resp.raise_for_status()
            self._auth_time = time.time()
            self._login_failures = 0
            self._login_cooldown = 30  # reset backoff on success
            log.info("Authenticated with UniFi Protect at %s", self.base_url)
            return True
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 403:
                self._login_failures += 1
                # Exponential backoff: 30s, 60s, 120s, 240s … cap at 10 min
                self._login_cooldown = min(30 * (2 ** self._login_failures), 600)
                log.error(
                    "Protect login blocked (403) — backing off %ds (attempt %d): %s",
                    self._login_cooldown, self._login_failures, exc,
                )
            else:
                log.error("Protect login failed: %s", exc)
            return False
        except requests.RequestException as exc:
            log.error("Protect login failed: %s", exc)
            return False

    def is_authenticated(self) -> bool:
        return (time.time() - self._auth_time) < self._auth_lifetime

    def _get(self, path: str, **kwargs) -> requests.Response:
        resp = self.session.get(
            f"{self.base_url}{path}", timeout=15, **kwargs
        )
        if resp.status_code in (401, 403):
            log.info("Session expired (HTTP %d) — re-authenticating", resp.status_code)
            # Force login by resetting cooldown and auth time
            self._auth_time = 0
            self._last_login_attempt = 0
            if self.login():
                resp = self.session.get(
                    f"{self.base_url}{path}", timeout=15, **kwargs
                )
        return resp

    # ---- cameras -----------------------------------------------------

    def get_bootstrap(self) -> dict:
        resp = self._get("/proxy/protect/api/bootstrap")
        resp.raise_for_status()
        return resp.json()

    def list_cameras(self) -> list[dict]:
        bootstrap = self.get_bootstrap()
        return [
            {"id": c["id"], "name": c["name"], "type": c.get("type", "")}
            for c in bootstrap.get("cameras", [])
        ]

    def find_camera(self, name: str) -> Optional[str]:
        """Return the camera ID whose name contains *name* (case-insensitive)."""
        for cam in self.list_cameras():
            if name.lower() in cam["name"].lower():
                log.info("Matched camera: %s (%s)", cam["name"], cam["id"])
                return cam["id"]
        return None

    # ---- events ------------------------------------------------------

    def get_smart_detections(
        self,
        camera_id: str,
        detection_type: str = "person",
        lookback_seconds: int = 30,
    ) -> list[dict]:
        """Return recent smart-detection events for a camera."""
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - lookback_seconds * 1000

        resp = self._get(
            "/proxy/protect/api/events",
            params={
                "start": start_ms,
                "end": now_ms,
                "types": "smartDetectZone",
                "cameras": camera_id,
                "smartDetectTypes": detection_type,
                "orderDirection": "DESC",
                "limit": 10,
            },
        )
        if resp.status_code != 200:
            log.warning("Events request failed: HTTP %d", resp.status_code)
            return []
        return resp.json()

    # ---- media -------------------------------------------------------

    def get_event_thumbnail(
        self, event_id: str, width: int = 640
    ) -> Optional[bytes]:
        """JPEG thumbnail for an event (already cropped to the detection area)."""
        resp = self._get(
            f"/proxy/protect/api/events/{event_id}/thumbnail",
            params={"w": width},
        )
        if resp.status_code == 200 and resp.content:
            return resp.content
        log.warning("No thumbnail for event %s (HTTP %d)", event_id, resp.status_code)
        return None

    def get_camera_snapshot(
        self, camera_id: str, width: int = 3840
    ) -> Optional[bytes]:
        """Live JPEG snapshot from a camera (API — typically 640x360)."""
        resp = self._get(
            f"/proxy/protect/api/cameras/{camera_id}/snapshot",
            params={"w": width},
        )
        if resp.status_code == 200 and resp.content:
            return resp.content
        return None

    def get_rtsp_frame(self, camera_id: str) -> Optional[bytes]:
        """Grab a single high-res frame from the camera's RTSP stream.

        Uses OpenCV to connect to RTSP channel 0 (4K) and capture one
        frame. Falls back to the API snapshot on failure.
        """
        try:
            import cv2

            bootstrap = self.get_bootstrap()
            for cam in bootstrap.get("cameras", []):
                if cam["id"] == camera_id:
                    channels = cam.get("channels", [])
                    # Prefer channel 0 (highest res)
                    for ch in channels:
                        if ch.get("id") == 0 and ch.get("isRtspEnabled"):
                            alias = ch["rtspAlias"]
                            break
                    else:
                        # Fall back to any enabled channel
                        for ch in channels:
                            if ch.get("isRtspEnabled"):
                                alias = ch["rtspAlias"]
                                break
                        else:
                            log.warning("No RTSP channel enabled")
                            return None
                    break
            else:
                return None

            host = self.base_url.replace("https://", "").replace("http://", "")
            rtsp_url = f"rtsps://{host}:7441/{alias}"
            log.info("RTSP frame grab: %s", rtsp_url)

            # Open stream, grab one frame, close immediately
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)

            if not cap.isOpened():
                log.warning("RTSP: could not open stream")
                cap.release()
                return None

            ret, frame = cap.read()
            cap.release()

            if not ret or frame is None:
                log.warning("RTSP: failed to read frame")
                return None

            # Encode as JPEG
            _, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            data = jpg.tobytes()
            h, w = frame.shape[:2]
            log.info("RTSP frame: %dx%d (%d bytes)", w, h, len(data))
            return data

        except Exception as exc:
            log.warning("RTSP frame grab failed: %s", exc)
            return None
