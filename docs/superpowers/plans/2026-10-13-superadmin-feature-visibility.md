# Superadmin Feature Visibility Implementation Plan

> **For agentic workers:** Execute inline in this session; keep backend/frontend branch names coordinated and do not push.

**Goal:** Let superadmins hide or restore individual portal pages globally while preserving RBAC as the per-role access filter.

**Architecture:** The backend owns a code-defined feature catalog and persists only explicit enabled/disabled overrides in a single table. The frontend fetches that catalog once through TanStack Query and uses the same state for the superadmin settings tab, Sidebar, and route guards. Missing database overrides default to enabled, so migrations and newly added catalog entries preserve current visibility.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Pydantic; Next.js 16 App Router, React 19, TanStack Query, Vitest.

---

## Task 1: Backend feature visibility API

**Files:**
- Create `qa-hub-backend/app/core/features.py` with the stable keys, labels, routes, RBAC actions, and `enabled_by_default=True` for each sidebar page, including Metriche and API Docs.
- Create `qa-hub-backend/app/models/feature_visibility.py` for one persisted override per feature key.
- Modify `qa-hub-backend/app/models/__init__.py` to register the model for Alembic discovery.
- Create `qa-hub-backend/app/schemas/feature_visibility.py` for catalog response and boolean update payloads.
- Create `qa-hub-backend/app/services/feature_visibility.py` to merge code catalog with persisted overrides and validate updates against known feature keys.
- Create `qa-hub-backend/app/api/v1/features.py` with `GET /api/v1/features` and `PATCH /api/v1/features/{feature_key}`.
- Modify `qa-hub-backend/app/main.py` to register the router.
- Create `qa-hub-backend/alembic/versions/0015_feature_visibility.py` with a `feature_visibility` table; keep it empty at migration so all catalog defaults resolve to enabled.
- Modify `qa-hub-backend/README.md` and regenerate `qa-hub-backend/openapi/qa-hub-be.json` with `python -m scripts.generate_openapi`.
- Create `qa-hub-backend/tests/test_feature_visibility_service.py` and `qa-hub-backend/tests/test_feature_visibility_api.py`.

- [ ] Write tests first for: all catalog entries default enabled without rows, including `settings_gherkin`; a stored false override is returned; valid updates persist; unknown keys are rejected; GET/PATCH API contracts and unknown-key error handling.
- [ ] Run `cd qa-hub-backend && .venv/bin/pytest tests/test_feature_visibility_service.py tests/test_feature_visibility_api.py -q` and confirm expected RED failures.
- [ ] Implement catalog, model, schema, service, router, registration, and migration with missing overrides interpreted as true.
- [ ] Re-run the two test files and confirm GREEN.

## Task 2: Shared frontend feature state and admin controls

**Files:**
- Modify `qa-hub-frontend/src/types/index.ts` with the response types.
- Create `qa-hub-frontend/src/hooks/useFeatureVisibility.ts` for the shared query and update mutation; invalidate `feature-visibility` after updates.
- Create `qa-hub-frontend/src/app/(portal)/settings/users/FeatureVisibilityTab.tsx` for the page-by-page toggle list and pending/error states.
- Modify `qa-hub-frontend/src/app/(portal)/settings/users/page.tsx` to add the **Funzionalità** tab, visible under its existing superadmin guard.
- Create `qa-hub-frontend/src/app/(portal)/settings/users/FeatureVisibilityTab.test.tsx`.

- [ ] Write the failing tab test for catalog rendering, toggling a feature, and visible mutation failure/pending state.
- [ ] Run `cd qa-hub-frontend && pnpm test -- FeatureVisibilityTab.test.tsx` and confirm RED.
- [ ] Implement the query, update mutation, and tab using existing Settings Users and TanStack Query patterns.
- [ ] Re-run the focused test and confirm GREEN.

## Task 3: Sidebar and route gating

**Files:**
- Create `qa-hub-frontend/src/lib/features.ts` for route-to-feature matching, including exact root matching and longest-prefix matching for nested routes.
- Modify `qa-hub-frontend/src/components/shell/Sidebar.tsx` so each toggleable item requires both its existing RBAC action and its feature enabled flag; keep admin navigation outside the feature catalog.
- Modify `qa-hub-frontend/src/components/auth/RouteGuard.tsx` to check feature visibility in addition to the existing RBAC action and render a neutral unavailable state for globally disabled pages without redirecting.
- Create `qa-hub-frontend/src/components/auth/RouteGuard.test.tsx` and `qa-hub-frontend/src/components/shell/Sidebar.test.tsx`.

- [ ] Write failing tests for: enabled feature plus allowed RBAC renders; disabled feature blocks even superadmin; enabled feature with denied RBAC remains blocked; each of the two Data Hub and Data Quality leaf routes can be toggled independently; Settings Gherkin routes map to their toggle; disabled items disappear from the Sidebar; API Docs link follows its toggle.
- [ ] Run `cd qa-hub-frontend && pnpm test -- RouteGuard.test.tsx Sidebar.test.tsx` and confirm RED.
- [ ] Implement one shared feature-state query used by route guards and Sidebar; avoid changing or merging `roles.permissions`.
- [ ] Re-run focused tests and confirm GREEN.

## Task 4: Integration checks and branch hygiene

- [ ] Run backend feature tests plus existing role/RBAC tests: `cd qa-hub-backend && .venv/bin/pytest tests/test_feature_visibility_service.py tests/test_feature_visibility_api.py tests/test_roles_api.py -q`.
- [ ] Run frontend feature tests plus the existing SANP page tests: `cd qa-hub-frontend && pnpm test -- FeatureVisibilityTab.test.tsx RouteGuard.test.tsx Sidebar.test.tsx 'src/app/(portal)/sanp-health/page.test.tsx'`.
- [ ] Run backend lint on changed Python files and frontend lint/typecheck using existing package scripts.
- [ ] Run `git diff --check` in both repositories, verify each branch is based on its fetched `origin/main`, and leave both branches local and unpushed.
