# YouTube AI Factory

An **autonomous, event-driven video production pipeline** that runs entirely on AWS EKS.
Given a schedule (or a single webhook call), the system researches trending tech topics,
writes a script, generates an AI avatar video, edits it with FFmpeg, and publishes the
finished result to YouTube — with zero human intervention.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  TRIGGER  CronJob (Mon/Wed/Fri 08:00 UTC)  OR  POST /run             │
│                          │                                           │
│               Pipeline Orchestrator (Flask + K8s watcher)           │
│                  Deployment · default namespace                      │
└──────────────────────┬───────────────────────────────────────────────┘
                       │
         ┌─────────────▼──────────────┐
         │  Phase 1 — Scriptwriter    │  Spot t3.medium · light-agents pool
         │  RSS → GPT-4o → script.json│
         └─────────────┬──────────────┘
                       │  s3://yt-scripts/{run_id}/script.json
         ┌─────────────▼──────────────┐
         │  Phase 2 — Avatar Director │  Spot t3.medium · light-agents pool
         │  HeyGen API → avatar.mp4   │
         └─────────────┬──────────────┘
                       │  s3://yt-raw-video/{run_id}/avatar.mp4
         ┌─────────────▼──────────────┐
         │  Phase 3 — Video Editor    │  On-demand c5.2xlarge · video-editor pool
         │  FFmpeg composite → MP4    │  Karpenter provisions + destroys per run
         └─────────────┬──────────────┘
                       │  s3://yt-final-video/{run_id}/final.mp4
         ┌─────────────▼──────────────┐
         │  Phase 4 — SEO Publisher   │  Spot t3.medium · light-agents pool
         │  GPT-4o metadata + YT API  │
         └─────────────┬──────────────┘
                       │
                  Live YouTube URL
```

---

## Stack

| Layer | Technology |
|-------|-----------|
| Cloud | AWS (EKS, S3, Secrets Manager, CloudWatch) |
| Infrastructure | Terraform >= 1.6 · terraform-aws-modules/eks v20 |
| Auto-scaling | Karpenter v1.0 (scale-to-zero on video-editor pool) |
| Orchestration | Kubernetes 1.30 · Flask webhook · K8s Job watcher |
| AI — Script | OpenAI GPT-4o (JSON mode) |
| AI — Avatar | HeyGen v2 API (lip-sync, green-screen render) |
| Video editing | FFmpeg 6 (chromakey · subtitle burn-in · audio mix) |
| Publishing | YouTube Data API v3 (OAuth2 resumable upload) |
| State bus | Redis 7 (pipeline run state per run_id) |
| Language | Python 3.11 |
| CI/CD | GitHub Actions → DockerHub |

---

## Repository Structure

```
openclaw/
├── terraform/
│   ├── providers.tf          # AWS, Helm, kubectl providers
│   ├── vpc.tf                # VPC data source, EKS subnets, NAT GW, S3 VPC endpoint
│   ├── eks.tf                # EKS managed cluster + light-agents node group
│   ├── karpenter.tf          # Karpenter Helm release + NodePool CRDs
│   ├── s3.tf                 # 4 media pipeline buckets + lifecycle rules
│   ├── iam.tf                # Per-agent IRSA roles (least-privilege)
│   ├── secrets.tf            # Secrets Manager — all API credentials
│   └── billing.tf            # CloudWatch billing alarms ($20 / $50)
│
├── k8s/
│   ├── namespaces.yaml
│   ├── configmap.yaml        # Pipeline config (bucket names, AWS region)
│   ├── rbac.yaml             # ServiceAccounts + IRSA annotations + ClusterRole
│   ├── orchestrator.yaml     # Orchestrator Deployment + ClusterIP Service
│   ├── pipeline-cronjob.yaml
│   ├── scriptwriter-job.yaml
│   ├── avatar-director-job.yaml
│   ├── video-editor-job.yaml # Heavy job — c5.2xlarge, tainted NodePool
│   ├── seo-publisher-job.yaml
│   ├── redis.yaml
│   ├── network-policy.yaml
│   └── quota.yaml
│
├── brain/
│   ├── main.py               # Flask server + K8s Job watcher
│   ├── requirements.txt
│   └── Dockerfile
│
└── agent/
    ├── agent.py              # Dispatcher (routes ROLE to skill module)
    ├── scriptwriter.py       # Phase 1: RSS fetch + GPT-4o script
    ├── avatar_director.py    # Phase 2: HeyGen API + S3 upload
    ├── video_editor.py       # Phase 3: FFmpeg compositing pipeline
    ├── seo_publisher.py      # Phase 4: YouTube Data API v3 upload
    ├── requirements.txt
    ├── Dockerfile            # Standard agents (python:3.11-slim)
    └── Dockerfile.video_editor  # Video editor (python:3.11-slim + FFmpeg 6)
