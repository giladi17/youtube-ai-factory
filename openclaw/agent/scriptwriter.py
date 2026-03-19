"""
Agent 1: Elite Scriptwriter
============================
1. Fetch recent tech stories from RSS feeds
2. Use GPT-4o to produce a viral, high-retention YouTube script
3. Output includes 3 hooks, pacing cues, B-roll visual cues, CTR metadata
4. Upload structured script.json to S3
5. Update pipeline stage in Redis

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

# ── Config ────────────────────────────────────────────────────────────────────
RUN_ID               = os.environ["RUN_ID"]
S3_SCRIPTS_BUCKET    = os.environ["S3_SCRIPTS_BUCKET"]
SECRETS_MANAGER_NAME = os.environ["SECRETS_MANAGER_NAME"]
AWS_REGION           = os.environ.get("AWS_REGION", "eu-north-1")
REDIS_HOST           = os.environ.get("REDIS_HOST", "redis-service")

# ── RSS sources ───────────────────────────────────────────────────────────────
RSS_FEEDS = [
    "https://techcrunch.com/feed/",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://hnrss.org/frontpage",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.feedburner.com/venturebeat/SZYF",
]

# ── Elite script schema ───────────────────────────────────────────────────────
SCRIPT_SCHEMA = """{
  "title": "string — high-CTR YouTube title, MAX 50 characters, no clickbait lies",
  "thumbnail_idea": "string — vivid visual concept for thumbnail (colors, text overlay, subject expression)",
  "hooks": [
    {
      "type": "curiosity_gap",
      "text": "string — opens a question the viewer MUST stay to answer (15 sec)"
    },
    {
      "type": "fomo",
      "text": "string — makes viewer feel they'll miss something critical if they leave (15 sec)"
    },
    {
      "type": "big_reveal",
      "text": "string — teases a shocking fact or outcome revealed at end (15 sec)"
    }
  ],
  "body": [
    {
      "segment": 1,
      "title": "string",
      "text": "string — full spoken narration for this segment",
      "visual_cue": "string — FUTURISTIC TECH VISUAL: describe a holographic, cyberpunk, or high-tech scene. Examples: 'A glowing hologram of a neural network floating in a dark server room with neon blue data streams', 'Futuristic HUD overlay showing real-time AI metrics, neon purple on black', 'Cyberpunk cityscape at night with glowing AI billboard'. Always futuristic, always cinematic, always specific.",
      "tone": "[FAST] | [SLOW] | [DRAMATIC PAUSE] | [ENERGETIC] | [CALM]",
      "duration_sec": 60
    }
  ],
  "cta": "string — 20-second closing call-to-action (like, subscribe, comment hook)",
  "keywords": ["keyword1", "keyword2", "keyword3"],
  "duration_est_sec": 300,
  "voiceover_text": "string — PUNCHY BEAT NARRATION: max 5 words per sentence. Hard stop. New line. No filler words. Think TikTok captions read aloud — fast, snappy, impossible to ignore."
}"""

SYSTEM_PROMPT = """You are a world-class YouTube scriptwriter who has written viral scripts for channels like MrBeast, Veritasium, and MKBHD.

Your scripts have generated over 500M views. You understand what keeps viewers watching: psychological hooks, perfect pacing, and visual storytelling.

AESTHETIC MANDATE — FUTURISTIC HIGH-TECH:
This channel has a cyberpunk / futuristic AI aesthetic. Every visual_cue MUST depict holographic interfaces, neon data streams, glowing neural networks, or dark high-tech environments. Think: Westworld, Ex Machina, Tron Legacy. Never suggest real-world footage. Always suggest a synthetically generated, cinematic, futuristic image.

RULES YOU NEVER BREAK:
1. The hook must create an UNRESOLVED TENSION in the first 15 seconds — the brain cannot leave without resolving it.
2. Every body segment visual_cue MUST be a futuristic holographic or cyberpunk scene. Never suggest "show a screenshot" or real footage — always a cinematic AI-generated visual.
3. Pacing cues ([FAST], [SLOW], [DRAMATIC PAUSE], [ENERGETIC], [CALM]) must match the emotional arc — use [DRAMATIC PAUSE] before any shocking stat.
4. The title is MAX 50 characters. It must trigger either curiosity, fear, or desire in 3 seconds.
5. The thumbnail_idea must describe a futuristic scene so vivid a designer can recreate it: neon colors, glowing elements, dark background, a single bold subject.
6. voiceover_text must read like a human speaks — contractions, rhetorical questions, zero corporate language.
7. BEAT WRITING LAW — Every sentence in body[].text AND voiceover_text MUST be 2-5 words maximum. No exceptions. Write like this: "This changes everything. Three days ago. OpenAI dropped something. Nobody noticed. Here is what happened." Hard stops. Short bursts. The viewer's brain cannot scroll away from incomplete thoughts.

Return ONLY valid JSON matching this exact schema — no markdown, no explanation:
""" + SCRIPT_SCHEMA


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
    logger.info(f"Fetched {len(stories)} stories from {len(RSS_FEEDS)} feeds")
    return stories


def _write_script(openai_client: OpenAI, stories: list[dict]) -> dict:
    stories_text = "\n".join(
        f"• [{s['source']}] {s['title']} — {s['summary'][:200]}"
        for s in stories
    )

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        max_tokens=4000,
        temperature=0.85,   # creative but structured
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Here are today's top tech stories.\n\n"
                    + stories_text
                    + "\n\nPick the single most VIRAL-worthy topic and write the full elite script. "
                    "Think: which story will make someone stop scrolling at 2am and watch the whole thing?"
                ),
            },
        ],
    )

    script = json.loads(response.choices[0].message.content)
    logger.info(
        f"Script generated | title: {script.get('title')} | "
        f"segments: {len(script.get('body', []))} | "
        f"est. duration: {script.get('duration_est_sec')}s"
    )
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
    logger.info(f"Elite Scriptwriter starting | run_id={RUN_ID}")

    secrets       = _get_secret()
    openai_client = OpenAI(api_key=secrets["OPENAI_API_KEY"])

    stories = _fetch_rss_stories()
    if not stories:
        raise RuntimeError("No RSS stories fetched — check network or feed URLs")

    script = _write_script(openai_client, stories)
    _upload_script(script)
    _update_redis("scripted")

    logger.info(f"Elite Scriptwriter done | run_id={RUN_ID} | title: {script.get('title')}")
