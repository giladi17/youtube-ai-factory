"""
Agent 4: SEO Publisher
=======================
1. Download script.json and final.mp4 from S3
2. Use OpenAI GPT-4o to generate clickbait title, SEO description, and tags
3. Authenticate to YouTube Data API v3 via OAuth2 refresh token
4. Upload final.mp4 with generated metadata (resumable upload, chunked)
5. Report live YouTube URL to Redis
6. Update pipeline stage

Input  artifacts: s3://yt-scripts/{RUN_ID}/script.json
                  s3://yt-final-video/{RUN_ID}/final.mp4
Redis update:     run:{RUN_ID}:stage = "published"
                  run:{RUN_ID}:result = "https://youtu.be/<video_id>"
"""
import json
import logging
import os
import tempfile
from pathlib import Path

import boto3
import redis
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from openai import OpenAI

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
RUN_ID                = os.environ["RUN_ID"]
S3_SCRIPTS_BUCKET     = os.environ["S3_SCRIPTS_BUCKET"]
S3_FINAL_VIDEO_BUCKET = os.environ["S3_FINAL_VIDEO_BUCKET"]
SECRETS_MANAGER_NAME  = os.environ["SECRETS_MANAGER_NAME"]
AWS_REGION            = os.environ.get("AWS_REGION", "eu-north-1")
REDIS_HOST            = os.environ.get("REDIS_HOST", "redis-service")
CHUNK_SIZE            = int(os.environ.get("YOUTUBE_UPLOAD_CHUNK_SIZE", str(10 * 1024 * 1024)))  # 10 MiB

# YouTube category: 28 = Science & Technology
YT_CATEGORY_ID = "28"


def _get_secret() -> dict:
    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    return json.loads(sm.get_secret_value(SecretId=SECRETS_MANAGER_NAME)["SecretString"])


def _download_s3(bucket: str, key: str, dest: Path) -> Path:
    s3 = boto3.client("s3", region_name=AWS_REGION)
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, key, str(dest))
    logger.info(f"Downloaded s3://{bucket}/{key} → {dest}")
    return dest


# ─────────────────────────────────────────────────────────────────────────
# SEO metadata generation
# ─────────────────────────────────────────────────────────────────────────

SEO_SCHEMA = """{
  "title": "string — YouTube title, max 70 chars, compelling and searchable",
  "description": "string — full SEO description, 800-2000 chars. Include: hook paragraph, key points with timestamps, related keywords naturally embedded, subscribe CTA at the end",
  "tags": ["tag1", "tag2", "..."]
}

Rules:
- Title must be under 70 characters and create curiosity or urgency
- Description must NOT include the title text verbatim
- Include exactly 15-20 tags, each under 30 characters
- Tags should mix: broad terms, specific terms, question-form terms"""


def _generate_metadata(openai_client: OpenAI, script: dict) -> dict:
    topic       = script.get("topic", "")
    keywords    = script.get("keywords", [])
    hook        = script.get("hook", "")
    body        = script.get("body", [])
    body_titles = [seg.get("title", "") for seg in body]

    prompt = (
        f"Topic: {topic}\n"
        f"Hook: {hook}\n"
        f"Body sections: {', '.join(body_titles)}\n"
        f"Keywords: {', '.join(keywords)}\n\n"
        "Generate optimised YouTube metadata that will maximise click-through rate and search ranking."
    )

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        max_tokens=1200,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a YouTube SEO specialist. Generate viral-optimised metadata "
                    "for a technology YouTube video.\n\nReturn ONLY valid JSON:\n" + SEO_SCHEMA
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    metadata = json.loads(response.choices[0].message.content)
    logger.info(f"SEO metadata generated | title: {metadata.get('title')}")
    return metadata


# ─────────────────────────────────────────────────────────────────────────
# YouTube upload
# ─────────────────────────────────────────────────────────────────────────

def _build_youtube_client(secrets: dict):
    """Build an authenticated YouTube Data API v3 client using OAuth2 refresh token."""
    creds = Credentials(
        token=None,
        refresh_token=secrets["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=secrets["YOUTUBE_CLIENT_ID"],
        client_secret=secrets["YOUTUBE_CLIENT_SECRET"],
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _upload_video(youtube, video_path: Path, title: str, description: str, tags: list[str]) -> str:
    """Upload video to YouTube using resumable upload. Returns video ID."""
    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=CHUNK_SIZE,
        resumable=True,
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title":       title[:100],       # YouTube hard limit
                "description": description[:5000], # YouTube hard limit
                "tags":        tags[:500],
                "categoryId":  YT_CATEGORY_ID,
            },
            "status": {
                "privacyStatus":           "public",
                "selfDeclaredMadeForKids": False,
            },
        },
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            logger.info(f"Upload progress: {pct}%")

    video_id = response["id"]
    logger.info(f"YouTube upload complete | video_id={video_id}")
    return video_id


# ─────────────────────────────────────────────────────────────────────────
# Redis helpers
# ─────────────────────────────────────────────────────────────────────────

def _update_redis(stage: str, result_url: str | None = None) -> None:
    r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
    r.setex(f"run:{RUN_ID}:stage", 86400, stage)
    if result_url:
        r.setex(f"run:{RUN_ID}:result", 86400, result_url)


# ─────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────

def run() -> None:
    logger.info(f"SEO Publisher starting | run_id={RUN_ID}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # ── 1. Download inputs ────────────────────────────────────────────
        script_path = _download_s3(S3_SCRIPTS_BUCKET,     f"{RUN_ID}/script.json", tmp / "script.json")
        video_path  = _download_s3(S3_FINAL_VIDEO_BUCKET, f"{RUN_ID}/final.mp4",   tmp / "final.mp4")

        # ── 2. Load secrets ───────────────────────────────────────────────
        secrets       = _get_secret()
        openai_client = OpenAI(api_key=secrets["OPENAI_API_KEY"])

        # ── 3. Generate SEO metadata ──────────────────────────────────────
        with open(script_path) as f:
            script = json.load(f)

        metadata    = _generate_metadata(openai_client, script)
        title       = metadata["title"]
        description = metadata["description"]
        tags        = metadata.get("tags", [])

        # ── 4. Upload to YouTube ──────────────────────────────────────────
        youtube  = _build_youtube_client(secrets)
        video_id = _upload_video(youtube, video_path, title, description, tags)

        yt_url = f"https://youtu.be/{video_id}"
        logger.info(f"Published: {yt_url}")

        # ── 5. Update Redis ───────────────────────────────────────────────
        _update_redis("published", yt_url)

    logger.info(f"SEO Publisher done | run_id={RUN_ID} | url={yt_url}")
