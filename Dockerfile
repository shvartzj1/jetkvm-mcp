# syntax=docker/dockerfile:1

# ---- Builder stage -----------------------------------------------------
# Pinned to the Debian codename so the runtime stage's FFmpeg SONAME package
# names stay valid; a bare python:3.12-slim would silently roll to a newer
# Debian release and 404 on libav*NN.
FROM python:3.12-slim-trixie AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libavdevice-dev \
        libavfilter-dev \
        libavformat-dev \
        libavcodec-dev \
        libavutil-dev \
        libswscale-dev \
        libswresample-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---- Runtime stage -----------------------------------------------------
# Only the shared libraries, none of the -dev headers or the compiler.
FROM python:3.12-slim-trixie AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        libavdevice61 \
        libavfilter10 \
        libavformat61 \
        libavcodec61 \
        libavutil59 \
        libswscale8 \
        libswresample5 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY --chown=appuser:appuser server.py smoke_test.py ./
COPY --chown=appuser:appuser jetkvm/ ./jetkvm/

USER appuser

# The server communicates over stdio (MCP protocol); no port needs to be exposed.
# `python smoke_test.py` is kept in the image as a connectivity check:
#   docker run --rm -e JETKVM_URL=... jetkvm-mcp python smoke_test.py
CMD ["python", "server.py"]
