#!/usr/bin/env bash
# Deploy reception-agent and bridge-agent to Google Cloud Run.
# Usage: ./deploy-cloud-run.sh YOUR_PROJECT_ID [REGION]
# Example: ./deploy-cloud-run.sh my-gcp-project asia-south1

set -euo pipefail

PROJECT="${1:?usage: $0 PROJECT_ID [REGION]}"
REGION="${2:-asia-south1}"
REPO="gcr.io/${PROJECT}"

echo "==> Project : ${PROJECT}"
echo "==> Region  : ${REGION}"
echo "==> Image repo: ${REPO}"

# ── 1. Enable required APIs ──────────────────────────────────────────────────
echo "==> Enabling Cloud Run and Container Registry APIs..."
gcloud services enable run.googleapis.com \
                        containerregistry.googleapis.com \
                        --project "${PROJECT}"

# ── 2. Authenticate Docker with GCR ─────────────────────────────────────────
echo "==> Configuring Docker auth for gcr.io..."
gcloud auth configure-docker --quiet

# ── 3. Build and push the shared image ──────────────────────────────────────
IMAGE="${REPO}/livekit-agents:latest"
echo "==> Building and pushing image: ${IMAGE}"
docker build -t "${IMAGE}" ./reception-agent
docker push "${IMAGE}"

# ── 4. Deploy reception-agent ────────────────────────────────────────────────
echo "==> Deploying reception-agent..."
gcloud run deploy reception-agent \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --min-instances 1 \
  --max-instances 3 \
  --memory 1Gi \
  --cpu 1 \
  --port 8081 \
  --timeout 3600 \
  --no-allow-unauthenticated \
  --env-vars-file cloud-run-env.yaml \
  --project "${PROJECT}"

# ── 5. Deploy bridge-agent (same image, different entrypoint) ────────────────
echo "==> Deploying bridge-agent..."
gcloud run deploy bridge-agent \
  --image "${IMAGE}" \
  --command "sh" \
  --args "-c,uv run src/bridge_agent.py start --port \${PORT:-8081}" \
  --region "${REGION}" \
  --platform managed \
  --min-instances 1 \
  --max-instances 3 \
  --memory 1Gi \
  --cpu 1 \
  --port 8081 \
  --timeout 3600 \
  --no-allow-unauthenticated \
  --env-vars-file cloud-run-env.yaml \
  --project "${PROJECT}"

echo ""
echo "Done. Both services are deployed with min-instances=1 (always on)."
echo "They connect outbound to LiveKit Cloud — no inbound URLs needed."
