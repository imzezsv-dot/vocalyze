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
    # `small` rather than the repo default `large-v3`: on a free CPU tier
    # large-v3 transcribes roughly slower than real time, so a 10-minute
    # meeting outlives any reviewer's patience. Set WHISPER_MODEL=large-v3
    # on a GPU tier.
    WHISPER_MODEL=small \
    # Model caches. Without these, downloads land in an unwritable ~/.cache
    # and the first real transcription fails instead of the first upload.
    HF_HOME=/home/vocalyze/.cache/huggingface \
    TORCH_HOME=/home/vocalyze/.cache/torch \
    XDG_CACHE_HOME=/home/vocalyze/.cache

RUN mkdir -p "$DATA_DIR" "$XDG_CACHE_HOME"

EXPOSE 7860

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
