"""
Agent 3: Video Editor  ★ HEAVY ★
==================================
Runs on a Karpenter-provisioned c5.2xlarge node.

Pipeline:
  1. Download inputs from S3 to /tmp/render workspace
       - avatar.mp4  (green-screen render from HeyGen)
       - script.json (for voiceover text → subtitle generation)
       - background  (first PNG/JPG found in s3://assets/backgrounds/)
       - music       (random MP3 from s3://assets/music/)
  2. Generate SRT subtitle file from voiceover_text
  3. Run FFmpeg filtergraph:
       a. Scale background to 1920×1080
       b. Chromakey avatar (remove #00B140 green), scale to 1920×1080
       c. Composite: background + keyed avatar overlay
       d. Burn subtitles via libass
       e. Mix voiceover (from avatar.mp4) with background music at -20dBFS
       f. H.264 encode, 1080p@30fps, AAC 192k, faststart
  4. Upload final.mp4 to S3
  5. Update Redis

Input  artifacts: s3://yt-scripts/{RUN_ID}/script.json
                  s3://yt-raw-video/{RUN_ID}/avatar.mp4
                  s3://yt-assets/backgrounds/*.{png,jpg}
                  s3://yt-assets/music/*.mp3
Output artifact:  s3://yt-final-video/{RUN_ID}/final.mp4
Redis update:     run:{RUN_ID}:stage = "video_ready"
"""
import json
import logging
import os
import random
import subprocess
import sys
from pathlib import Path

import boto3
import redis

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
RUN_ID                = os.environ["RUN_ID"]
S3_SCRIPTS_BUCKET     = os.environ["S3_SCRIPTS_BUCKET"]
S3_RAW_VIDEO_BUCKET   = os.environ["S3_RAW_VIDEO_BUCKET"]
S3_ASSETS_BUCKET      = os.environ["S3_ASSETS_BUCKET"]
S3_FINAL_VIDEO_BUCKET = os.environ["S3_FINAL_VIDEO_BUCKET"]
AWS_REGION            = os.environ.get("AWS_REGION", "eu-north-1")
REDIS_HOST            = os.environ.get("REDIS_HOST", "redis-service")
RESOLUTION            = os.environ.get("VIDEO_EDITOR_TARGET_RESOLUTION", "1920x1080")
FPS                   = os.environ.get("VIDEO_EDITOR_TARGET_FPS", "30")

WORKSPACE = Path("/tmp/render")
WIDTH, HEIGHT = map(int, RESOLUTION.split("x"))

# Words per minute for subtitle timing estimation
SPEAKING_RATE_WPM = 150


# ─────────────────────────────────────────────────────────────────────────
# S3 helpers
# ─────────────────────────────────────────────────────────────────────────

def _s3() -> boto3.client:
    return boto3.client("s3", region_name=AWS_REGION)


