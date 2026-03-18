# ── Standard agent image — Scriptwriter, Avatar Director, SEO Publisher ───
# Lightweight Python 3.11 slim. No FFmpeg.
# Image: giladi17/yt-factory-agent:latest
FROM python:3.11-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.11-slim

WORKDIR /app
COPY --from=builder /root/.local /root/.local

# Dispatcher + all light-agent skills
COPY agent.py          .
COPY scriptwriter.py   .
COPY avatar_director.py .
COPY seo_publisher.py  .

ENV PATH=/root/.local/bin:$PATH

CMD ["python", "agent.py"]
