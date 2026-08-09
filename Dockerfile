FROM python:3.12-slim

# System dependencies required by aiortc / av (FFmpeg)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libavdevice-dev \
        libavfilter-dev \
        libavformat-dev \
        libavcodec-dev \
        libavutil-dev \
        libswscale-dev \
        libswresample-dev \
        libssl-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The server communicates over stdio (MCP protocol); no port needs to be exposed.
CMD ["python", "server.py"]
