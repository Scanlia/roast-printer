# Roast Printer 🔥

A surveillance camera roast machine. Detects people via UniFi Protect, grabs a photo, sends it to Google Gemini for a savage outfit roast, and prints it on a thermal receipt printer.

Also listens to conversations via a tablet microphone — when Gemini hears something funny or roast-worthy, it prints a commentary receipt.

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
│ UniFi Protect │────▶│  roast-printer   │────▶│ Android/ESP32│
│   Camera      │     │  (Docker)        │     │ Print Bridge │
└──────────────┘     │                  │     └──────┬───────┘
                     │  - Person detect │            │
┌──────────────┐     │  - Gemini roast  │     ┌──────▼───────┐
│ Tablet Mic   │────▶│  - Audio STT     │     │ Epson TM-T88V│
│ (Android)    │     │  - Convo roast   │     │  Receipt     │
└──────────────┘     └──────────────────┘     └──────────────┘
```

### Features

- **Outfit Roast**: Detects people on camera, crops & analyses their outfit, prints a roast receipt
- **Conversation Roast**: Streams audio from a tablet mic, transcribes speech, and when something funny is said, Gemini prints a commentary receipt
- **Web Dashboard**: Live log viewer, latest roast display, reprint button, settings (port 8899)
- **Multiple print bridges**: ESP32 serial, Windows GDI, Android Termux USB

## Setup

1. Copy `.env.example` to `.env` and fill in your credentials
2. Run: `docker compose up -d --build`
3. Dashboard: `http://<host>:8899`

### Android Print Bridge (Termux)

On the Android tablet:
```bash
# Download the scripts from the dashboard
curl http://<host>:8899/android/setup.sh | bash
python print_bridge.py
```

### Audio Listener (Tablet)

The Android tablet also runs the audio capture client that streams microphone audio to the server for conversation roasting. See the dashboard for setup instructions.

## Environment Variables

See [.env.example](.env.example) for all configuration options.

## Hardware

- UniFi Protect camera (any model with smart detection)
- Epson TM-T88V thermal receipt printer (80mm)
- Android tablet (for Termux print bridge + microphone)
- Optional: ESP32-C6 + MAX3232 for serial bridge
