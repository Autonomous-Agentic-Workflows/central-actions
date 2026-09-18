#!/usr/bin/env bash
# Bootstrap Autonomous-Agentic-Workflows/central-actions and the org-level GCP OIDC trust.
#
# Prerequisites: gh (authenticated as an org owner), gcloud (authenticated), jq.
# Run from the root of this repository checkout.
#
#   ORG=Autonomous-Agentic-Workflows GCP_PROJECT=my-gcp-project ./scripts/bootstrap-org.sh [--skip-gcp] [--skip-github]
#
# Everything is idempotent; re-running is safe.
set -euo pipefail

ORG="${ORG:-Autonomous-Agentic-Workflows}"
REPO="${REPO:-central-actions}"
TAG="${TAG:-v1.1.0}"
MAJOR_TAG="${MAJOR_TAG:-v1}"
GCP_PROJECT="${GCP_PROJECT:-}"
POOL="${POOL:-github-pool}"
PROVIDER="${PROVIDER:-github-provider}"
SA_NAME="${SA_NAME:-github-deployer}"
REGION="${REGION:-us-central1}"
SKIP_GCP=false; SKIP_GITHUB=false
for a in "$@"; do case "$a" in --skip-gcp) SKIP_GCP=true;; --skip-github) SKIP_GITHUB=true;; esac; done

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# ----------------------------------------------------------------------------- GitHub
if [ "$SKIP_GITHUB" = false ]; then
  log "Ensuring ${ORG}/${REPO} exists"
  if ! gh repo view "${ORG}/${REPO}" >/dev/null 2>&1; then
    # Reusable workflows must be callable by every org repo: public, or internal/private
    # with Actions access set to 'organization' (done below).
    gh repo create "${ORG}/${REPO}" --private --description "Centralized reusable workflows and composite actions for ${ORG}" --confirm
  fi

  log "Pushing ${TAG} and floating ${MAJOR_TAG}"
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/${ORG}/${REPO}.git"
  git push -u origin HEAD:main
  # Semantic tags are immutable: refuse to move an existing one (bump TAG instead).
  if git ls-remote --exit-code --tags origin "refs/tags/${TAG}" >/dev/null 2>&1; then
    echo "ERROR: ${TAG} already exists on origin. Semantic tags are immutable - set TAG=v1.x.y to a new version." >&2
    exit 1
  fi
  git tag -a "${TAG}" -m "central-actions ${TAG}"
  git push origin "${TAG}"
  # Only the floating major tag may move.
  git tag -fa "${MAJOR_TAG}" -m "central-actions ${MAJOR_TAG} -> ${TAG} (floating)"
  git push -f origin "${MAJOR_TAG}"

  log "Allowing all ${ORG} repositories to call these workflows"
  gh api -X PUT "repos/${ORG}/${REPO}/actions/permissions/access" -f access_level=organization

  log "Release ${TAG}"
  gh release view "${TAG}" --repo "${ORG}/${REPO}" >/dev/null 2>&1 \
    || gh release create "${TAG}" --repo "${ORG}/${REPO}" --title "${TAG}" --generate-notes
fi

# ----------------------------------------------------------------------------- GCP OIDC
if [ "$SKIP_GCP" = false ]; then
  [ -n "$GCP_PROJECT" ] || { echo "GCP_PROJECT is required (or pass --skip-gcp)"; exit 1; }
  PROJECT_NUMBER=$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)')
  SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"

  log "Enabling APIs on ${GCP_PROJECT}"
  gcloud services enable iamcredentials.googleapis.com sts.googleapis.com run.googleapis.com \
    cloudbuild.googleapis.com artifactregistry.googleapis.com --project "$GCP_PROJECT"

  log "Workload Identity pool/provider (trust limited to repository_owner == '${ORG}')"
  gcloud iam workload-identity-pools describe "$POOL" --location=global --project "$GCP_PROJECT" >/dev/null 2>&1 \
    || gcloud iam workload-identity-pools create "$POOL" --location=global --display-name="GitHub Actions" --project "$GCP_PROJECT"
  gcloud iam workload-identity-pools providers describe "$PROVIDER" --workload-identity-pool="$POOL" --location=global --project "$GCP_PROJECT" >/dev/null 2>&1 \
    || gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
         --workload-identity-pool="$POOL" --location=global --project "$GCP_PROJECT" \
         --issuer-uri="https://token.actions.githubusercontent.com" \
         --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
         --attribute-condition="attribute.repository_owner == '${ORG}'"

  log "Deployer service account ${SA_EMAIL}"
  gcloud iam service-accounts describe "$SA_EMAIL" --project "$GCP_PROJECT" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$SA_NAME" --display-name="GitHub Actions deployer" --project "$GCP_PROJECT"
  for role in roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.writer roles/storage.admin roles/iam.serviceAccountUser; do
    gcloud projects add-iam-policy-binding "$GCP_PROJECT" --member="serviceAccount:${SA_EMAIL}" --role="$role" --condition=None >/dev/null
  done
  # Let every repo in the org impersonate the deployer.
  gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project "$GCP_PROJECT" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository_owner/${ORG}" >/dev/null

  WIP="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"

  if [ "$SKIP_GITHUB" = false ]; then
    log "Organization secrets on ${ORG} (visible to all repositories)"
    gh secret set GCP_WORKLOAD_IDENTITY_PROVIDER --org "$ORG" --visibility all --body "$WIP"
    gh secret set GCP_SERVICE_ACCOUNT           --org "$ORG" --visibility all --body "$SA_EMAIL"
    gh secret set GCP_PROJECT_ID                --org "$ORG" --visibility all --body "$GCP_PROJECT"
  fi

  cat <<EOF

GCP_WORKLOAD_IDENTITY_PROVIDER = ${WIP}
GCP_SERVICE_ACCOUNT            = ${SA_EMAIL}
GCP_PROJECT_ID                 = ${GCP_PROJECT}
Region                         = ${REGION}
EOF
fi

log "Done. Next: add the org ruleset requiring 'ci / gate' (see docs/GOVERNANCE.md)."
