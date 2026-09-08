# Vocalyze — one-container deployment (works on Hugging Face Spaces,
# Render, Fly.io, Railway, any host that runs a Dockerfile).
FROM python:3.11-slim

# ffmpeg is required for audio decoding
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# HF Spaces requires the service on port 7860. Locally it defaults to 8000.
ENV PORT=7860 \
    HOST=0.0.0.0 \
    DATA_DIR=/data/vocalyze \
    PYTHONUNBUFFERED=1

# The encryption key is generated on first boot if none was set.
# For a real deployment, inject ENCRYPTION_KEY as a secret.
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 7860

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
