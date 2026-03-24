#!/bin/bash
# Deploy GlimpseUI to Google Cloud Run
# Usage: ./deploy.sh [project-id] [region]

set -e

PROJECT_ID="${1:-$(gcloud config get-value project)}"
REGION="${2:-us-central1}"
SERVICE="glimpseui"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}"

echo "🚀 Deploying GlimpseUI"
echo "   Project : $PROJECT_ID"
echo "   Region  : $REGION"
echo "   Image   : $IMAGE"
echo ""

# Build & push image
echo "📦 Building container..."
gcloud builds submit --tag "$IMAGE" .

# Deploy to Cloud Run
echo "☁️  Deploying to Cloud Run..."
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --platform managed \
  --region "$REGION" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --concurrency 10 \
  --set-env-vars "OPENROUTER_MODEL=google/gemini-2.0-flash-exp:free" \
  --update-secrets "OPENROUTER_API_KEY=openrouter-api-key:latest"

echo ""
echo "✅ Deployed!"
gcloud run services describe "$SERVICE" \
  --platform managed \
  --region "$REGION" \
  --format "value(status.url)"
