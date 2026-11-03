# CivicLens

The **CivicLens Intelligent Duplicate Civic Issue Detection and Prioritization System**.

## Current architecture

- React + TypeScript + Vite citizen and administration frontend
- Firebase Authentication and Cloud Storage
- Supabase PostgreSQL with PostGIS and pgvector
- Firebase Hosting configuration
- Privacy-first pseudonymous complaint intake
- Issue registry, escalation matrix, transparency and security views
- Database migration covering complaint lifecycle, master issues, duplicate evidence,
  priority, escalation, resolution, expenditure and audit records

The frontend uses Firebase Authentication for identity and the FastAPI service for
authenticated complaint workflows, public registry data, staff operations, analytics,
notifications, and escalation views. The backend uses Supabase PostgreSQL/PostGIS
through server-only credentials.

## Local setup

```powershell
npm install
Copy-Item .env.example .env.local
npm run dev
```

For local backend development, set `VITE_API_BASE_URL=http://localhost:8000`
in `.env.local`. The example defaults to the deployed Cloud Run API.

Only browser-safe configuration belongs in Vite environment variables (`VITE_*`).
Never commit Firebase service-account files, Supabase service-role keys, Resend keys,
database passwords, or other private credentials. Server-only values must not use the
`VITE_` prefix.

## Database setup

The initial schema is in
`supabase/migrations/202611020001_initial_schema.sql`. Apply it through the Supabase
SQL Editor or the Supabase CLI after enabling the PostGIS and vector extensions.
It creates the privacy boundaries, complaint lifecycle, duplicate evidence, master
issues, priority fields, escalation history, resolution records, financial records,
audit logs, and the redacted public issue view.

After applying the initial schema, also run
`supabase/migrations/202611020002_storage.sql`. It creates the private
`complaint-evidence` Storage bucket. Evidence is uploaded by the backend with the
server-only Supabase key; it is not publicly readable.

Then run
`supabase/migrations/202611020003_public_issue_visibility.sql`. Provisional issues
remain private until they leave `under_verification`; only operational or resolved
issues appear in the public registry.

Then run
`supabase/migrations/202611020004_duplicate_candidates.sql`. It adds the private
PostGIS lookup used during intake to find nearby issues in the same category.
Candidate scores are advisory and require later human verification; no complaint
is automatically rejected. A strong same-category match within 500 meters is linked
to the existing master issue and counted as another report; weaker matches remain
potential candidates for review.

Initial priority is also calculated transparently during intake. Emergency
language is marked critical; recognized safety or access hazards increase the
priority score. This advisory score does not replace authorized human review.

Run `supabase/migrations/202611020005_dashboard_summary.sql` to enable the live
privacy-safe overview metrics. The frontend displays `--` until the summary API
is available rather than showing fabricated operational counts.

Run `supabase/migrations/202611020006_firebase_roles.sql` after configuring
Firebase. The frontend requires Firebase email/password authentication. New
accounts are citizens by default; staff roles are assigned by setting
`firebase_profiles.firebase_uid` and `firebase_profiles.role` in Supabase. The backend verifies
Firebase ID tokens using the server-only `FIREBASE_SERVICE_ACCOUNT_FILE` setting (or valid
single-line `FIREBASE_SERVICE_ACCOUNT_JSON`).
Never expose that JSON or the Supabase service-role key to the browser.

Run `supabase/migrations/202611020007_staff_workflow.sql` to enable the staff
operations queue, auditable status changes, and Firebase UID actor fields.

Run `supabase/migrations/202611020008_review_notifications.sql` to enable
duplicate review decisions, department-scoped staff profiles, private evidence
preview links, and in-app notification records.

## Backend development

The backend is a separate FastAPI service in `backend/`. Docker is not required for
local development. It requires the Supabase service-role key only on the server:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
$env:SUPABASE_URL = "https://your-project.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY = "server-only-key"
$env:CORS_ORIGINS = "http://localhost:5173,http://localhost:4173,http://localhost:4174,http://localhost:4175,http://127.0.0.1:5173,http://127.0.0.1:4173,http://127.0.0.1:4174,http://127.0.0.1:4175"
Set-Location backend
uvicorn app.main:app --reload --port 8000
```

Never put `SUPABASE_SERVICE_ROLE_KEY` or `RESEND_API_KEY` in a `VITE_*` variable.
The browser calls `POST /api/v1/complaints`; it receives the tracking token once,
while the database stores only its SHA-256 hash.

### Secret handling

Keep `backend\.env` and the Firebase Admin service-account JSON outside the project
directory or inject them through the deployment platform's secret manager. The
repository contains only `backend\.env.example`; never copy production values into
that file or into a submission archive. If a service-account JSON, Supabase
service-role key, or Resend key has been stored in a shared folder, rotate or revoke
it in the provider console before deploying again.

For Cloud Run deployment, use Google Cloud's source deployment/buildpacks instead
of maintaining a Dockerfile:

```powershell
gcloud run deploy civiclens-api `
  --source backend `
  --region asia-south1 `
  --allow-unauthenticated
```

