# Deploy to Google Cloud (Cloud Run + Cloud SQL)

The managed setup from CLAUDE.md Â§3: one image, two Cloud Run services, Postgres
on Cloud SQL, secrets in Secret Manager. No Docker needed locally (Cloud Build
builds the image). Rough cost at low traffic: $35â€“50/month, mostly the
always-on worker and the database. Check current prices for your region first.

Commands are for PowerShell; in bash, replace `$env:X` with `$X` and the
backtick line breaks with `\`.

## 0. Variables

```powershell
$env:PROJECT = "your-project-id"            # your GCP project id
$env:REGION  = "me-central1"                # Doha. Dammam (me-central2) needs Google sales approval per account
$env:SQL     = "agent-db"
$env:IMAGE   = "$env:REGION-docker.pkg.dev/$env:PROJECT/agent/whatsapp-agent"
$env:SA      = "agent-runtime@$env:PROJECT.iam.gserviceaccount.com"
gcloud config set project $env:PROJECT
```

## 1. Project and APIs (once)

Billing must be linked to the project (console â†’ Billing), by its owner.

```powershell
gcloud services enable run.googleapis.com sqladmin.googleapis.com `
  artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com
gcloud artifacts repositories create agent --repository-format=docker --location=$env:REGION
gcloud iam service-accounts create agent-runtime --display-name="WhatsApp agent runtime"
foreach ($role in "roles/secretmanager.secretAccessor", "roles/cloudsql.client") {
  gcloud projects add-iam-policy-binding $env:PROJECT --member="serviceAccount:$env:SA" --role=$role --condition=None
}
```

## 2. Database (once, ~10 minutes)

```powershell
gcloud sql instances create $env:SQL --database-version=POSTGRES_16 --edition=ENTERPRISE `
  --tier=db-f1-micro --region=$env:REGION --storage-size=10 --backup-start-time=01:00
gcloud sql databases create wa_agent --instance=$env:SQL
gcloud sql users create agent --instance=$env:SQL --password=<generated, never typed in chat>
```

`DATABASE_URL` for Cloud Run (unix socket via the Cloud SQL connector):

```
postgresql+psycopg://agent:<password>@/wa_agent?host=/cloudsql/<PROJECT>:<REGION>:<SQL>
```

## 3. Secrets (once; rotate with `versions add`)

One secret per key in CLAUDE.md Â§8. Create from a file or stdin so values never
appear in shell history:

```powershell
# e.g. for each of: WHATSAPP_ACCESS_TOKEN WHATSAPP_APP_SECRET WEBHOOK_VERIFY_TOKEN
#                   LLM_API_KEY CALENDAR_CREDENTIALS DATABASE_URL INBOX_AUTH_SECRET
gcloud secrets create LLM_API_KEY --data-file=<file containing only the value>
gcloud secrets versions add LLM_API_KEY --data-file=<file>     # rotation
```

`CALENDAR_CREDENTIALS` = the service-account key JSON file itself.

## 4. Build and migrate (every release)

```powershell
$env:TAG = (git rev-parse --short HEAD)
gcloud builds submit --config deploy/gcp/cloudbuild.yaml --substitutions "_IMAGE=$env:IMAGE`:$env:TAG" .
```

`gcloud builds submit` uploads the folder minus `.gitignore`d files, so `.env`
never leaves the machine.

Non-secret settings: copy `deploy/gcp/env.example.yaml` to `deploy/gcp/env.yaml` (git-ignored) and fill it in;
see the ConfigMap keys in CLAUDE.md Â§8.

```powershell
$secrets = "WHATSAPP_ACCESS_TOKEN=WHATSAPP_ACCESS_TOKEN:latest,WHATSAPP_APP_SECRET=WHATSAPP_APP_SECRET:latest,WEBHOOK_VERIFY_TOKEN=WEBHOOK_VERIFY_TOKEN:latest,LLM_API_KEY=LLM_API_KEY:latest,CALENDAR_CREDENTIALS=CALENDAR_CREDENTIALS:latest,DATABASE_URL=DATABASE_URL:latest,INBOX_AUTH_SECRET=INBOX_AUTH_SECRET:latest"
$sql = "$env:PROJECT`:$env:REGION`:$env:SQL"

gcloud run jobs deploy agent-migrate --image "$env:IMAGE`:$env:TAG" --region $env:REGION `
  --service-account $env:SA --set-cloudsql-instances $sql `
  --env-vars-file deploy/gcp/env.yaml --set-secrets $secrets `
  --command python --args "-m,alembic,upgrade,head"
gcloud run jobs execute agent-migrate --region $env:REGION --wait
```

## 5. Services (every release)

```powershell
# API: webhook + /inbox. Public, scales to zero when idle.
gcloud run deploy agent-api --image "$env:IMAGE`:$env:TAG" --region $env:REGION `
  --service-account $env:SA --set-cloudsql-instances $sql --allow-unauthenticated `
  --env-vars-file deploy/gcp/env.yaml --set-secrets $secrets `
  --update-env-vars FORWARDED_ALLOW_IPS=* --args api `
  --min-instances 0 --max-instances 2 --memory 512Mi

# Worker: exactly one, CPU always on (it polls the queue). Not public.
gcloud run deploy agent-worker --image "$env:IMAGE`:$env:TAG" --region $env:REGION `
  --service-account $env:SA --set-cloudsql-instances $sql --no-allow-unauthenticated `
  --env-vars-file deploy/gcp/env.yaml --set-secrets $secrets --args worker `
  --min-instances 1 --max-instances 1 --no-cpu-throttling --memory 512Mi
```

The worker listens on `$PORT` only for Cloud Run's health check
(`app/worker/health.py`); all its work is the job loop.

## 5b. Low-cost mode (demos, very low traffic)

For a demo budget, run the worker with zero idle instances instead of always on:

```powershell
gcloud run deploy agent-worker ... --allow-unauthenticated `
  --min-instances 0 --max-instances 1 --no-cpu-throttling --args worker
```

Then set `WORKER_WAKE_URL` on `agent-api` to the worker's URL. After storing a
message, the API pings it; Cloud Run starts an instance, the job loop answers,
and the instance stops after ~15 idle minutes. The first reply after a quiet
period takes a few seconds longer (cold start). The worker URL only answers `ok`.
Also stop the database between sessions:

```powershell
gcloud sql instances patch $env:SQL --activation-policy=NEVER    # stop (storage still billed)
gcloud sql instances patch $env:SQL --activation-policy=ALWAYS   # start
```

Use the always-on worker for clients: replies are instant and there's no wake step.

## 6. Meta webhook

Callback URL `https://<agent-api URL>/webhook`, verify token =
`WEBHOOK_VERIFY_TOKEN`, subscribe to `messages`, app subscribed to the WABA.

## Checks

```powershell
curl.exe -s https://<api>/healthz                                     # {"status":"ok"}
curl.exe -s "https://<api>/webhook?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=42"  # 42
gcloud run services logs read agent-worker --region $env:REGION --limit 20
```
