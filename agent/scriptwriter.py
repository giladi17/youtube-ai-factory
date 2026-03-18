"""
Agent 1: Scriptwriter
======================
1. Fetch recent tech stories from RSS feeds
2. Use OpenAI GPT-4o to select the best YouTube topic and write a full script
3. Upload structured script.json to S3
4. Update pipeline stage in Redis

Output artifact: s3://yt-scripts-{env}/{RUN_ID}/script.json
Redis update:    run:{RUN_ID}:stage = "scripted"
"""
import json
import logging
import os

import boto3
import feedparser
import redis
from openai import OpenAI

logger = logging.getLogger(__name__)

# ── Config from environment (injected via ConfigMap + agent.py) ───────────
RUN_ID               = os.environ["RUN_ID"]
S3_SCRIPTS_BUCKET    = os.environ["S3_SCRIPTS_BUCKET"]
SECRETS_MANAGER_NAME = os.environ["SECRETS_MANAGER_NAME"]
AWS_REGION           = os.environ.get("AWS_REGION", "eu-north-1")
REDIS_HOST           = os.environ.get("REDIS_HOST", "redis-service")

# ── RSS sources — covers AI, cloud, developer, and consumer tech ──────────
RSS_FEEDS = [
    "https://techcrunch.com/feed/",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://hnrss.org/frontpage",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.feedburner.com/venturebeat/SZYF",
]

# Script structure schema (passed to GPT as a JSON mode prompt)
SCRIPT_SCHEMA = """{
  "topic": "string — concise video topic title",
  "hook": "string — first 15 seconds, attention-grabbing opener that promises value",
  "body": [
    {"segment": 1, "title": "string", "content": "string", "duration_sec": 60},
    {"segment": 2, "title": "string", "content": "string", "duration_sec": 60},
    {"segment": 3, "title": "string", "content": "string", "duration_sec": 60}
  ],
  "cta": "string — 30-second call-to-action: like, subscribe, comment prompt",
  "keywords": ["keyword1", "keyword2", "keyword3"],
  "duration_est_sec": 240,
  "voiceover_text": "string — complete script text for text-to-speech, natural conversational voice, no stage directions"
}"""


def _get_secret() -> dict:
    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    return json.loads(sm.get_secret_value(SecretId=SECRETS_MANAGER_NAME)["SecretString"])


def _fetch_rss_stories(max_per_feed: int = 5) -> list[dict]:
    stories = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                stories.append({
                    "title":   entry.get("title", ""),
                    "summary": entry.get("summary", "")[:400],
                    "source":  feed.feed.get("title", url),
                })
        except Exception as exc:
            logger.warning(f"RSS fetch failed for {url}: {exc}")
    logger.info(f"Fetched {len(stories)} stories from {len(RSS_FEEDS)} RSS feeds")
    return stories


def _write_script(openai_client: OpenAI, stories: list[dict]) -> dict:
    stories_text = "\n".join(
        f"• [{s['source']}] {s['title']} — {s['summary'][:200]}"
        for s in stories
    )

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        max_tokens=2500,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a YouTube content strategist specialising in technology videos. "
                    "Given a list of recent tech news stories, select the single most "
                    "compelling topic for a YouTube video (aim for topics with broad appeal "
                    "and high search volume) and write a complete, engaging script.\n\n"
                    "Return ONLY valid JSON matching this exact schema:\n"
                    + SCRIPT_SCHEMA
                ),
            },
            {
                "role": "user",
                "content": (
                    "Here are today's top tech stories. Choose the best one for YouTube "
                    "and write the full script:\n\n" + stories_text
                ),
            },
        ],
    )
    script = json.loads(response.choices[0].message.content)
    logger.info(f"Script generated | topic: {script.get('topic')} | est. duration: {script.get('duration_est_sec')}s")
    return script


def _upload_script(script: dict) -> None:
    s3  = boto3.client("s3", region_name=AWS_REGION)
    key = f"{RUN_ID}/script.json"
    s3.put_object(
        Bucket=S3_SCRIPTS_BUCKET,
        Key=key,
        Body=json.dumps(script, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info(f"Script uploaded → s3://{S3_SCRIPTS_BUCKET}/{key}")


def _update_redis(stage: str) -> None:
    r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
    r.setex(f"run:{RUN_ID}:stage", 86400, stage)


def run() -> None:
    logger.info(f"Scriptwriter starting | run_id={RUN_ID}")

    secrets       = _get_secret()
    openai_client = OpenAI(api_key=secrets["OPENAI_API_KEY"])

    stories = _fetch_rss_stories()
    if not stories:
        raise RuntimeError("No RSS stories fetched — check network or feed URLs")

    script = _write_script(openai_client, stories)
    _upload_script(script)
    _update_redis("scripted")

    logger.info(f"Scriptwriter done | run_id={RUN_ID} | topic: {script.get('topic')}")
