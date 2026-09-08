"""Audio intake: probe, validate, normalise.

Everything downstream assumes 16 kHz mono PCM — the same preprocessing the
dataset component applied to AMI and ICSI. Normalising here rather than in
each model means Whisper and pyannote see byte-identical audio, so their
timelines line up and the aligner is not fighting two different resamplers.

ffmpeg is invoked with an argument list, never a shell string: filenames come
from uploads and must never reach a shell.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..core.logging import get_logger

log = get_logger("vocalyze.audio")


class AudioError(RuntimeError):
    pass


@dataclass
class AudioInfo:
    duration: float
    sample_rate: int
    channels: int
    codec: str
    format: str


def _run(command: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise AudioError(
            f"{command[0]} was not found. Install ffmpeg (apt install ffmpeg / brew install ffmpeg) "
            "or set FFMPEG_BIN to its path."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffmpeg timed out while reading the file.") from exc


def probe(path: Path, ffmpeg_bin: str = "ffmpeg") -> AudioInfo:
    """Read stream metadata. Doubles as the 'is this really audio' check."""
    ffprobe = "ffprobe" if ffmpeg_bin == "ffmpeg" else ffmpeg_bin.replace("ffmpeg", "ffprobe")
    result = _run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=120,
    )
    if result.returncode != 0:
        raise AudioError("That file could not be decoded as audio.")
    try:
        payload = json.loads(result.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise AudioError("Audio metadata could not be read.") from exc

    streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise AudioError("The file contains no audio track.")
    stream = streams[0]
    container = payload.get("format", {})
    duration = float(container.get("duration") or stream.get("duration") or 0.0)
    return AudioInfo(
        duration=duration,
        sample_rate=int(stream.get("sample_rate") or 0),
        channels=int(stream.get("channels") or 0),
        codec=str(stream.get("codec_name") or "unknown"),
        format=str(container.get("format_name") or "unknown"),
    )


def normalise(
    source: Path,
    target: Path,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    ffmpeg_bin: str = "ffmpeg",
) -> Path:
    """Decode anything ffmpeg understands into 16 kHz mono 16-bit WAV."""
    target.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            ffmpeg_bin, "-nostdin", "-y",
            "-i", str(source),
            "-vn",                       # drop video; meeting recordings are often .mp4
            "-ac", str(channels),
            "-ar", str(sample_rate),
            "-acodec", "pcm_s16le",
            "-f", "wav",
            str(target),
        ]
    )
    if result.returncode != 0 or not target.exists():
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown ffmpeg error"]
        raise AudioError(f"Audio could not be converted: {detail[0]}")
    log.info("normalised %s -> %d Hz mono (%.1f KB)", source.name, sample_rate, target.stat().st_size / 1024)
    return target


def extension_of(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".")
