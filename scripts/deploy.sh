#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="vellum-agent-500809"
REGION="us-central1"
SERVICE="vellum"
REPO="vellum-repo"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}"
SUBSCRIPTION="vellum-gmail-sub"
GCS_BUCKET=$(grep -E '^GCS_BUCKET=' .local.env | cut -d= -f2- || true)

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

echo "==> Granting Cloud Run access to secrets"
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
RUNTIME_SERVICE_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud secrets add-iam-policy-binding vellum-credentials \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet
gcloud secrets add-iam-policy-binding vellum-token \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet

if [[ -n "${GCS_BUCKET}" ]]; then
  echo "==> Configuring delivery bucket"
  gcloud storage buckets describe "gs://${GCS_BUCKET}" --project="${PROJECT_ID}" &>/dev/null || \
    gcloud storage buckets create "gs://${GCS_BUCKET}" \
      --project="${PROJECT_ID}" \
      --location="${REGION}" \
      --uniform-bucket-level-access
  gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" \
    --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
    --role="roles/storage.objectAdmin" \
    --quiet
  gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" \
    --member="allUsers" \
    --role="roles/storage.objectViewer" \
    --quiet
  gcloud storage cp assets/vellum_email_header.png \
    "gs://${GCS_BUCKET}/assets/vellum_email_header.png" \
    --content-type=image/png
  gcloud storage cp assets/vellum_email_header_dark.png \
    "gs://${GCS_BUCKET}/assets/vellum_email_header_dark.png" \
    --content-type=image/png
fi

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
  --set-env-vars="GMAIL_CREDENTIALS_PATH=/secrets/credentials/credentials.json,GMAIL_TOKEN_PATH=/secrets/token/token.json,$(grep -v '^#' .local.env | grep '=' | grep -v 'GMAIL_CREDENTIALS_PATH\|GMAIL_TOKEN_PATH\|^PORT=\|^SMTP_' | tr '\n' ',' | sed 's/,$//')" \
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