def _download(bucket: str, key: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _s3().download_file(bucket, key, str(dest))
    logger.info(f"Downloaded s3://{bucket}/{key} → {dest} ({dest.stat().st_size // 1024} KB)")
    return dest


def _list_prefix(bucket: str, prefix: str) -> list[str]:
    resp = _s3().list_objects_v2(Bucket=bucket, Prefix=prefix)
    return [obj["Key"] for obj in resp.get("Contents", [])]


def _upload(local_path: Path, bucket: str, key: str) -> None:
    _s3().upload_file(
        str(local_path),
        bucket,
        key,
        ExtraArgs={"ContentType": "video/mp4"},
    )
    logger.info(f"Uploaded {local_path} → s3://{bucket}/{key}")


# ─────────────────────────────────────────────────────────────────────────
# Asset resolution
# ─────────────────────────────────────────────────────────────────────────

def _get_background() -> Path | None:
    """Download the first available background image from S3 assets."""
    keys = _list_prefix(S3_ASSETS_BUCKET, "backgrounds/")
    image_keys = [k for k in keys if k.lower().endswith((".png", ".jpg", ".jpeg"))]
    if not image_keys:
        logger.warning("No background images found in assets bucket — using solid colour fallback")
        return None
    key  = image_keys[0]
    dest = WORKSPACE / "background" / Path(key).name
    return _download(S3_ASSETS_BUCKET, key, dest)


def _get_music() -> Path | None:
    """Download a random background music track from S3 assets."""
    keys = _list_prefix(S3_ASSETS_BUCKET, "music/")
    mp3_keys = [k for k in keys if k.lower().endswith(".mp3")]
    if not mp3_keys:
        logger.warning("No music tracks found in assets bucket — output will have voiceover only")
        return None
    key  = random.choice(mp3_keys)
    dest = WORKSPACE / "music" / Path(key).name
    return _download(S3_ASSETS_BUCKET, key, dest)


# ─────────────────────────────────────────────────────────────────────────
# Subtitle generation
# ─────────────────────────────────────────────────────────────────────────

def _srt_timestamp(seconds: float) -> str:
    h   = int(seconds // 3600)
    m   = int((seconds % 3600) // 60)
    s   = int(seconds % 60)
    ms  = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _generate_srt(voiceover_text: str, words_per_line: int = 8) -> str:
    """
    Convert voiceover text to SRT format.
    Timing is estimated at SPEAKING_RATE_WPM words per minute.
    """
    words         = voiceover_text.split()
    secs_per_word = 60.0 / SPEAKING_RATE_WPM
    lines         = []
    idx           = 1
    word_pos      = 0

    for i in range(0, len(words), words_per_line):
        chunk     = words[i : i + words_per_line]
        start_sec = word_pos * secs_per_word
        end_sec   = (word_pos + len(chunk)) * secs_per_word

        lines.append(str(idx))
        lines.append(f"{_srt_timestamp(start_sec)} --> {_srt_timestamp(end_sec)}")
        lines.append(" ".join(chunk))
        lines.append("")

        idx      += 1
        word_pos += len(chunk)

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# FFmpeg pipeline
# ─────────────────────────────────────────────────────────────────────────

def _build_ffmpeg_cmd(
    avatar_path: Path,
    background_path: Path | None,
    music_path: Path | None,
    srt_path: Path,
    output_path: Path,
) -> list[str]:
    """
    Construct the FFmpeg command for the full compositing pipeline.

    Filter graph overview:
      [bg]   — background (image looped or colour source)
      [0:v]  — avatar with green screen → chromakey → scale → [keyed]
      [bg][keyed] → overlay → [composed]
      [composed] → subtitles → [video_out]
      [0:a] → voice volume → [voice]
      [2:a or generated silence] → music volume + fade-out → [music]
      [voice][music] → amix → [audio_out]
    """
    # Escape SRT path for libass (colons and backslashes need escaping)
    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")

    inputs = ["-i", str(avatar_path)]

    # Background: image or generated colour source
    if background_path:
        inputs += ["-loop", "1", "-i", str(background_path)]
        bg_filter   = f"[1:v] scale={WIDTH}:{HEIGHT},setsar=1 [bg];"
        bg_input_ix = 1
    else:
        # Fallback: dark gradient background via lavfi
        inputs += [
            "-f", "lavfi",
            "-i", f"color=c=0x1a1a2e:size={WIDTH}x{HEIGHT}:rate={FPS}",
        ]
        bg_filter   = f"[1:v] setsar=1 [bg];"
        bg_input_ix = 1

    # Music input
    if music_path:
        inputs += ["-i", str(music_path)]
        music_idx = bg_input_ix + 1
        music_filter = (
            f"[{music_idx}:a] volume=0.15,"
            f"afade=t=out:st=0:d=3 [music_raw];"   # fade will be trimmed to video length
        )
        audio_mix = "[voice][music_raw] amix=inputs=2:normalize=0 [audio_out]"
    else:
        music_filter = ""
        audio_mix    = "[voice] anull [audio_out]"

    filter_complex = (
        # ── Video chain ──────────────────────────────────────────────
        f"{bg_filter}"
        # Chromakey: remove #00B140 green, similarity=0.3, blend=0.05
        f"[0:v] chromakey=0x00B140:0.3:0.05, scale={WIDTH}:{HEIGHT},setsar=1 [keyed];"
        f"[bg][keyed] overlay=0:0 [composed];"
        # Subtitle burn-in
        f"[composed] subtitles='{srt_escaped}':force_style='FontSize=24,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=1' [video_out];"
        # ── Audio chain ─────────────────────────────────────────────
        f"[0:a] volume=1.0 [voice];"
        f"{music_filter}"
        f"{audio_mix}"
    )

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", "[video_out]",
            "-map", "[audio_out]",
            # Video codec
            "-c:v",     "libx264",
            "-preset",  "medium",
            "-crf",     "22",
            "-r",       FPS,
            "-pix_fmt", "yuv420p",
            # Audio codec
            "-c:a",     "aac",
            "-b:a",     "192k",
            # Container
            "-movflags", "+faststart",
            str(output_path),
        ]
    )
    return cmd


def _run_ffmpeg(cmd: list[str]) -> None:
    logger.info(f"FFmpeg command: {' '.join(cmd[:8])} ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"FFmpeg stderr:\n{result.stderr[-3000:]}")
        raise RuntimeError(f"FFmpeg exited with code {result.returncode}")
    logger.info("FFmpeg render complete")


# ─────────────────────────────────────────────────────────────────────────
# Redis helper
# ─────────────────────────────────────────────────────────────────────────

def _update_redis(stage: str) -> None:
    r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
    r.setex(f"run:{RUN_ID}:stage", 86400, stage)


# ─────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────

def run() -> None:
    logger.info(f"Video Editor starting | run_id={RUN_ID} | resolution={RESOLUTION} | fps={FPS}")

    WORKSPACE.mkdir(parents=True, exist_ok=True)

    # ── 1. Download inputs ────────────────────────────────────────────────
    avatar_path = _download(S3_RAW_VIDEO_BUCKET, f"{RUN_ID}/avatar.mp4", WORKSPACE / "avatar.mp4")
    script_path = _download(S3_SCRIPTS_BUCKET,   f"{RUN_ID}/script.json", WORKSPACE / "script.json")
    background  = _get_background()
    music       = _get_music()

    # ── 2. Parse script → generate subtitles ─────────────────────────────
    with open(script_path) as f:
        script = json.load(f)

    voiceover_text = script.get("voiceover_text", "")
    if not voiceover_text:
        raise ValueError("script.json missing 'voiceover_text' field")

    srt_content = _generate_srt(voiceover_text)
    srt_path    = WORKSPACE / "subtitles.srt"
    srt_path.write_text(srt_content, encoding="utf-8")
    logger.info(f"SRT generated ({len(srt_content.splitlines())} lines)")

    # ── 3. Run FFmpeg ─────────────────────────────────────────────────────
    output_path = WORKSPACE / "final.mp4"
    cmd = _build_ffmpeg_cmd(avatar_path, background, music, srt_path, output_path)
    _run_ffmpeg(cmd)

    if not output_path.exists() or output_path.stat().st_size < 10_000:
        raise RuntimeError(f"Output file missing or suspiciously small: {output_path.stat().st_size} bytes")

    logger.info(f"Final video size: {output_path.stat().st_size // (1024*1024)} MiB")

    # ── 4. Upload to S3 ───────────────────────────────────────────────────
    _upload(output_path, S3_FINAL_VIDEO_BUCKET, f"{RUN_ID}/final.mp4")

    # ── 5. Update Redis ───────────────────────────────────────────────────
    _update_redis("video_ready")

    logger.info(f"Video Editor done | run_id={RUN_ID}")