```

---

## Prerequisites

- AWS CLI configured with sufficient IAM permissions
- Terraform >= 1.6
- `kubectl` and `helm`
- DockerHub account (or substitute your own registry)
- API credentials: OpenAI, HeyGen, YouTube OAuth2 (client ID + secret + refresh token)

---

## Deployment

### Step 1 — Provision infrastructure

```bash
cd terraform

# Phase 1: EKS cluster must exist before Helm/kubectl providers can authenticate
terraform init
terraform apply -target=module.eks -target=module.karpenter \
  -var="openai_api_key=sk-..."             \
  -var="heygen_api_key=..."                \
  -var="youtube_client_id=..."             \
  -var="youtube_client_secret=..."         \
  -var="youtube_refresh_token=..."         \
  -var="dockerhub_token=..."               \
  -var="billing_alert_email=you@example.com" \
  -var="heygen_renewal_date=2027-01-01"

# Phase 2: Karpenter NodePools, S3, IAM, Secrets, Billing alarms
terraform apply  # (same -var flags)
```

Sensitive variables can be stored in a gitignored `secrets.auto.tfvars` file instead.

### Step 2 — Configure kubectl

```bash
# Run the command printed by terraform output configure_kubectl:
aws eks update-kubeconfig --region eu-north-1 --name yt-factory
```

### Step 3 — Apply Kubernetes manifests

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# One-time: DockerHub pull secret
kubectl create secret docker-registry dockerhub-secret \
  --docker-username=<USERNAME> --docker-password=<TOKEN>

# Substitute AWS Account ID into IRSA annotations and ConfigMap bucket names
envsubst < k8s/rbac.yaml      | kubectl apply -f -
envsubst < k8s/configmap.yaml | kubectl apply -f -

kubectl apply -f k8s/namespaces.yaml
kubectl apply -f k8s/quota.yaml
kubectl apply -f k8s/redis.yaml
kubectl apply -f k8s/network-policy.yaml
kubectl apply -f k8s/orchestrator.yaml
kubectl apply -f k8s/pipeline-cronjob.yaml
```

### Step 4 — Trigger a manual run

```bash
# Fire the pipeline immediately
kubectl run trigger --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s -X POST http://orchestrator-service:8080/run \
       -H "Content-Type: application/json" \
       -d '{"source":"manual"}'

# Poll status (replace RUN_ID with value returned above)
kubectl exec deploy/yt-factory-orchestrator -- \
  curl -s http://localhost:8080/status/<RUN_ID>
```

---

## Pipeline Stages

| Stage | Agent | Node type | Est. duration |
|-------|-------|-----------|--------------|
| 1 — Scriptwriter | RSS + GPT-4o | Spot t3.medium | ~2 min |
| 2 — Avatar Director | HeyGen render | Spot t3.medium | 10–30 min |
| 3 — Video Editor | FFmpeg 1080p | On-demand c5.2xlarge | 5–15 min |
| 4 — SEO Publisher | GPT-4o + YT upload | Spot t3.medium | 5–20 min |

The c5.2xlarge node is provisioned by Karpenter only for Phase 3 and
terminated within ~90 seconds of the FFmpeg job completing.

---

## FinOps Controls

| Control | Implementation |
|---------|---------------|
| Scale-to-zero rendering | Karpenter `consolidateAfter: 30s` on video-editor NodePool |
| Billing alarms | CloudWatch alarms at $20 (warning) and $50 (critical) via SNS email |
| No auto-renewals | Subscription renewal dates stored in Secrets Manager as reminder keys |
| S3 lifecycle rules | Raw video: 7 days · Final video: 30 days · Scripts: 90 days |
| Spot instances | Light-agents NodePool uses Spot t3.medium (~70% discount vs on-demand) |

---

## CI/CD

Push to `main` triggers GitHub Actions to build and push two Docker images:

| Image | Dockerfile | Used by stages |
|-------|-----------|---------------|
| `giladi17/yt-factory-agent:latest` | `agent/Dockerfile` | 1, 2, 4 |
| `giladi17/yt-factory-video-editor:latest` | `agent/Dockerfile.video_editor` | 3 |

---

## Security

- All API keys in **AWS Secrets Manager** — agents read them at runtime via IRSA
- **IRSA** (IAM Roles for Service Accounts) — no long-lived credentials on pods
- Each agent IAM role scoped to **least-privilege S3 access** only
- **NetworkPolicy** default-deny in both namespaces; egress whitelisted per agent
- Video Editor namespace fully isolated — no Redis, no external AI APIs

---

## License

MIT
