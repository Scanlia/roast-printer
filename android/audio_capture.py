#!/usr/bin/env python3
"""
Android Audio Capture — streams microphone audio to the roast-printer server.

Run on the Android tablet via Termux alongside print_bridge.py:
    pkg install python termux-api
    pip install pyaudio
    python audio_capture.py <SERVER_HOST>

Records audio in chunks (default 30 seconds) and POSTs each chunk
as a WAV file to the server's /api/audio endpoint.

The server transcribes the audio, feeds it into the conversation
roaster, and prints a receipt if something funny was said.
"""

import argparse
import io
import struct
import sys
import time
import urllib.request
import urllib.error


def test_pyaudio() -> bool:
    """Try to open the mic briefly to see if pyaudio actually works."""
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        try:
            stream = pa.open(
                format=pyaudio.paInt16, channels=1, rate=16000,
                input=True, frames_per_buffer=1024,
            )
            stream.read(1024, exception_on_overflow=False)
            stream.stop_stream()
            stream.close()
            return True
        except Exception:
            return False
        finally:
            pa.terminate()
    except Exception:
        return False


def record_chunk_pyaudio(duration: int, rate: int = 16000, channels: int = 1) -> bytes:
    """Record a chunk of audio using PyAudio and return WAV bytes."""
    import pyaudio

    CHUNK_FRAMES = 1024
    FORMAT = pyaudio.paInt16

    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(
            format=FORMAT,
            channels=channels,
            rate=rate,
            input=True,
            frames_per_buffer=CHUNK_FRAMES,
        )

        frames = []
        num_chunks = int(rate / CHUNK_FRAMES * duration)
        for _ in range(num_chunks):
            data = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
            frames.append(data)

        stream.stop_stream()
        stream.close()
    finally:
        pa.terminate()

    # Build WAV in memory
    audio_data = b"".join(frames)
    return _make_wav(audio_data, channels, rate, 2)  # 2 bytes per sample (16-bit)


def record_chunk_termux(duration: int, rate: int = 16000) -> bytes:
    """Record using termux-microphone-record as fallback.

    termux-microphone-record runs asynchronously — start it, wait for
    the duration, then stop it and read the file.
    """
    import subprocess
    import tempfile
    import os

    tmp = tempfile.mktemp(suffix=".wav")
    try:
        # Start recording (returns immediately, records in background)
        subprocess.run(
            ["termux-microphone-record", "-f", tmp,
             "-e", "pcm", "-b", "16", "-r", str(rate), "-c", "1"],
            timeout=10,
        )
        # Wait for the requested duration
        time.sleep(duration)
        # Stop recording
        subprocess.run(["termux-microphone-record", "-q"], timeout=5)
        time.sleep(0.5)

        if os.path.exists(tmp):
            with open(tmp, "rb") as f:
                return f.read()
    except Exception as e:
        print(f"  termux-microphone-record error: {e}", file=sys.stderr)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    return b""


def _make_wav(pcm_data: bytes, channels: int, rate: int, sample_width: int) -> bytes:
    """Wrap raw PCM data in a WAV header."""
    data_size = len(pcm_data)
    buf = io.BytesIO()
    # RIFF header
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVE")
    # fmt chunk
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))  # chunk size
    buf.write(struct.pack("<H", 1))   # PCM format
    buf.write(struct.pack("<H", channels))
    buf.write(struct.pack("<I", rate))
    buf.write(struct.pack("<I", rate * channels * sample_width))  # byte rate
    buf.write(struct.pack("<H", channels * sample_width))  # block align
    buf.write(struct.pack("<H", sample_width * 8))  # bits per sample
    # data chunk
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(pcm_data)
    return buf.getvalue()


