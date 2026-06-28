#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="vellum-agent-500723"
REGION="us-central1"
SERVICE="vellum"
REPO="vellum-repo"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}"
SUBSCRIPTION="vellum-gmail-sub"

echo "==> Configuring Docker auth for Artifact Registry"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "==> Creating Artifact Registry repository (idempotent)"
gcloud artifacts repositories create "${REPO}" \
  --repository-format=docker \
  --location="${REGION}" \
  --project="${PROJECT_ID}" 2>/dev/null || true

echo "==> Building image"
docker build --platform linux/amd64 -t "${IMAGE}:latest" .

echo "==> Pushing image"
docker push "${IMAGE}:latest"

echo "==> Storing secrets in Secret Manager (idempotent)"
gcloud secrets describe vellum-credentials --project="${PROJECT_ID}" &>/dev/null || \
  gcloud secrets create vellum-credentials --project="${PROJECT_ID}" \
    --replication-policy=automatic
gcloud secrets versions add vellum-credentials \
  --project="${PROJECT_ID}" --data-file=credentials.json

gcloud secrets describe vellum-token --project="${PROJECT_ID}" &>/dev/null || \
  gcloud secrets create vellum-token --project="${PROJECT_ID}" \
    --replication-policy=automatic
gcloud secrets versions add vellum-token \
  --project="${PROJECT_ID}" --data-file=token.json

echo "==> Deploying to Cloud Run"
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}:latest" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --platform=managed \
  --allow-unauthenticated \
  --execution-environment=gen2 \
  --port=8080 \
  --memory=2Gi \
  --cpu=2 \
  --timeout=900 \
  --concurrency=2 \
  --min-instances=1 \
  --max-instances=2 \
  --no-cpu-throttling \
  --set-secrets="/secrets/credentials/credentials.json=vellum-credentials:latest,/secrets/token/token.json=vellum-token:latest" \
  --set-env-vars="GMAIL_CREDENTIALS_PATH=/secrets/credentials/credentials.json,GMAIL_TOKEN_PATH=/secrets/token/token.json,$(grep -v '^#' .local.env | grep '=' | grep -v 'GMAIL_CREDENTIALS_PATH\|GMAIL_TOKEN_PATH\|^PORT=' | tr '\n' ',' | sed 's/,$//')" \
  --quiet

echo "==> Fetching Cloud Run URL"
SERVICE_URL=$(gcloud run services describe "${SERVICE}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(status.url)")
echo "Service URL: ${SERVICE_URL}"

echo "==> Updating Pub/Sub subscription endpoint"
gcloud pubsub subscriptions modify-push-config "${SUBSCRIPTION}" \
  --project="${PROJECT_ID}" \
  --push-endpoint="${SERVICE_URL}/gmail/webhook"

echo ""
echo "Deployed: ${SERVICE_URL}"
echo "Webhook:  ${SERVICE_URL}/gmail/webhook"
