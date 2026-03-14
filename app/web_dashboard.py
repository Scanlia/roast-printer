"""Web dashboard — live log viewer, latest roast, reprint, audio listener.

Runs a threaded HTTP server on port 8899 alongside the main event loop.
"""

import json
import logging
import threading
from collections import deque
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Optional

log = logging.getLogger(__name__)

# Ring buffer of recent log entries
_log_entries: deque[dict] = deque(maxlen=500)
_latest_roast: dict = {}
_latest_convo_roast: dict = {}
_latest_payload: Optional[dict] = None
_printer_client = None
_audio_listener = None
_cooldown_seconds: int = 120
_silence_threshold: int = 100
_lock = threading.Lock()


# ---- Custom log handler that captures into the ring buffer ----

class WebLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "name": record.name,
            "msg": self.format(record),
        }
        with _lock:
            _log_entries.append(entry)


def set_latest_roast(roast_text: str, image_b64: Optional[str] = None) -> None:
    with _lock:
        _latest_roast.update({
            "text": roast_text,
            "image": image_b64,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })


def set_latest_payload(payload: dict) -> None:
    """Store the last receipt payload for reprinting."""
    global _latest_payload
    with _lock:
        _latest_payload = payload


def set_printer_client(client) -> None:
    """Give the dashboard a reference to the printer client for reprints."""
    global _printer_client
    _printer_client = client


def set_audio_listener(listener) -> None:
    """Give the dashboard a reference to the audio listener."""
    global _audio_listener
    _audio_listener = listener


def set_latest_convo_roast(roast_text: str) -> None:
    """Store the latest conversation roast for dashboard display."""
    with _lock:
        _latest_convo_roast.update({
            "text": roast_text,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })


def set_cooldown(seconds: int) -> None:
    global _cooldown_seconds
    with _lock:
        _cooldown_seconds = seconds


def get_cooldown() -> int:
    with _lock:
        return _cooldown_seconds


def set_silence_threshold(threshold: int) -> None:
    global _silence_threshold
    with _lock:
        _silence_threshold = max(0, threshold)


def get_silence_threshold() -> int:
    with _lock:
        return _silence_threshold


def _do_reprint() -> dict:
    """Resend the last receipt payload to the printer."""
    with _lock:
        payload = _latest_payload
        client = _printer_client
    if not payload:
        return {"ok": False, "error": "No receipt to reprint"}
    if not client:
        return {"ok": False, "error": "Printer not configured"}
    ok = client.print_receipt(payload)
    return {"ok": ok}


def _do_test_print(method: str) -> dict:
    """Print the last receipt using a specific ESC/POS rendering method."""
    from printer_client import ESCPOS_METHODS, _render_escpos, _tcp_send
    with _lock:
        payload = _latest_payload
        client = _printer_client
    if not payload:
        return {"ok": False, "error": "No cached receipt — walk past the camera first"}
    if not client:
        return {"ok": False, "error": "Printer not configured"}
    if method not in ESCPOS_METHODS:
        return {"ok": False, "error": f"Unknown method: {method}",
                "available": list(ESCPOS_METHODS.keys())}

    log.info("Test print method=%s to %s:%d", method,
             client.android_host, client.android_port)
    escpos = _render_escpos(payload, client.paper_width_dots, method=method)
    if not escpos:
        return {"ok": False, "error": "Render failed"}
    ok = _tcp_send(client.android_host, client.android_port, escpos, 30,
                   f"Test-{method}")
    return {"ok": ok, "method": method, "bytes": len(escpos)}


# ---- HTTP handler ----

class DashboardHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress access logs

    def do_GET(self):
        if self.path == "/api/logs":
            self._json_response(list(_log_entries))
        elif self.path == "/api/latest":
            self._json_response(_latest_roast)
        elif self.path == "/api/reprint":
            result = _do_reprint()
            self._json_response(result)
        elif self.path == "/api/config":
            self._json_response({"cooldown": get_cooldown(), "silence_threshold": get_silence_threshold()})
        elif self.path.startswith("/api/test_print/"):
            method = self.path.split("/")[-1]
            result = _do_test_print(method)
            self._json_response(result)
        elif self.path == "/api/test_methods":
            from printer_client import ESCPOS_METHODS
            self._json_response({"methods": list(ESCPOS_METHODS.keys())})
        elif self.path == "/android/print_bridge.py":
            self._serve_file("/app/android/print_bridge.py", "text/plain")
        elif self.path == "/android/setup.sh":
            self._serve_file("/app/android/setup.sh", "text/plain")
        elif self.path == "/android/audio_capture.py":
            self._serve_file("/app/android/audio_capture.py", "text/plain")
        elif self.path == "/api/audio/status":
            self._handle_audio_status()
        elif self.path == "/api/audio/transcript":
            self._handle_audio_transcript()
        elif self.path == "/api/audio/config":
            self._json_response({"silence_threshold": get_silence_threshold()})
        elif self.path == "/api/convo_latest":
            self._json_response(_latest_convo_roast)
        else:
            self._serve_dashboard()

    def do_POST(self):
        if self.path == "/api/cooldown":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            try:
                secs = max(0, int(body["seconds"]))
                set_cooldown(secs)
                log.info("Cooldown updated to %ds via dashboard", secs)
                self._json_response({"ok": True, "cooldown": secs})
            except (KeyError, ValueError) as e:
                self._json_response({"ok": False, "error": str(e)})
        elif self.path == "/api/audio":
            self._handle_audio_upload()
        elif self.path == "/api/audio/toggle":
            self._handle_audio_toggle()
        elif self.path == "/api/audio/silence_threshold":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            try:
                val = max(0, int(body["threshold"]))
                set_silence_threshold(val)
                log.info("Silence threshold updated to %d via dashboard", val)
                self._json_response({"ok": True, "threshold": val})
            except (KeyError, ValueError) as e:
                self._json_response({"ok": False, "error": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path, content_type="application/octet-stream"):
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _serve_dashboard(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(DASHBOARD_HTML.encode())

    def _handle_audio_upload(self):
        """Receive an audio chunk from the tablet and process it."""
        if not _audio_listener:
            self._json_response({"ok": False, "error": "Audio listener not configured"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0 or content_length > 10_000_000:  # 10 MB max
            self._json_response({"ok": False, "error": "Invalid content length"})
            return

        content_type = self.headers.get("Content-Type", "audio/wav")
        # Map common types
        mime_map = {
            "audio/wav": "audio/wav",
            "audio/wave": "audio/wav",
            "audio/x-wav": "audio/wav",
            "audio/webm": "audio/webm",
            "audio/ogg": "audio/ogg",
            "audio/mp4": "audio/mp4",
            "application/octet-stream": "audio/wav",  # default fallback
        }
        mime_type = mime_map.get(content_type.split(";")[0].strip(), "audio/wav")

        audio_bytes = self.rfile.read(content_length)
        result = _audio_listener.process_audio_chunk(audio_bytes, mime_type)
        self._json_response(result)

    def _handle_audio_toggle(self):
        """Enable/disable audio listening."""
        if not _audio_listener:
            self._json_response({"ok": False, "error": "Audio listener not configured"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        enabled = body.get("enabled", not _audio_listener.enabled)
        _audio_listener.enabled = enabled
        self._json_response({"ok": True, "enabled": _audio_listener.enabled})

    def _handle_audio_status(self):
        """Return audio listener status and stats."""
        if not _audio_listener:
            self._json_response({"enabled": False, "configured": False})
            return

        stats = _audio_listener.stats
        self._json_response({
            "configured": True,
            "enabled": _audio_listener.enabled,
            **stats,
        })

    def _handle_audio_transcript(self):
        """Return the current conversation transcript."""
        if not _audio_listener:
            self._json_response({"transcript": ""})
            return

        self._json_response({
            "transcript": _audio_listener.roaster.get_transcript(),
        })


DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Roast Printer Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #111; color: #eee; font-family: 'Courier New', monospace; }
  .container { max-width: 1400px; margin: 0 auto; padding: 1rem; }
  h1 { text-align: center; font-size: 1.8rem; padding: 1rem 0;
       border-bottom: 2px solid #ff4444; margin-bottom: 1rem; }
  h1 span { color: #ff4444; }

  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
  .log-full { margin-top: 1rem; }

  .card { background: #1a1a1a; border: 1px solid #333; border-radius: 8px;
          padding: 1rem; overflow: hidden; }
  .card h2 { font-size: 1rem; color: #ff4444; margin-bottom: .5rem;
             border-bottom: 1px solid #333; padding-bottom: .3rem; }

  #roast-box { text-align: center; }
  #roast-text { font-size: 1.1rem; line-height: 1.5; padding: 1rem;
                font-style: italic; color: #ffcc00; min-height: 4rem; }
  #roast-time { color: #666; font-size: .8rem; }
  #roast-img { max-width: 100%; width: 100%; border: 1px solid #333; margin-top: .5rem;
               display: none; border-radius: 4px; image-rendering: auto; }

  .btn { padding: .5rem 1.2rem; font-size: .9rem;
         background: #ff4444; color: #fff; border: none; border-radius: 4px;
         cursor: pointer; font-family: inherit; font-weight: bold; }
  .btn:hover { background: #ff6666; }
  .btn:disabled { background: #555; cursor: not-allowed; }
  .btn-green { background: #4caf50; }
  .btn-green:hover { background: #66bb6a; }

  #reprint-btn { margin-top: .8rem; }
  #reprint-status { color: #666; font-size: .75rem; margin-top: .3rem; }

  #convo-text { font-size: 1.05rem; line-height: 1.5; padding: 1rem;
                font-style: italic; color: #66ccff; min-height: 3rem; }
  #convo-time { color: #666; font-size: .8rem; }
  #transcript-box { max-height: 200px; overflow-y: auto; font-size: .75rem;
                    color: #aaa; background: #151515; border-radius: 4px;
                    padding: .5rem; margin-top: .5rem; white-space: pre-wrap; }
  .audio-stats { font-size: .75rem; color: #666; margin-top: .5rem; }
  .audio-stats span { color: #aaa; }

  #log-box { max-height: 70vh; overflow-y: auto; font-size: .75rem;
             line-height: 1.4; }
  .log-line { padding: 2px 0; border-bottom: 1px solid #1f1f1f; white-space: pre-wrap;
              word-break: break-all; }
  .log-line .ts { color: #666; }
  .log-line .lvl-INFO { color: #4caf50; }
  .log-line .lvl-WARNING { color: #ff9800; }
  .log-line .lvl-ERROR { color: #f44336; }
  .log-line .lvl-DEBUG { color: #666; }

  .status-bar { text-align: center; padding: .5rem; color: #666; font-size: .7rem; }
</style>
</head>
<body>
<div class="container">
  <h1>&#x1F525; <span>ROAST PRINTER</span> &#x1F525;</h1>

  <div class="grid">
    <div class="card" id="roast-box">
      <h2>&#x1F4F7; Latest Outfit Roast</h2>
      <div id="roast-text">Waiting for first victim...</div>
      <div id="roast-time"></div>
      <img id="roast-img" alt="victim">
      <br>
      <button class="btn" id="reprint-btn" onclick="doReprint()">&#x1F5A8; Reprint</button>
      <div id="reprint-status"></div>
    </div>

    <div class="card" id="convo-box" style="text-align:center">
      <h2>&#x1F3A4; Conversation Eavesdropper</h2>
      <div id="audio-status" style="margin-bottom:.5rem">
        <span id="audio-indicator" style="font-size:.9rem">&#x23F8; Checking...</span>
        <button class="btn" id="audio-toggle-btn" onclick="toggleAudio()" style="margin-left:.5rem;font-size:.8rem">Toggle</button>
      </div>
      <div id="convo-text">Listening for something roast-worthy...</div>
      <div id="convo-time"></div>
      <div id="transcript-box" style="text-align:left">(transcript will appear here)</div>
      <div class="audio-stats">
        Chunks: <span id="audio-chunks">0</span> |
        Transcribed: <span id="audio-transcribed">0</span> |
        Roasts: <span id="audio-roasts">0</span> |
        Last: <span id="audio-last-time">-</span>
      </div>
    </div>
  </div>

  <div class="grid" style="margin-top:1rem">
    <div class="card">
      <h2>Settings</h2>
      <div style="padding:.5rem 0">
        <label style="color:#aaa;font-size:.85rem;display:block;margin-bottom:.4rem">Cooldown between prints (seconds)</label>
        <div style="display:flex;gap:.5rem;align-items:center">
          <input id="cooldown-input" type="number" min="0" step="10"
            style="width:90px;padding:.4rem .6rem;background:#222;border:1px solid #444;
                   color:#eee;border-radius:4px;font-family:inherit;font-size:1rem">
          <button class="btn" onclick="saveCooldown()" style="font-size:.85rem">Save</button>
          <span id="cooldown-status" style="color:#666;font-size:.8rem"></span>
        </div>
      </div>
      <div style="padding:.5rem 0">
        <label style="color:#aaa;font-size:.85rem;display:block;margin-bottom:.4rem">Audio silence threshold (RMS &mdash; lower = more sensitive)</label>
        <div style="display:flex;gap:.5rem;align-items:center">
          <input id="silence-input" type="number" min="0" step="25"
            style="width:90px;padding:.4rem .6rem;background:#222;border:1px solid #444;
                   color:#eee;border-radius:4px;font-family:inherit;font-size:1rem">
          <button class="btn" onclick="saveSilenceThreshold()" style="font-size:.85rem">Save</button>
          <span id="silence-status" style="color:#666;font-size:.8rem"></span>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>&#x1F4F1; Android Tablet Setup</h2>
      <div style="font-size:.8rem;color:#aaa;line-height:1.6;padding:.5rem 0">
        <p>On the Android tablet (Termux):</p>
        <pre style="background:#222;padding:.5rem;border-radius:4px;margin:.3rem 0;overflow-x:auto;color:#eee">
# Print bridge
curl SERVER:8899/android/print_bridge.py -o print_bridge.py
python print_bridge.py

# Audio capture (new terminal)
curl SERVER:8899/android/audio_capture.py -o audio_capture.py
pip install pyaudio
python audio_capture.py SERVER</pre>
      </div>
    </div>
  </div>

  <div class="card log-full">
    <h2>Live Log</h2>
    <div id="log-box"></div>
  </div>

  <div class="status-bar">
    Auto-refreshes every 2s &bull; <span id="conn-status">connecting...</span>
  </div>
</div>

<script>
const logBox = document.getElementById('log-box');
const roastText = document.getElementById('roast-text');
const roastTime = document.getElementById('roast-time');
const roastImg = document.getElementById('roast-img');
const connStatus = document.getElementById('conn-status');
const reprintBtn = document.getElementById('reprint-btn');
const reprintStatus = document.getElementById('reprint-status');
const convoText = document.getElementById('convo-text');
const convoTime = document.getElementById('convo-time');
const transcriptBox = document.getElementById('transcript-box');
const audioIndicator = document.getElementById('audio-indicator');
let lastLogCount = 0;

async function fetchLogs() {
  try {
    const r = await fetch('/api/logs');
    const logs = await r.json();
    connStatus.textContent = 'connected';

    if (logs.length !== lastLogCount) {
      lastLogCount = logs.length;
      logBox.innerHTML = logs.map(e =>
        `<div class="log-line"><span class="ts">${e.ts}</span> ` +
        `<span class="lvl-${e.level}">[${e.level}]</span> ${e.msg}</div>`
      ).join('');
      logBox.scrollTop = logBox.scrollHeight;
    }
  } catch(e) {
    connStatus.textContent = 'disconnected';
  }
}

async function fetchLatest() {
  try {
    const r = await fetch('/api/latest');
    const data = await r.json();
    if (data.text) {
      roastText.textContent = data.text;
      roastTime.textContent = data.time || '';
    }
    if (data.image) {
      roastImg.src = 'data:image/jpeg;base64,' + data.image;
      roastImg.style.display = 'block';
    }
  } catch(e) {}
}

async function fetchConvoLatest() {
  try {
    const r = await fetch('/api/convo_latest');
    const data = await r.json();
    if (data.text) {
      convoText.textContent = data.text;
      convoTime.textContent = data.time || '';
    }
  } catch(e) {}
}

async function fetchAudioStatus() {
  try {
    const r = await fetch('/api/audio/status');
    const data = await r.json();
    if (!data.configured) {
      audioIndicator.textContent = '\\u26A0 Not configured';
      return;
    }
    audioIndicator.textContent = data.enabled ? '\\u25B6 Listening' : '\\u23F8 Paused';
    audioIndicator.style.color = data.enabled ? '#4caf50' : '#ff9800';
    document.getElementById('audio-chunks').textContent = data.chunks_received || 0;
    document.getElementById('audio-transcribed').textContent = data.chunks_transcribed || 0;
    document.getElementById('audio-roasts').textContent = data.roasts_generated || 0;
    document.getElementById('audio-last-time').textContent = data.last_chunk_time || '-';
  } catch(e) {}
}

async function fetchTranscript() {
  try {
    const r = await fetch('/api/audio/transcript');
    const data = await r.json();
    if (data.transcript) {
      transcriptBox.textContent = data.transcript;
      transcriptBox.scrollTop = transcriptBox.scrollHeight;
    }
  } catch(e) {}
}

async function toggleAudio() {
  try {
    const r = await fetch('/api/audio/toggle', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    const data = await r.json();
    fetchAudioStatus();
  } catch(e) {}
}

async function doReprint() {
  reprintBtn.disabled = true;
  reprintStatus.textContent = 'Sending...';
  try {
    const r = await fetch('/api/reprint');
    const data = await r.json();
    reprintStatus.textContent = data.ok ? 'Sent!' : ('Error: ' + (data.error || 'unknown'));
  } catch(e) {
    reprintStatus.textContent = 'Failed: ' + e.message;
  }
  setTimeout(() => { reprintBtn.disabled = false; reprintStatus.textContent = ''; }, 3000);
}

async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    const data = await r.json();
    document.getElementById('cooldown-input').value = data.cooldown ?? 120;
    document.getElementById('silence-input').value = data.silence_threshold ?? 100;
  } catch(e) {}
}

async function saveCooldown() {
  const input = document.getElementById('cooldown-input');
  const status = document.getElementById('cooldown-status');
  const secs = parseInt(input.value, 10);
  if (isNaN(secs) || secs < 0) { status.textContent = 'Invalid value'; return; }
  try {
    const r = await fetch('/api/cooldown', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({seconds: secs}),
    });
    const data = await r.json();
    status.textContent = data.ok ? '\u2713 Saved' : ('Error: ' + data.error);
    status.style.color = data.ok ? '#4caf50' : '#f44336';
  } catch(e) {
    status.textContent = 'Failed';
  }
  setTimeout(() => { status.textContent = ''; }, 3000);
}

async function saveSilenceThreshold() {
  const input = document.getElementById('silence-input');
  const status = document.getElementById('silence-status');
  const val = parseInt(input.value, 10);
  if (isNaN(val) || val < 0) { status.textContent = 'Invalid value'; return; }
  try {
    const r = await fetch('/api/audio/silence_threshold', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({threshold: val}),
    });
    const data = await r.json();
    status.textContent = data.ok ? '\u2713 Saved' : ('Error: ' + data.error);
    status.style.color = data.ok ? '#4caf50' : '#f44336';
  } catch(e) {
    status.textContent = 'Failed';
  }
  setTimeout(() => { status.textContent = ''; }, 3000);
}

setInterval(fetchLogs, 2000);
setInterval(fetchLatest, 3000);
setInterval(fetchConvoLatest, 3000);
setInterval(fetchAudioStatus, 3000);
setInterval(fetchTranscript, 5000);
fetchLogs();
fetchLatest();
fetchConvoLatest();
fetchAudioStatus();
loadConfig();
</script>
</body>
</html>
"""


def start_dashboard(port: int = 8899) -> None:
    """Start the dashboard HTTP server in a daemon thread."""
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Dashboard running on http://0.0.0.0:%d", port)
