FROM python:3.12-slim

WORKDIR /app

# OS deps for Pillow, OpenCV-headless, and RTSP (ffmpeg)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libjpeg62-turbo-dev zlib1g-dev libglib2.0-0 \
        libxcb1 libsm6 libxext6 libxrender1 \
        libgl1 libglx-mesa0 \
        ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# CPU-only PyTorch (keeps image ~150 MB instead of 2 GB for CUDA)
RUN pip install --no-cache-dir \
    torch torchvision --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download YOLOv8n weights so first startup is instant
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

COPY app/ .
COPY android/ android/

CMD ["python", "-u", "main.py"]