def send_audio(server_url: str, wav_data: bytes) -> bool:
    """POST audio data to the server."""
    try:
        req = urllib.request.Request(
            server_url,
            data=wav_data,
            headers={"Content-Type": "audio/wav"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            print(f"  Server: {body}")
            return resp.status == 200
    except urllib.error.URLError as e:
        print(f"  Send error: {e}", file=sys.stderr)
        return False


def detect_silence(wav_data: bytes, threshold: int = 500) -> bool:
    """Check if a WAV chunk is mostly silence (skip sending to save bandwidth)."""
    # Skip WAV header (44 bytes) and check RMS of samples
    if len(wav_data) < 100:
        return True
    pcm = wav_data[44:]
    if not pcm:
        return True

    # Calculate RMS of 16-bit samples
    n_samples = len(pcm) // 2
    if n_samples == 0:
        return True

    total = 0
    for i in range(0, min(len(pcm) - 1, 10000), 2):  # sample first 5000 samples
        sample = struct.unpack_from("<h", pcm, i)[0]
        total += sample * sample

    rms = (total / min(n_samples, 5000)) ** 0.5
    return rms < threshold


def main():
    parser = argparse.ArgumentParser(description="Stream mic audio to roast-printer server")
    parser.add_argument("server", help="Server hostname or IP (e.g. 192.168.0.50)")
    parser.add_argument("--port", type=int, default=8899, help="Server port (default: 8899)")
    parser.add_argument("--duration", type=int, default=30, help="Chunk duration in seconds (default: 30)")
    parser.add_argument("--rate", type=int, default=16000, help="Sample rate (default: 16000)")
    parser.add_argument("--method", choices=["auto", "pyaudio", "termux"], default="auto",
                        help="Recording method (default: auto — tries pyaudio, falls back to termux)")
    parser.add_argument("--silence-threshold", type=int, default=500,
                        help="RMS threshold below which audio is considered silence (default: 500)")
    args = parser.parse_args()

    # Auto-detect method
    if args.method == "auto":
        try:
            import pyaudio  # noqa: F401
            print("pyaudio installed — testing mic access...")
            if test_pyaudio():
                args.method = "pyaudio"
                print("Method: pyaudio (mic test passed)")
            else:
                args.method = "termux"
                print("pyaudio mic test FAILED — using termux-microphone-record")
                print("  (Grant mic permission: Termux:API app, Android Settings > Apps > Termux:API > Permissions)")
        except ImportError:
            args.method = "termux"
            print("pyaudio not found — using termux-microphone-record")
            print("  (To use pyaudio: pkg install portaudio && pip install pyaudio)")

    server_url = f"http://{args.server}:{args.port}/api/audio"

    print("=" * 40)
    print("  Audio Capture for Roast Printer")
    print(f"  Server: {server_url}")
    print(f"  Chunk: {args.duration}s @ {args.rate}Hz")
    print(f"  Method: {args.method}")
    print("=" * 40)
    print()

    # Test connection
    try:
        req = urllib.request.Request(
            f"http://{args.server}:{args.port}/api/audio/status",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"Server connected: {resp.read().decode()}")
    except Exception as e:
        print(f"WARNING: Cannot reach server: {e}")
        print("Will keep trying...\n")

    record_fn = record_chunk_pyaudio if args.method == "pyaudio" else record_chunk_termux
    chunk_num = 0

    while True:
        try:
            chunk_num += 1
            print(f"[{chunk_num}] Recording {args.duration}s...")

            try:
                wav_data = record_fn(args.duration, args.rate)
            except Exception as rec_err:
                if record_fn == record_chunk_pyaudio:
                    print(f"  pyaudio error: {rec_err}")
                    print("  Falling back to termux-microphone-record...")
                    record_fn = record_chunk_termux
                    wav_data = record_fn(args.duration, args.rate)
                else:
                    raise

            if not wav_data:
                print(f"  No audio captured, retrying...")
                time.sleep(2)
                continue

            print(f"  Captured {len(wav_data)} bytes")

            # Skip silence
            if detect_silence(wav_data, args.silence_threshold):
                print(f"  Silence detected, skipping")
                continue

            # Send to server
            print(f"  Sending to server...")
            send_audio(server_url, wav_data)

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"  Error: {e}", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    main()
