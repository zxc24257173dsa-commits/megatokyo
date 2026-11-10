#!/usr/bin/env bash
# Megatokyo ERP - one-time Google Cloud setup (run once in Cloud Shell, in the project you created).
#
# Creates, in Tokyo (asia-northeast1):
#   - PostgreSQL 16 database (Cloud SQL, smallest size, daily backups + point-in-time recovery)
#   - a private file bucket (photos, receipts, import files)
#   - generated secrets in Secret Manager (database URL, login signing key, encryption key, scheduler key)
#   - two service accounts: erp-runtime (the app) and erp-deployer (used by GitHub Actions, no keys:
#     Workload Identity Federation restricted to this one GitHub repository)
#   - a container registry
# Safe to run again: everything that already exists is kept (secrets are never regenerated).
# Prints, at the end, the values to send back (no passwords or keys are printed).
set -euo pipefail

GITHUB_REPO="${GITHUB_REPO:-zxc24257173dsa-commits/megatokyo}"
REGION="asia-northeast1"
PROJECT="$(gcloud config get-value project 2>/dev/null)"
[ -n "$PROJECT" ] || { echo "請先選擇專案：gcloud config set project <專案ID>"; exit 1; }
NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
ADMIN_EMAIL="$(gcloud config get-value account 2>/dev/null)"
SQL=megatokyo-db
BUCKET="${PROJECT}-megatokyo-files"
RUNTIME="erp-runtime@${PROJECT}.iam.gserviceaccount.com"
DEPLOYER="erp-deployer@${PROJECT}.iam.gserviceaccount.com"
say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }
exists() { "$@" >/dev/null 2>&1; }

say "1/8 開啟需要的 Google Cloud 服務（約 1 分鐘）"
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com cloudscheduler.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com storage.googleapis.com iam.googleapis.com

say "2/8 建立映像檔倉庫"
exists gcloud artifacts repositories describe megatokyo --location "$REGION" ||
  gcloud artifacts repositories create megatokyo --repository-format docker --location "$REGION"

say "3/8 建立資料庫（第一次約 10 分鐘，請稍候）"
if ! exists gcloud sql instances describe "$SQL"; then
  gcloud sql instances create "$SQL" --database-version POSTGRES_16 --edition enterprise --tier db-f1-micro \
    --region "$REGION" --storage-type SSD --storage-size 10 --storage-auto-increase \
    --backup-start-time 18:00 --enable-point-in-time-recovery --retained-backups-count 14
fi
exists gcloud sql databases describe erp --instance "$SQL" || gcloud sql databases create erp --instance "$SQL"
CONN="$(gcloud sql instances describe "$SQL" --format='value(connectionName)')"

say "4/8 產生密鑰（只在第一次產生，之後不會改）"
secret() {  # name, value-generator
  if ! exists gcloud secrets describe "$1"; then
    "$2" | gcloud secrets create "$1" --replication-policy automatic --data-file=-
  fi
}
gen32b64() { head -c 32 /dev/urandom | base64 | tr -d '\n'; }
gen48() { head -c 48 /dev/urandom | base64 | tr -d '\n/+=' | head -c 48; }
if ! exists gcloud secrets describe erp-database-url; then
  DBPW="$(gen48)"
  gcloud sql users create erp --instance "$SQL" --password "$DBPW" >/dev/null 2>&1 ||
    gcloud sql users set-password erp --instance "$SQL" --password "$DBPW"
  printf 'postgresql+psycopg://erp:%s@/erp?host=/cloudsql/%s' "$DBPW" "$CONN" |
    gcloud secrets create erp-database-url --replication-policy automatic --data-file=-
  unset DBPW
fi
secret erp-jwt-secret gen48
secret erp-data-key gen32b64
secret erp-cron-secret gen48

say "5/8 建立檔案儲存空間（不公開）"
exists gcloud storage buckets describe "gs://$BUCKET" ||
  gcloud storage buckets create "gs://$BUCKET" --location "$REGION" --uniform-bucket-level-access \
    --public-access-prevention

say "6/8 建立服務帳號與權限"
exists gcloud iam service-accounts describe "$RUNTIME" ||
  gcloud iam service-accounts create erp-runtime --display-name "Megatokyo ERP (app)"
exists gcloud iam service-accounts describe "$DEPLOYER" ||
  gcloud iam service-accounts create erp-deployer --display-name "Megatokyo ERP (GitHub deploy)"
sleep 10  # new service accounts take a moment to appear
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$RUNTIME" --role roles/cloudsql.client --condition=None >/dev/null
for s in erp-database-url erp-jwt-secret erp-data-key erp-cron-secret; do
  gcloud secrets add-iam-policy-binding "$s" --member "serviceAccount:$RUNTIME" --role roles/secretmanager.secretAccessor >/dev/null
done
gcloud secrets add-iam-policy-binding erp-cron-secret --member "serviceAccount:$DEPLOYER" --role roles/secretmanager.secretAccessor >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$RUNTIME" --role roles/storage.objectAdmin >/dev/null
for r in roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.writer roles/cloudscheduler.admin \
         roles/serviceusage.serviceUsageConsumer roles/storage.admin roles/logging.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$DEPLOYER" --role "$r" --condition=None >/dev/null
done
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME" --member "serviceAccount:$DEPLOYER" --role roles/iam.serviceAccountUser >/dev/null
# Cloud Build's own account pushes the image
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:${NUMBER}-compute@developer.gserviceaccount.com" \
  --role roles/artifactregistry.writer --condition=None >/dev/null 2>&1 || true

say "7/8 讓 GitHub（只限 $GITHUB_REPO）可以部署，不需要金鑰檔"
exists gcloud iam workload-identity-pools describe github --location global ||
  gcloud iam workload-identity-pools create github --location global --display-name "GitHub Actions"
if ! exists gcloud iam workload-identity-pools providers describe megatokyo --location global --workload-identity-pool github; then
  gcloud iam workload-identity-pools providers create-oidc megatokyo --location global --workload-identity-pool github \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition "assertion.repository == '${GITHUB_REPO}'"
fi
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${GITHUB_REPO}" >/dev/null

say "8/8 完成"
URL="https://megatokyo-erp-${NUMBER}.${REGION}.run.app"
cat <<EOF

================ 請把下面這 5 行複製，貼給 Claude ================
GCP_PROJECT=${PROJECT}
GCP_WIF_PROVIDER=projects/${NUMBER}/locations/global/workloadIdentityPools/github/providers/megatokyo
GCP_SQL_CONNECTION=${CONN}
ADMIN_EMAIL=${ADMIN_EMAIL}
APP_URL=${URL}
=================================================================
（裡面沒有任何密碼或金鑰，可以放心貼。）
EOF
