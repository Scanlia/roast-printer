"""Configuration from environment variables."""

import os
from dataclasses import dataclass


@dataclass
class Config:
    # UniFi Protect
    protect_host: str
    protect_username: str
    protect_password: str
    protect_api_key: str
    protect_verify_ssl: bool
    camera_name: str

    # Gemini
    gemini_api_key: str
    gemini_model: str

    # ESP32 / Windows bridge printer
    esp32_host: str
    esp32_port: int

    # Android Termux printer bridge (optional)
    android_host: str
    android_port: int
    android_enabled: bool

    # Receipt
    receipt_width_px: int

    # Timing
    poll_interval_seconds: int
    cooldown_seconds: int

    # Audio / conversation roasting
    audio_enabled: bool
    audio_cooldown_seconds: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            protect_host=os.environ.get("PROTECT_HOST", "192.168.0.1"),
            protect_username=os.environ.get("PROTECT_USERNAME", ""),
            protect_password=os.environ.get("PROTECT_PASSWORD", ""),
            protect_api_key=os.environ.get("PROTECT_API_KEY", ""),
            protect_verify_ssl=os.environ.get("PROTECT_VERIFY_SSL", "false").lower() == "true",
            camera_name=os.environ.get("CAMERA_NAME", "G6 Bullet"),
            gemini_api_key=os.environ["GEMINI_API_KEY"],
            gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
            esp32_host=os.environ.get("ESP32_HOST", "192.168.0.100"),
            esp32_port=int(os.environ.get("ESP32_PORT", "9100")),
            android_host=os.environ.get("ANDROID_HOST", ""),
            android_port=int(os.environ.get("ANDROID_PORT", "9100")),
            android_enabled=os.environ.get("ANDROID_ENABLED", "false").lower() == "true",
            receipt_width_px=int(os.environ.get("RECEIPT_WIDTH_PX", "576")),
            poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "5")),
            cooldown_seconds=int(os.environ.get("COOLDOWN_SECONDS", "120")),
            audio_enabled=os.environ.get("AUDIO_ENABLED", "true").lower() == "true",
            audio_cooldown_seconds=int(os.environ.get("AUDIO_COOLDOWN_SECONDS", "90")),
        )