The backend source upload excludes local environment and Firebase service-account
files through `backend/.gcloudignore`. Complaint evidence requests are limited to
30 MiB total to stay below Cloud Run's HTTP/1 request-size limit.

The backend includes `backend/Procfile` so Cloud Run starts the FastAPI application
with Uvicorn on the platform-provided `PORT`.

Cloud Run creates the runtime container internally; no local Docker installation
or Docker commands are needed.

## Build and deploy

```powershell
npm run build
firebase deploy --only hosting
```

### Progressive Web App

The frontend is installable as CivicLens through `public/manifest.webmanifest`.
The production build registers `public/sw.js`, which caches only the application
shell and static same-origin assets. Requests under `/api/` and all authenticated
or personalized responses bypass the service worker cache. When offline, navigation
falls back to `public/offline.html` rather than displaying fabricated live data.

For local PWA testing, use the production preview (service workers do not register
in Vite development mode):

```powershell
npm run build
npm run preview -- --host localhost --port 4173
```

Open `http://localhost:4173` in a browser. Use the **Install app** button when the
browser exposes installation support. On Android or Windows, use the browser's
install control if the button is not shown. On iOS, open the site in Safari,
choose **Share**, then **Add to Home Screen**. HTTPS is required in production
(localhost is trusted during local testing).

The secure backend API and deterministic scoring/evaluation modules are included in this
project. Any future asynchronous workers or heavier ML models must remain separate
server-side services and must use server-only secrets.

Each accepted complaint is now converted into a provisional master issue during
intake. The complaint is linked with a `new_issue` decision and moved to
`under_verification`; duplicate candidate scoring and human verification remain
separate processing stages.

## AI and architecture evaluation modules

The repository includes reproducible evaluation foundations under
`backend/experiments/`:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m experiments.synthetic_dataset --size 1000
..\.venv\Scripts\python.exe -m experiments.benchmark --size 1000
```

The current scoring module is a deterministic, dependency-light baseline. It
reports image, text, geographical, and combined scores with version labels and
weights through `backend/app/multimodal.py`. The image and text components are
explicitly baseline adapters (`hashed-image-baseline-v1` and
`token-overlap-baseline-v1`); they are not CLIP or Sentence Transformer
embeddings. Replace those adapters with approved model implementations before
describing production ML accuracy.

The benchmark runs the same scoring workload with 1, 2, 4, and 8 worker
configurations where supported and writes elapsed time, throughput, speedup, and
efficiency. Small workloads can be slower in parallel because process startup
overhead is measured honestly.

Migration `202611020009_ai_workflow_evidence.sql` adds storage for similarity
model versions, score weights, duplicate decision history, merge/unmerge
history, priority history, emergency review, root-cause records, escalation
notifications, and experiment results. These tables are persistence foundations;
the FastAPI and frontend integrations expose the operational workflows that are
currently implemented.

Run migrations `202611020010_operations_analytics.sql` and
`202611020011_firebase_complaint_identity.sql` in sequence after migration 009.
The former adds operational escalation fields and staff-only row-level security
policies for workflow history. The latter allows non-anonymous complaints to
identify their reporter with either a Supabase Auth UUID or a Firebase UID.

### Implemented versus planned

Implemented and verified: Firebase authentication, citizen/staff role guards,
citizen complaint intake, private evidence storage, tracking, public issue
visibility, staff status updates, department assignment, duplicate review,
notifications, PWA shell/offline behavior, deterministic multimodal scoring
evidence, synthetic dataset generation, metric calculation, and reproducible
parallel benchmark execution.

Partially implemented: duplicate detection currently uses the deterministic
baseline adapters, emergency language detection is keyword-based, and some
advanced workflow records are exposed through administrative views rather than
full standalone management screens.

Planned or production-hardening work: CLIP/Sentence Transformer model adapters,
side-by-side duplicate comparison UI, malware scanning integration for uploaded
evidence, live Supabase/Firebase integration tests, and persistence/monitoring
for benchmark results. Multilingual processing is intentionally out of scope;
the product currently supports English only.

### Local verification

The current local verification commands are:

```powershell
npm run lint
npm run build
& .\.venv\Scripts\python.exe -m pytest
```

These checks currently pass. Backend tests cover scoring, priority calculation,
evaluation metrics, reproducibility, role guards, and strict complaint payload
validation. Before production deployment, run the following against the
configured services: Firebase sign-in and token refresh, Supabase migrations and
RLS policies, complaint image upload and private signed URLs, notification
creation, every staff role workflow, CORS from the deployed frontend origin,
service-worker installation, offline navigation, and Lighthouse mobile checks.
