# Helm chart — pagopa-qa-centralhub-backend

Umbrella chart that wraps the PagoPA [`microservice-chart`](https://github.com/pagopa/aks-microservice-chart-blueprint) v7.5.0 and deploys the QA Central Hub backend on AKS.

Three Helm releases per environment, sharing the same image:

| Release | Values file | Purpose | Service/Ingress | Replicas |
|---|---|---|---|---|
| `pagopa-qa-centralhub-backend-<env>` | `values-<env>.yaml` | FastAPI API (uvicorn on `8080`) | yes | autoscaled |
| `pagopa-qa-centralhub-backend-worker-<env>` | `values-worker-<env>.yaml` | Celery worker — executes background tasks (`celery -A app.tasks worker`) | no | 1+ (scalable) |
| `pagopa-qa-centralhub-backend-beat-<env>`   | `values-beat-<env>.yaml`   | Celery Beat — enqueues scheduled tasks (`celery -A app.tasks.schedule beat`) | no | **exactly 1** |

Only `dev` is currently configured — copy and adapt for `uat`/`prod` when needed.

## Prerequisites

1. **AKS cluster** with the namespace already provisioned (value of `vars.NAMESPACE`, e.g. `qa`).
2. **ServiceAccount** for workload identity already created in the namespace and bound to a Managed Identity that has `get`/`list` on the Key Vault. The chart wires the SA via `--set microservice-chart.azure.workloadIdentityClientId=<vars.WORKLOAD_IDENTITY_ID>` injected by the workflow.
3. **Azure Key Vault** `pagopa-d-itn-qa-kv` (dev) populated with the secrets listed in `envSecret:` of `values-dev.yaml`:
   - `qa-centralhub-database-url` — full async DSN, e.g. `postgresql+asyncpg://qachub:changeme@localhost:5432/qachub?ssl=require`
   - `qa-centralhub-redis-url` — e.g. `rediss://:<key>@<cache>.redis.cache.windows.net:6380/0`
   - `qa-centralhub-jwt-secret` — random hex (`openssl rand -hex 32`)
   - `qa-centralhub-encryption-key` — Fernet key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
   - `qa-centralhub-github-token` — PAT with read on `pagopa/pagopa-qa` Actions
   - `qa-centralhub-jira-api-token` — Atlassian API token for the user in `JIRA_EMAIL`
4. **GHCR image** `ghcr.io/pagopa/pagopa-qa-centralhub-backend:<version>` built and pushed by the `04_release_and_deploy.yml` workflow. AKS nodes pull it from GHCR (public image — no `imagePullSecret` required, as per PagoPA convention).

## Local commands

```bash
# Pull chart dependencies (downloads microservice-chart 7.5.0)
helm dependency build ./helm

# Dry-run render
helm template pagopa-qa-centralhub-backend-dev ./helm \
  -f helm/values-dev.yaml \
  --set microservice-chart.image.tag=0.1.0 \
  -n qa

# Install / upgrade backend (replace <tag> with the GHCR tag you want)
helm upgrade --install pagopa-qa-centralhub-backend-dev ./helm \
  -n qa \
  -f helm/values-dev.yaml \
  --set microservice-chart.image.tag=<tag> \
  --set microservice-chart.azure.workloadIdentityClientId=<workload-identity-client-id> \
  --atomic --timeout 10m

# Same for worker and beat (different values file + release name)
helm upgrade --install pagopa-qa-centralhub-backend-worker-dev ./helm \
  -n qa -f helm/values-worker-dev.yaml \
  --set microservice-chart.image.tag=<tag> \
  --set microservice-chart.azure.workloadIdentityClientId=<workload-identity-client-id> \
  --atomic --timeout 10m

helm upgrade --install pagopa-qa-centralhub-backend-beat-dev ./helm \
  -n qa -f helm/values-beat-dev.yaml \
  --set microservice-chart.image.tag=<tag> \
  --set microservice-chart.azure.workloadIdentityClientId=<workload-identity-client-id> \
  --atomic --timeout 10m
```

## Database migrations

Alembic migrations are **not** run automatically. After a deploy that introduces new migrations, run:

```bash
kubectl -n qa exec deploy/qa-centralhub-backend -- alembic upgrade head
```

(Or wire it as a Helm pre-install/upgrade Job in a follow-up if you want it automated.)

## CI/CD

The pipeline follows the PagoPA standard pattern and is split in two files:

- **`.github/workflows/04_release_and_deploy.yml`** — semver bump via PR labels (`patch`/`minor`/`major`/`skip`), tag/release via `pagopa/github-actions-template/maven-release`, then build+push image to GHCR via `pagopa/github-actions-template/ghcr-build-push`, then invokes the reusable deploy workflow.
- **`.github/workflows/04_deploy_with_github_runner.yml`** — runs on the per-env self-hosted runner (`[self-hosted-job, <env>]`), authenticates to Azure via OIDC, fetches AKS credentials, and runs three `helm upgrade --install` (backend / worker / beat).

### Required GitHub Environment configuration

For each environment (`dev`, `uat`, `prod`):

**Secrets**:
- `CLIENT_ID` — federated identity client id (app registration trusted by your GH repo/env)
- `TENANT_ID`
- `SUBSCRIPTION_ID`
- `BOT_TOKEN_GITHUB` — PAT used by the release action to push tags (repo-level)
- `SLACK_WEBHOOK_URL_DEPLOY` — optional, only for prod notifications

**Variables** (set under *Environment variables*, not secrets):
- `NAMESPACE` — AKS namespace, e.g. `qa`
- `CLUSTER_NAME` — AKS cluster name
- `CLUSTER_RESOURCE_GROUP` — AKS resource group
- `WORKLOAD_IDENTITY_ID` — client id of the Managed Identity bound to the SA used by the pods (for Key Vault access)

### Triggers

- PR merged on `main` → auto release (bump from PR labels) + deploy to `dev`.
- `workflow_dispatch` from Actions UI → pick env, semver mode, beta flag.
- `merge_group` → CI dry-run on the merge queue.
