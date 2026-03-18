"""
Local test runner for Elite Video Editor
==========================================
Mocks:  S3 (upload/download), Redis
Real:   FFmpeg pipeline (all passes), local assets

Prerequisites:
  python setup_test_assets.py   <- creates avatar_input.mp4, lo-fi.wav, openai.png, test_input.json

Usage (PowerShell):
  python test_video_editor.py

Output:
  openclaw/outputs/test-run-001.mp4
"""
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).resolve().parent     # openclaw/
AVATAR_SRC  = ROOT / "avatar_input.mp4"
SCRIPT_SRC  = ROOT / "test_input.json"
WORKSPACE   = ROOT / "tmp_render_test"            # avoid /tmp on Windows
OUTPUTS     = ROOT / "outputs"
RUN_ID      = "test-run-001"

# ── Pre-flight checks ──────────────────────────────────────────────────────────
missing = [p for p in (AVATAR_SRC, SCRIPT_SRC) if not p.exists() or p.stat().st_size == 0]
if missing:
    print("Missing test assets. Run first:")
    print("  python setup_test_assets.py")
    for p in missing:
        print(f"  Missing: {p}")
    sys.exit(1)

if not shutil.which("ffmpeg"):
    print("FFmpeg not found in PATH.")
    print("Install from: https://www.gyan.dev/ffmpeg/builds/")
    sys.exit(1)

WORKSPACE.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)
logging.info(f"Workspace: {WORKSPACE}")

# ── Inject environment variables ───────────────────────────────────────────────
os.environ["RUN_ID"]                = RUN_ID
os.environ["S3_SCRIPTS_BUCKET"]     = "mock-scripts"
os.environ["S3_RAW_VIDEO_BUCKET"]   = "mock-raw-video"
os.environ["S3_ASSETS_BUCKET"]      = ""
os.environ["S3_FINAL_VIDEO_BUCKET"] = "mock-final-video"
os.environ["AWS_REGION"]            = "eu-north-1"
os.environ["REDIS_HOST"]            = "localhost"
# Override workspace and outputs to local paths (avoid /tmp on Windows)
os.environ["OUTPUTS_DIR"]           = str(OUTPUTS)

# ── S3 mock — maps keys to local source files ──────────────────────────────────
# Key map: S3 key suffix → local source file
S3_KEY_MAP: dict[str, Path] = {
    "avatar.mp4":  AVATAR_SRC,
    "script.json": SCRIPT_SRC,
}

def make_mock_s3():
    client = MagicMock()

    def _download_file(bucket, key, dest):
        """Resolve S3 key to a local source file by filename."""
        filename = Path(key).name
        src = S3_KEY_MAP.get(filename)
        if src is None or not src.exists():
            raise FileNotFoundError(f"Mock S3: no mapping for key '{key}'")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        logging.info(f"[mock S3 download] {bucket}/{key} -> {dest}")

    def _upload_file(src, bucket, key, **kwargs):
        dest = OUTPUTS / Path(key).name
        shutil.copy2(src, dest)
        size_mib = Path(src).stat().st_size / (1024 * 1024)
        logging.info(f"[mock S3 upload] {src} -> {bucket}/{key}  ({size_mib:.1f} MiB)")

    client.download_file.side_effect = _download_file
    client.upload_file.side_effect   = _upload_file
    client.list_objects_v2.return_value = {"Contents": []}
    return client

mock_s3_client    = make_mock_s3()
mock_redis_client = MagicMock()

def mock_boto3_client(service, **kwargs):
    if service == "s3":
        return mock_s3_client
    return MagicMock()

# ── Patch WORKSPACE inside video_editor to our local path ─────────────────────
# video_editor.py hardcodes WORKSPACE = Path("/tmp/render") which doesn't exist on Windows.
# We patch it after import.

with patch("boto3.client", side_effect=mock_boto3_client), \
     patch("redis.Redis", return_value=mock_redis_client):

    import agent.video_editor as ve

    # Override WORKSPACE so FFmpeg writes to our local path
    ve.WORKSPACE = WORKSPACE

    logging.info("=" * 60)
    logging.info(f"Starting Elite Video Editor  run_id={RUN_ID}")
    logging.info(f"Avatar  : {AVATAR_SRC} ({AVATAR_SRC.stat().st_size // 1024} KB)")
    logging.info(f"Script  : {SCRIPT_SRC.name}")
    logging.info(f"Workspace: {WORKSPACE}")
    logging.info("=" * 60)

    ve.run()

# ── Report ─────────────────────────────────────────────────────────────────────
final = OUTPUTS / f"{RUN_ID}.mp4"
if final.exists() and final.stat().st_size > 50_000:
    size_mib = final.stat().st_size / (1024 * 1024)
    print()
    print("=" * 60)
    print(f"  SUCCESS  {final.name}  ({size_mib:.1f} MiB)")
    print(f"  Path: {final}")
    print("=" * 60)
else:
    print()
    print("Output file missing or too small — check logs above.")
    sys.exit(1)
