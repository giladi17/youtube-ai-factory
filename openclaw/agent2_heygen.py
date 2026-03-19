"""
Agent 2: HeyGen Avatar Generator (Hybrid Workflow)
===================================================
1. Load script.json (local file OR S3 for K8s)
2. Submit voiceover_text to HeyGen v2 API
3. Poll until render completes
4. Download avatar.mp4 locally → outputs/<run_id>_avatar.mp4

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  WHERE TO PUT YOUR HEYGEN API KEY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Option A — Local dev (recommended):
      PowerShell:  $env:HEYGEN_API_KEY = "your_key_here"
      Bash:        export HEYGEN_API_KEY="your_key_here"

  Option B — Permanent (Windows only):
      [System.Environment]::SetEnvironmentVariable(
          "HEYGEN_API_KEY", "your_key_here", "User")

  Option C — K8s production:
      Store in AWS Secrets Manager under key "HEYGEN_API_KEY"
      Set env var SECRETS_MANAGER_NAME to your secret name.

  Get your key at: https://app.heygen.com/settings?nav=API
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Usage:
  python agent2_heygen.py --script outputs/factory-test-001_script.json
  python agent2_heygen.py --script outputs/factory-test-001_script.json --run-id my-video-001

Output:
  outputs/<run_id>_avatar.mp4
"""
import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent2-heygen")

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT    = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
OUTPUTS.mkdir(exist_ok=True)

# ── HeyGen config ──────────────────────────────────────────────────────────────
HEYGEN_API_BASE      = "https://api.heygen.com"
HEYGEN_POLL_INTERVAL = int(os.environ.get("HEYGEN_POLL_INTERVAL_SEC", "30"))
HEYGEN_MAX_WAIT      = int(os.environ.get("HEYGEN_MAX_WAIT_SEC", "1800"))  # 30 min

# ── Avatar settings — swap these to change your presenter ─────────────────────
# Find available avatars at: https://app.heygen.com/avatars
HEYGEN_AVATAR_ID = os.environ.get("HEYGEN_AVATAR_ID", "Angela-inblackskirt-20220820")
# Find voice IDs at: https://docs.heygen.com/reference/list-voices
HEYGEN_VOICE_ID  = os.environ.get("HEYGEN_VOICE_ID",  "1bd001e7e50f421d891986aad5158bc8")
# Green-screen background — MUST be #00B140 to match Agent 3 chromakey
HEYGEN_BG_COLOR  = "#00B140"


# ── API key resolution ─────────────────────────────────────────────────────────

def _get_api_key() -> str:
    """
    Priority:
      1. HEYGEN_API_KEY env var  (local dev)
      2. AWS Secrets Manager     (K8s production)
    """
    key = os.environ.get("HEYGEN_API_KEY", "").strip()
    if key:
        return key

    # K8s fallback: pull from Secrets Manager
    secret_name = os.environ.get("SECRETS_MANAGER_NAME", "")
    if secret_name:
        try:
            import boto3
            aws_region = os.environ.get("AWS_REGION", "eu-north-1")
            sm      = boto3.client("secretsmanager", region_name=aws_region)
            secrets = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
            key     = secrets.get("HEYGEN_API_KEY", "")
            if key:
                log.info("HeyGen API key loaded from AWS Secrets Manager")
                return key
        except Exception as exc:
            log.warning(f"Secrets Manager lookup failed: {exc}")

    log.error(
        "HeyGen API key not found.\n"
        "  Set it with:  $env:HEYGEN_API_KEY = 'your_key_here'\n"
        "  Get key at:   https://app.heygen.com/settings?nav=API"
    )
    sys.exit(1)


# ── HeyGen API calls ───────────────────────────────────────────────────────────

