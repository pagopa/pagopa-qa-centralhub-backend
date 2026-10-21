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

## Celery queue isolation

The API, worker and beat values set the same `CELERY_QUEUE` within each environment:

| Environment | Queue / exchange / routing key |
|---|---|
| dev | `qa-centralhub-dev` |
| uat | `qa-centralhub-uat` |
| prod | `qa-centralhub-prod` |

The worker command intentionally has no `-Q`: Celery consumes its configured default
queue, derived from `CELERY_QUEUE`. API publications and beat schedules use that
same default. If overriding the variable, update all three releases together.
This separates CentralHub tasks from other applications such as Superset that
consume the generic `celery` queue on a shared Redis broker.

For the initial migration, pause manual sync requests and stop CentralHub beat,
deploy the updated worker, then API and beat with matching queue names. Wait for
old API/worker pods to terminate before resuming requests. Coordinate this rollout
rather than relying on rolling updates to switch all producers/consumers at once.
Already-running tasks are not moved; allow them to finish before stopping workers.
Messages already in the old `celery` queue are not migrated automatically. Do not
purge that queue: it may contain tasks belonging to other applications. Reconcile
failed/stuck application runs individually before retrying them after the rollout.

Verify from an API pod:

```bash
kubectl --context "$CTX" -n "$NS" exec deployment/qa-centralhub-backend -- \
  celery -A app.tasks inspect active_queues --timeout=10
kubectl --context "$CTX" -n "$NS" exec deployment/qa-centralhub-backend -- \
  celery -A app.tasks inspect registered --timeout=10
```

CentralHub workers must consume only the environment-specific queue and register
`app.tasks.sync_ea.sync_ea`. Other applications may still appear in inspect responses
because broker control traffic is shared; they must not consume the CentralHub
queue. Trigger a new EA sync and correlate `ea_sync_dispatched` with
`ea_sync_task_started` using the same `task_id`.

## EA Map sync diagnostics

Celery tasks use a fresh asyncio event loop per invocation. The shared task runner
disposes the worker's SQLAlchemy connection pool before that loop closes, including
on failure. Connections can be reused within a task, but not across tasks/loops.
The API and its in-process fallback retain their existing pool lifecycle.
After deploying a fix for `Future ... attached to a different loop`, restart/roll
all worker pods and verify two consecutive syncs in the same worker process.
Restarting alone without the fix only temporarily clears the stale connections.

The API persists a `running` run before publishing the Celery task. A successful
publish does not mean that a worker received it. Follow the `run_id` from
`POST /api/v1/ea/sync` or `GET /api/v1/ea/sync-runs` across API and worker logs.

| Event | Meaning |
|---|---|
| `ea_sync_run_created` | The run was committed to the database. |
| `ea_sync_dispatch_started` / `ea_sync_dispatched` | API publishing started / broker accepted the task; includes `task_id`. |
| `ea_sync_dispatch_inprocess` | Publishing failed; the existing in-process fallback was scheduled. |
| `ea_sync_task_started` | A Celery worker started execution; includes `task_id`. |
| `ea_sync_runner_started` | Execution entered the runner (also for in-process fallback). |
| `ea_sync_phase_started` / `ea_sync_phase_completed` | A phase started / finished, with `duration_ms` on completion. |
| `ea_sync_phase_failed` | A phase raised an exception, including errors later handled as partial results. |
| `ea_sync_done` / `ea_sync_failed` | Engine completion / failure. |
| `ea_sync_task_completed` / `ea_sync_task_failed` | Task returned / raised, including failures outside the engine. |

Phases include client configuration, `load_run`, `read_state`, `collect`,
`github_census`, `github_repositories`, `github_tree`, `github_files`,
`data_contracts`, `confluence_pages`, `apply`, `commit`, and `close_clients`.
Repository tree/file events also carry `repo`. Cached repositories do not emit
tree/file events. A completed collection phase can still contain partial failures;
check `ea_sync_sources_collected` counters and the final run status.

```bash
# Use the target cluster context and namespace; commands do not change the deployment.
kubectl --context "$CTX" -n "$NS" logs deployment/qa-centralhub-backend \
  --all-pods=true --all-containers=true --since=1h --prefix=true
kubectl --context "$CTX" -n "$NS" logs deployment/qa-centralhub-worker \
  --all-pods=true --all-containers=true --since=1h --prefix=true
```

Filter both outputs by the run ID. If `dispatched` has no corresponding worker
start, investigate consumers, task registration and broker/queue configuration.
Otherwise look for the last phase started without completion, or a phase failure.
Parallel repository phases may interleave; correlate by `run_id`, `phase` and `repo`.
Logs describe phase boundaries, not a persisted heartbeat or a task timeout.

New diagnostic error fields contain the exception type and stack frame locations,
not exception messages, local variables, source contents, credentials or broker URLs.
These are structured application logs, not distributed OpenTelemetry spans.
