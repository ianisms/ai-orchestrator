FROM python:3.11-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    IMAGE_OPENWAKEWORD_HOME=/opt/openwakeword_cache \
    OPENWAKEWORD_HOME=/opt/openwakeword_cache \
    PERSIST_OPENWAKEWORD_HOME=/ai/speaker/.cache/openwakeword

# ---- System deps ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    pulseaudio-utils \
    alsa-utils \
    libasound2-dev \
    sox \
    ca-certificates \
    curl \
    procps \
 && rm -rf /var/lib/apt/lists/*

# ---- Python deps ----
RUN python -m pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir \
    grpcio==1.76.0 \
    grpcio-tools==1.76.0 \
    protobuf==6.31.1 \
    pyalsaaudio==0.11.0 \
    numpy \
    openwakeword \
    pyyaml

# ---- Healthcheck ----
COPY docker/healthcheck.py /opt/app/healthcheck.py
# ---- Healthcheck ----
# Requires:
#   - wake_loop_alive_ts updated within 5s
#   - grpc_connected == true
#
HEALTHCHECK --interval=5s --timeout=2s --start-period=20s --retries=3 \
  CMD python /opt/app/healthcheck.py

# ---- OpenWakeWord bootstrap ----
WORKDIR /opt/app
COPY docker/oww_bootstrap.py /opt/app/oww_bootstrap.py
RUN chmod +x /opt/app/oww_bootstrap.py
RUN python -u /opt/app/oww_bootstrap.py

# ---- Protos ----
COPY docker/gen_protos.sh /opt/app/gen_protos.sh
RUN chmod +x /opt/app/gen_protos.sh

# Copy proto sources into image for build-time generation
COPY modules/speaker/protos /ai/speaker/modules/speaker/protos
COPY config/config.yaml /ai/speaker/config/config.yaml
RUN mkdir -p /ai/speaker/scripts
RUN /opt/app/gen_protos.sh


#---- entry ----
WORKDIR /ai/speaker
CMD ["python", "-u", "-m", "modules.speaker.orchestrator"]
