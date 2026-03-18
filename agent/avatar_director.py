"""
Agent 2: Avatar Director
=========================
1. Download script.json from S3
2. POST voiceover_text to HeyGen v2 API with a fixed avatar + voice
3. Poll HeyGen status until render completes (up to HEYGEN_MAX_WAIT_SEC)
4. Stream-download the rendered green-screen MP4 and upload it to S3
5. Update pipeline stage in Redis

Input  artifact: s3://yt-scripts-{env}/{RUN_ID}/script.json
Output artifact: s3://yt-raw-video-{env}/{RUN_ID}/avatar.mp4
Redis update:    run:{RUN_ID}:stage = "avatar_ready"
"""
import json
import logging
import os
import time

import boto3
import redis
import requests

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
RUN_ID               = os.environ["RUN_ID"]
S3_SCRIPTS_BUCKET    = os.environ["S3_SCRIPTS_BUCKET"]
S3_RAW_VIDEO_BUCKET  = os.environ["S3_RAW_VIDEO_BUCKET"]
SECRETS_MANAGER_NAME = os.environ["SECRETS_MANAGER_NAME"]
AWS_REGION           = os.environ.get("AWS_REGION", "eu-north-1")
REDIS_HOST           = os.environ.get("REDIS_HOST", "redis-service")
HEYGEN_POLL_INTERVAL = int(os.environ.get("HEYGEN_POLL_INTERVAL_SEC", "30"))
HEYGEN_MAX_WAIT      = int(os.environ.get("HEYGEN_MAX_WAIT_SEC", "1800"))

# Avatar + voice IDs — override via environment variables to change the presenter
HEYGEN_AVATAR_ID = os.environ.get("HEYGEN_AVATAR_ID", "Angela-inblackskirt-20220820")
HEYGEN_VOICE_ID  = os.environ.get("HEYGEN_VOICE_ID",  "1bd001e7e50f421d891986aad5158bc8")
HEYGEN_API_BASE  = "https://api.heygen.com"


def _get_secret() -> dict:
    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    return json.loads(sm.get_secret_value(SecretId=SECRETS_MANAGER_NAME)["SecretString"])


def _download_script() -> dict:
    s3       = boto3.client("s3", region_name=AWS_REGION)
    response = s3.get_object(Bucket=S3_SCRIPTS_BUCKET, Key=f"{RUN_ID}/script.json")
    return json.loads(response["Body"].read())


def _submit_heygen(api_key: str, voiceover_text: str) -> str:
    """Submit a HeyGen v2 video generation request. Returns video_id."""
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
                "input_text": voiceover_text,
                "voice_id":   HEYGEN_VOICE_ID,
                "speed":      1.0,
            },
            "background": {
                "type":  "color",
                "value": "#00B140",   # green screen
            },
        }],
        "dimension":    {"width": 1280, "height": 720},
        "aspect_ratio": "16:9",
    }
    resp = requests.post(
        f"{HEYGEN_API_BASE}/v2/video/generate",
        headers=headers,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    video_id = resp.json()["data"]["video_id"]
    logger.info(f"HeyGen job submitted | video_id={video_id}")
    return video_id


def _poll_heygen(api_key: str, video_id: str) -> str:
    """Poll HeyGen until render is complete. Returns the download URL."""
    headers = {"X-Api-Key": api_key}
    elapsed = 0

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
        logger.info(f"HeyGen status={status} | elapsed={elapsed}s | video_id={video_id}")

        if status == "completed":
            return data["video_url"]
        if status == "failed":
            raise RuntimeError(f"HeyGen render failed: {data.get('error', 'unknown')}")

        time.sleep(HEYGEN_POLL_INTERVAL)
        elapsed += HEYGEN_POLL_INTERVAL

    raise TimeoutError(f"HeyGen render timed out after {HEYGEN_MAX_WAIT}s")


def _stream_to_s3(video_url: str, dest_bucket: str, dest_key: str) -> None:
    """Stream-download video from URL and upload directly to S3.
    Uses requests streaming to avoid buffering the entire file in memory.
    """
    s3 = boto3.client("s3", region_name=AWS_REGION)
    with requests.get(video_url, stream=True, timeout=300) as r:
        r.raise_for_status()
        s3.upload_fileobj(
            r.raw,
            dest_bucket,
            dest_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
    logger.info(f"Avatar video uploaded → s3://{dest_bucket}/{dest_key}")


def _update_redis(stage: str) -> None:
    r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
    r.setex(f"run:{RUN_ID}:stage", 86400, stage)


def run() -> None:
    logger.info(f"Avatar Director starting | run_id={RUN_ID}")

    secrets  = _get_secret()
    api_key  = secrets["HEYGEN_API_KEY"]
    script   = _download_script()

    voiceover_text = script.get("voiceover_text", "")
    if not voiceover_text:
        raise ValueError("script.json is missing 'voiceover_text' field")

    logger.info(f"Voiceover text length: {len(voiceover_text)} chars | topic: {script.get('topic')}")

    video_id  = _submit_heygen(api_key, voiceover_text)
    video_url = _poll_heygen(api_key, video_id)

    dest_key = f"{RUN_ID}/avatar.mp4"
    _stream_to_s3(video_url, S3_RAW_VIDEO_BUCKET, dest_key)
    _update_redis("avatar_ready")

    logger.info(f"Avatar Director done | run_id={RUN_ID}")
