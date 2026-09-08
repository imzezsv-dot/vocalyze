"""Publish Vocalyze as a Hugging Face Space, from CI.

Same result as `scripts/deploy_space.sh`, without needing a terminal: the
workflow that calls this is a button in the Actions tab.

Everything here is idempotent — running it again updates the Space in place
rather than creating a second one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parents[2]

# Files that must never reach a public Space: local state, caches, and any
# .env a contributor happened to have sitting in their checkout.
EXCLUDE = [
    ".git/*",
    ".github/*",
    ".venv/*",
    "var/*",
    "**/__pycache__/*",
    "*.pyc",
    ".env",
    ".env.*",
]
# No negations: `ignore_patterns` has no "except this one" form, so anything
# that must survive an exclusion has to not match it in the first place.


def fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def main() -> None:
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        fail("HF_TOKEN is empty.")

    space_name = os.environ.get("SPACE_NAME", "vocalyze").strip() or "vocalyze"
    whisper_model = os.environ.get("WHISPER_MODEL", "base").strip() or "base"
    private = os.environ.get("PRIVATE", "false").strip().lower() == "true"

    api = HfApi(token=token)

    try:
        username = api.whoami()["name"]
    except Exception as exc:  # noqa: BLE001
        fail(
            "That token was rejected by Hugging Face. It must be a WRITE token "
            f"from https://huggingface.co/settings/tokens — {exc}"
        )

    repo_id = f"{username}/{space_name}"
    print(f"Publishing to {repo_id}")

    # --------------------------------------------------------------- create
    api.create_repo(
        repo_id=repo_id,
        repo_type="space",
        space_sdk="docker",
        private=private,
        exist_ok=True,
    )

    # ------------------------------------------------- is pyannote usable?
    # Its weights are gated per account. Selecting it without acceptance makes
    # every first diarization fail with a raw 401, so check before choosing.
    diarization = "pyannote"
    for model in ("pyannote/speaker-diarization-3.1", "pyannote/segmentation-3.0"):
        try:
            api.model_info(model, token=token)
        except Exception:  # noqa: BLE001
            diarization = "mock"
            print(f"::warning::{model} is not accessible to {username} yet.")
            break

    if diarization == "mock":
        print(
            "::warning::Speaker separation starts approximate. To switch it on, "
            "accept the terms with THIS account on both "
            "https://hf.co/pyannote/speaker-diarization-3.1 and "
            "https://hf.co/pyannote/segmentation-3.0 , then set "
            "DIARIZATION_BACKEND=pyannote in the Space's settings."
        )
    else:
        print("Both pyannote models are accessible — real speaker separation is on.")

    # ------------------------------------------------------ configuration
    sys.path.insert(0, str(ROOT))          # before the import, not after
    from app.core.crypto import generate_service_key

    secrets = {
        # Without a stable key each restart mints a new one and older jobs
        # become unreadable ciphertext.
        "ENCRYPTION_KEY": generate_service_key(),
        "HUGGINGFACE_TOKEN": token,
    }
    variables = {
        "ASR_BACKEND": "whisper",
        "DIARIZATION_BACKEND": diarization,
        "SUMMARIZER_BACKEND": "extractive",
        "WHISPER_MODEL": whisper_model,
        "WHISPER_DEVICE": "cpu",
        # Sized for the two shared cores a free Space gets.
        "MAX_UPLOAD_MB": "50",
        "MAX_DURATION_MINUTES": "15",
    }

    for key, value in secrets.items():
        api.add_space_secret(repo_id=repo_id, key=key, value=value)
    for key, value in variables.items():
        api.add_space_variable(repo_id=repo_id, key=key, value=value)
    print(f"Set {len(secrets)} secrets and {len(variables)} variables.")

    # ------------------------------------------------------------- upload
    api.upload_folder(
        repo_id=repo_id,
        repo_type="space",
        folder_path=str(ROOT),
        ignore_patterns=EXCLUDE,
        commit_message="Vocalyze — integration and privacy layer",
    )

    # A Space reads its configuration from README.md's YAML front matter and
    # never looks at README_HF.md. Without this the Space does not know it is a
    # Docker build on port 7860, and never starts.
    api.upload_file(
        path_or_fileobj=str(ROOT / "README_HF.md"),
        path_in_repo="README.md",
        repo_id=repo_id,
        repo_type="space",
        commit_message="Space configuration",
    )

    url = f"https://huggingface.co/spaces/{repo_id}"
    print(f"\nPublished: {url}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(
            f"## Your live site\n\n"
            f"# {url}\n\n"
            f"It is **building now** — five to ten minutes the first time, because the "
            f"image bakes the Whisper `{whisper_model}` weights into itself. Watch the "
            f"build log on that page; the site answers as soon as it turns green.\n\n"
            f"| | |\n|---|---|\n"
            f"| Speech recognition | Whisper `{whisper_model}`, int8 on CPU — **your audio, really transcribed** |\n"
            f"| Speaker separation | `{diarization}`"
            + ("" if diarization == "pyannote" else " — approximate, see the warnings above")
            + " |\n"
            f"| Limits | 50 MB, 15 minutes |\n\n"
            f"A free Space sleeps after inactivity and takes about thirty seconds to "
            f"wake. **Open it once before you present.**\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