def _submit(api_key: str, voiceover_text: str) -> str:
    """POST to HeyGen v2 generate endpoint. Returns video_id."""
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    payload = {
        "video_inputs": [{
            "character": {
                "type":         "avatar",
                "avatar_id":    HEYGEN_AVATAR_ID,
                "avatar_style": "normal",
            },
            "voice": {
                "type":       "text",
                "input_text": voiceover_text[:2000],  # HeyGen v2 limit per segment
                "voice_id":   HEYGEN_VOICE_ID,
                "speed":      1.0,
            },
            "background": {
                "type":  "color",
                "value": HEYGEN_BG_COLOR,
            },
        }],
        "dimension":    {"width": 1920, "height": 1080},
        "aspect_ratio": "16:9",
    }

    log.info(f"Submitting to HeyGen | avatar={HEYGEN_AVATAR_ID} | "
             f"voice={HEYGEN_VOICE_ID} | text_len={len(voiceover_text)} chars")

    resp = requests.post(
        f"{HEYGEN_API_BASE}/v2/video/generate",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        log.error(f"HeyGen submit failed: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()

    video_id = resp.json()["data"]["video_id"]
    log.info(f"HeyGen job submitted | video_id={video_id}")
    return video_id


def _poll(api_key: str, video_id: str) -> str:
    """Poll HeyGen status until complete. Returns the MP4 download URL."""
    headers = {"X-Api-Key": api_key}
    elapsed = 0

    log.info(f"Polling HeyGen | max_wait={HEYGEN_MAX_WAIT}s | interval={HEYGEN_POLL_INTERVAL}s")

    while elapsed < HEYGEN_MAX_WAIT:
        resp = requests.get(
            f"{HEYGEN_API_BASE}/v1/video_status.get",
            params={"video_id": video_id},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data   = resp.json()["data"]
        status = data["status"]

        log.info(f"  status={status} | elapsed={elapsed}s")

        if status == "completed":
            url = data["video_url"]
            log.info(f"Render complete | url={url[:80]}...")
            return url

        if status == "failed":
            raise RuntimeError(f"HeyGen render failed: {data.get('error', 'unknown')}")

        time.sleep(HEYGEN_POLL_INTERVAL)
        elapsed += HEYGEN_POLL_INTERVAL

    raise TimeoutError(f"HeyGen timed out after {HEYGEN_MAX_WAIT}s | video_id={video_id}")


def _download_avatar(video_url: str, dest: Path) -> None:
    """Stream-download the rendered MP4 to a local file."""
    log.info(f"Downloading avatar video → {dest.name} ...")
    with requests.get(video_url, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):  # 1 MiB chunks
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    print(f"\r  {pct}%  ({downloaded // (1024*1024)} / {total // (1024*1024)} MiB)",
                          end="", flush=True)
    print()
    size_mib = dest.stat().st_size / (1024 * 1024)
    log.info(f"Avatar saved → {dest}  ({size_mib:.1f} MiB)")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Agent 2 — HeyGen Avatar Generator")
    parser.add_argument("--script",  required=True,
                        help="Path to script.json (from Agent 1)")
    parser.add_argument("--run-id",  default=None,
                        help="Run ID for output filename (default: inferred from script filename)")
    parser.add_argument("--out",     default=None,
                        help="Override output path for avatar.mp4")
    args = parser.parse_args()

    script_path = Path(args.script)
    if not script_path.exists():
        log.error(f"Script file not found: {script_path}")
        sys.exit(1)

    script = json.loads(script_path.read_text(encoding="utf-8"))

    run_id = args.run_id or script_path.stem.replace("_script", "")
    out    = Path(args.out) if args.out else OUTPUTS / f"{run_id}_avatar.mp4"

    voiceover_text = script.get("voiceover_text", "").strip()
    if not voiceover_text:
        # Build from body segments as fallback
        parts = [seg.get("text", "") for seg in script.get("body", [])]
        voiceover_text = "  ".join(p for p in parts if p)
    if not voiceover_text:
        log.error("No voiceover_text found in script.json")
        sys.exit(1)

    api_key  = _get_api_key()
    video_id = _submit(api_key, voiceover_text)
    url      = _poll(api_key, video_id)
    _download_avatar(url, out)

    log.info("")
    log.info("=" * 50)
    log.info("  AGENT 2 COMPLETE")
    log.info(f"  Avatar : {out}")
    log.info(f"  Next   : python agent3_pre_edit.py --script {script_path} --avatar {out}")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
