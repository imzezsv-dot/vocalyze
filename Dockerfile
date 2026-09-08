# Vocalyze — one container, real models.
#
# This is the deployment where Whisper and pyannote actually run: Hugging Face
# Spaces, Render, Fly, Railway, or any host that takes a Dockerfile.
#
#   docker build -t vocalyze .
#   docker run -p 7860:7860 -e ENCRYPTION_KEY=$(python -m app.core.crypto) vocalyze
#
# Build without the model weights' dependencies (scripted backends only, a
# much smaller and faster image):
#
#   docker build --build-arg WITH_MODELS=0 -t vocalyze .

FROM python:3.11-slim

# ffmpeg decodes the uploads; tini reaps the worker threads' children.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs the container as uid 1000, and a process that
# cannot write its own cache cannot download a model. Everything this user
# needs to write lives under its home directory.
RUN useradd -m -u 1000 vocalyze
WORKDIR /app

COPY requirements.txt requirements-models.txt ./

ARG WITH_MODELS=1
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$WITH_MODELS" = "1" ]; then pip install --no-cache-dir -r requirements-models.txt; fi

COPY . .
RUN chown -R vocalyze:vocalyze /app

USER vocalyze

ENV HOME=/home/vocalyze \
    PORT=7860 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Not /data: that exists on a Space only with paid persistent storage,
    # and the service must boot without it. Mount a volume here to keep jobs
    # across restarts; without one they live for the container's lifetime,
    # which is longer than any retention window this service defaults to.
    DATA_DIR=/home/vocalyze/data \
    # Model caches. Without these, downloads land in an unwritable ~/.cache
    # and the first real transcription fails instead of the first upload.
    HF_HOME=/home/vocalyze/.cache/huggingface \
    TORCH_HOME=/home/vocalyze/.cache/torch \
    XDG_CACHE_HOME=/home/vocalyze/.cache

RUN mkdir -p "$DATA_DIR" "$XDG_CACHE_HOME"

# ---------------------------------------------------------------------------
# Real models by default.
#
# This image exists to be the deployment where the models actually run, and a
# visitor who uploads their own recording and is handed the scripted sample
# meeting has been shown nothing. The scripted backends stay one environment
# variable away (ASR_BACKEND=mock) for a fast-booting preview.
#
# `base` rather than `small`: a free Space is 2 CPU cores. int8 `base` runs
# comfortably faster than real time there, `small` runs at roughly real time,
# and `large-v3` is far slower than real time and belongs on a GPU tier.
# ---------------------------------------------------------------------------
ARG WHISPER_MODEL=base
ENV WHISPER_MODEL=${WHISPER_MODEL} \
    WHISPER_DEVICE=cpu \
    ASR_BACKEND=whisper \
    SUMMARIZER_BACKEND=extractive \
    # pyannote needs a token and per-account licence acceptance, so it cannot
    # be the default. Set DIARIZATION_BACKEND=pyannote and HUGGINGFACE_TOKEN
    # to switch it on; until then speaker separation is approximate and the
    # interface's badge says which stage is scripted.
    DIARIZATION_BACKEND=mock \
    # Sized for 2 shared cores. The point of a limit here is that a visitor
    # who uploads a two-hour recording gets a clear refusal instead of a
    # progress bar that never moves.
    MAX_DURATION_MINUTES=15 \
    MAX_UPLOAD_MB=50

# Bake the weights into the image.
#
# Downloading them on first use means the first visitor waits several minutes
# on what looks like a hang, decides the demo is broken, and leaves. Doing it
# at build time costs image size once and makes every request fast. Runs as
# the `vocalyze` user so the files land in the cache that user can read.
RUN if [ "$WITH_MODELS" = "1" ]; then \
      python -c "from faster_whisper import WhisperModel; \
WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')" \
      && echo "[build] Whisper ${WHISPER_MODEL} baked into the image"; \
    fi

EXPOSE 7860

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
