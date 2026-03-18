"""
run_factory.py — MVP Faceless Video Pipeline
=============================================
Stages:
  1. Scriptwriter  — GPT-4o fetches RSS + generates viral script JSON
                     (falls back to test_input.json if no API key / quota hit)
  2. TTS           — edge-tts converts voiceover_text to voiceover.mp3
  3. Video Editor  — B-roll timeline + subtitles + auto-duck music over voiceover

All S3 and Redis calls are mocked locally.
Output: outputs/<run_id>.mp4

Prerequisites:
  pip install edge-tts openai feedparser boto3 redis requests
  FFmpeg in PATH

Usage (PowerShell):
  $env:OPENAI_API_KEY = "sk-..."
  python run_factory.py

  # Hebrew voiceover:
  python run_factory.py --voice he-IL-HilaNeural

  # Skip scriptwriter (use existing JSON):
  python run_factory.py --script test_input.json

  # Custom run ID:
  python run_factory.py --run-id my-video-001
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("factory")

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).resolve().parent
OUTPUTS   = ROOT / "outputs"
WORKSPACE = ROOT / "tmp_factory"
OUTPUTS.mkdir(exist_ok=True)
WORKSPACE.mkdir(exist_ok=True)

# ── CLI args ───────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="MVP Faceless Video Factory")
parser.add_argument("--voice",  default="en-US-AriaNeural",
                    help="edge-tts voice (default: en-US-AriaNeural). "
                         "Hebrew example: he-IL-HilaNeural")
parser.add_argument("--script", default=None,
                    help="Skip scriptwriter — use this JSON file path instead")
parser.add_argument("--run-id", default=None,
                    help="Run identifier (default: factory-YYYYMMDD-HHMMSS)")
args = parser.parse_args()

RUN_ID = args.run_id or f"factory-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
log.info(f"{'='*60}")
log.info(f"  YouTube AI Factory  |  run_id={RUN_ID}")
log.info(f"  voice={args.voice}")
log.info(f"{'='*60}")


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 1 — Scriptwriter
# ══════════════════════════════════════════════════════════════════════════════

def _run_scriptwriter() -> dict:
    """
    Runs Agent 1 with mocked S3/Secrets Manager/Redis.
    Returns the generated script dict.
    Falls back to test_input.json if OPENAI_API_KEY is missing or quota hit.
    """
    log.info("[Stage 1] Scriptwriter starting ...")

    # Fast-path: user supplied a script file
    if args.script:
        path = Path(args.script)
        if not path.exists():
            log.error(f"--script file not found: {path}")
            sys.exit(1)
        script = json.loads(path.read_text(encoding="utf-8"))
        log.info(f"[Stage 1] Loaded script from {path.name} — title: {script.get('title')}")
        return script

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        log.warning("[Stage 1] OPENAI_API_KEY not set — falling back to test_input.json")
        return _load_fallback_script()

    # Inject env vars for scriptwriter
    os.environ["RUN_ID"]               = RUN_ID
    os.environ["S3_SCRIPTS_BUCKET"]    = "mock-scripts"
    os.environ["SECRETS_MANAGER_NAME"] = "mock-secret"
    os.environ["AWS_REGION"]           = "eu-north-1"
    os.environ["REDIS_HOST"]           = "localhost"

    captured: dict = {}

    mock_sm = MagicMock()
    mock_sm.get_secret_value.return_value = {
        "SecretString": json.dumps({"OPENAI_API_KEY": api_key})
    }
    mock_s3 = MagicMock()

    def _capture_put(Bucket, Key, Body, **kwargs):
        captured.update(json.loads(Body.decode("utf-8")))
        log.info(f"[Stage 1] Script captured ({len(Body)} bytes)")

    mock_s3.put_object.side_effect = _capture_put

    def _boto3_client(service, **kwargs):
        return mock_sm if service == "secretsmanager" else mock_s3

    try:
        with patch("boto3.client", side_effect=_boto3_client), \
             patch("redis.Redis", return_value=MagicMock()):
            import agent.scriptwriter as sw
            sw.run()

        script = captured
        if not script:
            raise RuntimeError("Scriptwriter returned empty script")
        log.info(f"[Stage 1] Done — title: {script.get('title')}")
        return script

    except Exception as exc:
        log.warning(f"[Stage 1] Scriptwriter failed ({exc}) — falling back to test_input.json")
        return _load_fallback_script()


def _load_fallback_script() -> dict:
    fallback = ROOT / "test_input.json"
    if not fallback.exists():
        log.error("test_input.json not found. Run: python setup_test_assets.py")
        sys.exit(1)
    script = json.loads(fallback.read_text(encoding="utf-8"))
    log.info(f"[Stage 1] Fallback script loaded — title: {script.get('title')}")
    return script


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2 — TTS voiceover via edge-tts
# ══════════════════════════════════════════════════════════════════════════════

def _run_tts(script: dict, voice: str) -> Path:
    """
    Converts script voiceover_text (or concatenated body text) to MP3
    using edge-tts (Microsoft TTS, free, no API key needed).
    Returns path to voiceover.mp3.
    """
    log.info(f"[Stage 2] TTS starting — voice: {voice}")

    try:
        import edge_tts
    except ImportError:
        log.error("edge-tts not installed. Run: pip install edge-tts")
        sys.exit(1)

    # Prefer the pre-built voiceover_text; fall back to joining body segments
    text = script.get("voiceover_text", "").strip()
    if not text:
        parts = []
        # Include selected hook (first one)
        hooks = script.get("hooks", [])
        if hooks:
            parts.append(hooks[0].get("text", ""))
        for seg in script.get("body", []):
            parts.append(seg.get("text", ""))
        cta = script.get("cta", "")
        if cta:
            parts.append(cta)
        text = "  ".join(p for p in parts if p)

    if not text:
        log.error("[Stage 2] No text found in script for TTS")
        sys.exit(1)

    word_count = len(text.split())
    log.info(f"[Stage 2] Text: {word_count} words (~{word_count // 2}s at average TTS speed)")

    out = WORKSPACE / f"{RUN_ID}_voiceover.mp3"

    async def _synthesize():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(out))

    asyncio.run(_synthesize())

    size_kb = out.stat().st_size // 1024
    log.info(f"[Stage 2] Voiceover saved → {out.name} ({size_kb} KB)")
    return out


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 3 — Video Editor (faceless mode)
# ══════════════════════════════════════════════════════════════════════════════

def _run_video_editor(script: dict, voiceover: Path) -> Path:
    """
    Runs Agent 3 in faceless mode (no avatar/chromakey).
    Mocks S3 download (script.json) and upload (final.mp4).
    Returns path to the rendered video.
    """
    log.info("[Stage 3] Video Editor starting (faceless mode) ...")

    if not shutil.which("ffmpeg"):
        log.error("FFmpeg not in PATH. Install from https://www.gyan.dev/ffmpeg/builds/")
        sys.exit(1)

    # Write script to a staging area so S3 mock copies FROM here TO workspace
    staging = WORKSPACE / "_s3_staging"
    staging.mkdir(exist_ok=True)
    script_path = staging / "script.json"
    script_path.write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Set env vars
    os.environ["RUN_ID"]                = RUN_ID
    os.environ["S3_SCRIPTS_BUCKET"]     = "mock-scripts"
    os.environ["S3_RAW_VIDEO_BUCKET"]   = "mock-raw-video"
    os.environ["S3_ASSETS_BUCKET"]      = ""
    os.environ["S3_FINAL_VIDEO_BUCKET"] = "mock-final-video"
    os.environ["AWS_REGION"]            = "eu-north-1"
    os.environ["REDIS_HOST"]            = "localhost"
    os.environ["OUTPUTS_DIR"]           = str(OUTPUTS)
    os.environ["FACELESS_AUDIO"]        = str(voiceover)   # ← activates faceless mode

    final_out: list[Path] = []

    # S3 mock
    mock_s3 = MagicMock()

    def _download(bucket, key, dest):
        fname = Path(key).name
        # Look in staging first, then workspace root
        src = staging / fname
        if not src.exists():
            raise FileNotFoundError(f"Mock S3: no staging file for '{key}' (expected {src})")
        shutil.copy2(src, dest)
        log.info(f"[mock S3 download] {bucket}/{key} -> {Path(dest).name}")

    def _upload(src, bucket, key, **kwargs):
        dst = OUTPUTS / Path(key).name
        shutil.copy2(src, dst)
        size_mib = Path(src).stat().st_size / (1024 * 1024)
        final_out.append(dst)
        log.info(f"[mock S3 upload] {Path(src).name} -> {bucket}/{key}  ({size_mib:.1f} MiB)")

    mock_s3.download_file.side_effect  = _download
    mock_s3.upload_file.side_effect    = _upload
    mock_s3.list_objects_v2.return_value = {"Contents": []}

    def _boto3_client(service, **kwargs):
        return mock_s3

    with patch("boto3.client", side_effect=_boto3_client), \
         patch("redis.Redis", return_value=MagicMock()):

        import agent.video_editor as ve
        ve.WORKSPACE = WORKSPACE   # redirect from /tmp/render to local path
        ve.run()

    # Find output
    local_copy = OUTPUTS / f"{RUN_ID}.mp4"
    if local_copy.exists() and local_copy.stat().st_size > 50_000:
        log.info(f"[Stage 3] Done → {local_copy.name} "
                 f"({local_copy.stat().st_size / (1024*1024):.1f} MiB)")
        return local_copy

    raise RuntimeError("Video editor did not produce a valid output file")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Stage 1
    script = _run_scriptwriter()

    # Save script for inspection
    script_out = OUTPUTS / f"{RUN_ID}_script.json"
    script_out.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Script saved → {script_out.name}")

    # Stage 2
    voiceover = _run_tts(script, voice=args.voice)

    # Stage 3
    video = _run_video_editor(script, voiceover)

    # Final summary
    log.info("")
    log.info("=" * 60)
    log.info("  FACTORY COMPLETE")
    log.info(f"  Script  : {script_out}")
    log.info(f"  Voiceover: {voiceover}")
    log.info(f"  Video   : {video}  ({video.stat().st_size / (1024*1024):.1f} MiB)")
    log.info("=" * 60)
