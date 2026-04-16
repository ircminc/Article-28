# Deployment guide

Two deployment paths are supported:

1. **Team testing on GitHub Codespaces** — for immediate sharing with team
   members using synthetic data. Zero cloud setup. See [§1 Codespaces](#1-team-testing-on-github-codespaces).
2. **Production on a HIPAA-eligible cloud** (AWS / Azure / GCP) — for real
   operation. See [§3 Azure](#3-azure-container-apps-production) through §5
   for platform-specific runbooks.

The same Docker images run in both environments. Migrating from Codespaces
(test) to Azure (prod) changes no application code — only the hosting
infrastructure and the secrets.

> **Scope reminder.** Phase 5 added per-user login, role-based access control,
> and audit logging. The app is *technically* ready for a hosted deployment.
> It is **not** a substitute for the organisational controls HIPAA requires
> (BAA, risk assessment, data backup policy, incident response plan, training,
> etc.). Deploy real PHI only after those are in place.

---

## 1. Team testing on GitHub Codespaces

Codespaces is GitHub's cloud dev environment — it runs your repo inside a Linux
VM with ports forwarded to public URLs. This is the fastest way to give
teammates a live URL to click for testing.

**What it's good for:** demos, synthetic-data evaluation, collecting early
feedback before committing to cloud infrastructure.

**What it's NOT good for:** production, real PHI, or any long-lived
"come-to-this-URL-forever" deployment. Codespaces are ephemeral — they suspend
after 30 min of inactivity and the forwarded URL is tied to that specific
Codespace instance.

### 1a. One-time setup (repo owner, ~3 minutes)

Go to your repository on GitHub, then:

1. **Set the three Codespaces secrets.** Open
   **Settings → Secrets and variables → Codespaces → New repository secret**
   and create:

   | Name                   | Value                                                                   |
   |------------------------|-------------------------------------------------------------------------|
   | `APP_JWT_SECRET`       | Run locally: `python -c "import secrets; print(secrets.token_urlsafe(48))"` and paste the output |
   | `APP_ADMIN_USERNAME`   | e.g. `admin`                                                            |
   | `APP_ADMIN_PASSWORD`   | **Your chosen initial admin password** (min 10 chars)                   |

   These become environment variables inside every Codespace launched from
   this repo. They never appear in git history.

2. **(Optional) Enable public port forwarding defaults.** Settings → Codespaces
   → Port forwarding visibility → allow **Private** or **Public**. Team
   members will set individual ports to "Public" when they want to share.

### 1b. Launching a Codespace (anyone with repo access)

On the repo's main page:

1. Click the green **`<> Code`** button
2. Switch to the **Codespaces** tab
3. Click **Create codespace on phase-6-deployment** (or whichever branch is current)
4. Wait ~2 minutes while the devcontainer builds. The bottom terminal shows progress:
   - Installing Python + Node dependencies
   - Seeding synthetic reference data
   - Creating the admin user from the Codespaces secrets
   - Prints a success banner when ready

### 1c. Starting the app

In the Codespaces terminal:

```bash
bash .devcontainer/start-dev.sh
```

You'll see interleaved `[backend]` + `[frontend]` logs. Ports 8000 + 3000
auto-forward. Look at the **Ports** panel (bottom tray of VS Code in the
browser) — the frontend at port 3000 will open automatically in a new tab.

### 1d. Sharing with teammates

In the Ports panel:

1. Right-click port **3000** → **Port Visibility → Public**
2. Right-click port **8000** → **Port Visibility → Public**
3. Click the 🌐 icon next to port 3000 — copy the `https://<...>-3000.app.github.dev` URL
4. Send that URL to your teammate

Your teammate signs in with credentials **you've given them individually**:

- You sign in first with `APP_ADMIN_USERNAME` / `APP_ADMIN_PASSWORD`
- Navigate to **Users** in the sidebar (admin only)
- Click **New user** and create accounts for each teammate with role `analyst`
- Share each person's username + temporary password out-of-band (not in GitHub)
- They'll sign in, change their password from the user menu, and start using the app

### 1e. Loading real reference data (optional)

By default the Codespace seeds a *synthetic* reference dataset — ~25 HCPCS,
10 ICD-10, 23 EAPG weights, a handful of base rates. Enough for demos and UI
testing; not real NYS DOH values.

To load the real workbook in your Codespace:

1. In the file explorer, right-click the `workbooks/` folder → **Upload**
2. Choose your `Sample APG Fee Calculator.xlsx` (or equivalent)
3. In the terminal, re-run the loader:
   ```bash
   python -m backend.db.init_db --workbook workbooks/<your-file>.xlsx
   ```
4. The DB now has the real data. Existing users, audit log, and claims are preserved.

### 1f. Cost considerations

- **Free tier:** 120 core-hours/month per individual user (Pro accounts get 180).
  A 2-core machine idle 4 hours a day burns ~240 core-hours/month — right at
  the free tier edge.
- **Idle suspend:** Codespaces auto-suspend after 30 min idle. Wake-up takes ~10 sec.
- **Delete when done:** Settings → Codespaces → delete any you're not actively using.

When your team is ready for a permanent URL, move to §3 Azure.

---

---

## 2. What you need before a production cloud deployment

- [ ] A HIPAA-eligible cloud account (AWS, Azure, or GCP) with a signed BAA
- [ ] A private VPC / vnet with a private subnet for the app
- [ ] A managed certificate for TLS (ACM / Azure-managed / Google-managed)
- [ ] A container registry (ECR / ACR / Artifact Registry) or Docker Hub
- [ ] A managed secret store (Secrets Manager / Key Vault / Secret Manager)
- [ ] Encryption at rest enabled on whatever storage your SQLite DB lives on
  (EBS w/ KMS, managed disk w/ CMK, Persistent Disk w/ CMEK). For scale
  beyond a small team, migrate from SQLite to Postgres on RDS / Azure SQL /
  Cloud SQL — trivial switch, only the `DATABASE_URL` env var changes.
- [ ] A published domain with DNS pointing at the load balancer

---

### 2a. Secrets to provision

Wire these into your cloud's secret manager and inject as environment
variables on the backend container.

| Env var                 | Required? | Used by                                | Notes |
|-------------------------|-----------|----------------------------------------|-------|
| `APP_JWT_SECRET`        | YES       | backend runtime                        | 32+ random bytes. Rotating it invalidates all outstanding sessions — plan accordingly. |
| `APP_JWT_EXPIRES_MIN`   | no        | backend runtime                        | Default 480 (8h). |
| `APP_ADMIN_USERNAME`    | YES (once) | `python -m backend.db.init_admin`     | The initial admin's username. |
| `APP_ADMIN_PASSWORD`    | YES (once) | `python -m backend.db.init_admin`     | The initial admin's password. Must be 10+ chars. **Change it after first login.** |
| `APP_ADMIN_FULLNAME`    | no        | `init_admin`                           | Display name. |
| `APP_ADMIN_EMAIL`       | no        | `init_admin`                           | Display only (no email is sent). |
| `DATABASE_URL`          | YES (prod) | backend runtime                       | Defaults to local SQLite. In prod, point at Postgres: `postgresql+asyncpg://user:pass@host:5432/db`. |
| `LOG_LEVEL`             | no        | backend runtime                        | `INFO` default. |

### Generating a strong JWT secret

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
# or
openssl rand -base64 48
```

---

### 2b. Build + push the image

From the repo root:

```bash
# Build
docker build -t apg-analyzer-backend:phase-5 -f Dockerfile.backend .
docker build -t apg-analyzer-frontend:phase-5 -f Dockerfile.frontend .

# Tag for your registry and push (example: AWS ECR)
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin <ACCT>.dkr.ecr.us-east-1.amazonaws.com
docker tag apg-analyzer-backend:phase-5   <ACCT>.dkr.ecr.us-east-1.amazonaws.com/apg-analyzer-backend:phase-5
docker tag apg-analyzer-frontend:phase-5  <ACCT>.dkr.ecr.us-east-1.amazonaws.com/apg-analyzer-frontend:phase-5
docker push <ACCT>.dkr.ecr.us-east-1.amazonaws.com/apg-analyzer-backend:phase-5
docker push <ACCT>.dkr.ecr.us-east-1.amazonaws.com/apg-analyzer-frontend:phase-5
```

Equivalent flows exist for Azure ACR (`az acr login`) and GCP Artifact
Registry (`gcloud auth configure-docker`).

---

### 2c. First-run bootstrap (any platform)

The app needs **two one-shot tasks** to run before the backend accepts traffic:

#### Load the reference data

Mount the NYS DOH workbook into the container, and run:

```bash
python -m backend.db.init_db --workbook /app/workbooks/<your-workbook>.xlsx
```

If using `docker-compose.production.yml`:

```bash
WORKBOOK_DIR=/path/on/host WORKBOOK_FILENAME=sample.xlsx \
  docker compose -f docker-compose.yml -f docker-compose.production.yml \
    --profile init run --rm init-reference
```

#### Create the first admin

```bash
APP_ADMIN_USERNAME=admin \
APP_ADMIN_PASSWORD='<your-chosen-password>' \
APP_ADMIN_FULLNAME='Site Administrator' \
APP_ADMIN_EMAIL='admin@ircminc.example' \
  docker compose -f docker-compose.yml -f docker-compose.production.yml \
    --profile init run --rm init-admin
```

**Important:** the password is read from the env var *only*. Never pass it
on the command line (argv leaks into shell history + `ps`).

---

## 3. Azure Container Apps (production)

This is the **recommended path from Codespaces → production** because the app
already has a dormant GitHub Actions workflow (`.github/workflows/deploy-azure.yml`)
wired to build + push + deploy. Flipping it live is a matter of provisioning
Azure resources and adding GitHub secrets — no code changes.

### 3a. Provision Azure resources (one-time, ~15 min)

Prereqs: an Azure subscription with a signed BAA (for HIPAA eligibility).

```bash
# Variables you'll reuse
SUB_ID="<your-subscription-id>"
RG="apg-analyzer-rg"
LOC="eastus"
ACR="apganalyzer$RANDOM"          # Must be globally unique
ENV="apg-env"
BACKEND_APP="apg-backend"
FRONTEND_APP="apg-frontend"

az login
az account set --subscription "$SUB_ID"

# 1. Resource group + ACR + Container Apps environment
az group create -n "$RG" -l "$LOC"
az acr create -n "$ACR" -g "$RG" --sku Basic --admin-enabled true
az containerapp env create -n "$ENV" -g "$RG" -l "$LOC"

# 2. Service principal for GitHub Actions
az ad sp create-for-rbac --name "apg-gh-deploy" --role contributor \
  --scopes "/subscriptions/$SUB_ID/resourceGroups/$RG" --sdk-auth
# → Save this JSON output; you'll paste it as AZURE_CREDENTIALS in GitHub.

# 3. Key Vault for the JWT secret (recommended, optional for first run)
KV="apganalyzer-kv-$RANDOM"
az keyvault create -n "$KV" -g "$RG" -l "$LOC" --enable-rbac-authorization true

# 4. (Optional but recommended) Azure Files share for SQLite persistence
STORAGE="apgstorage$RANDOM"
az storage account create -n "$STORAGE" -g "$RG" --sku Standard_LRS --kind StorageV2
az storage share-rm create --storage-account "$STORAGE" -g "$RG" --name apg-data --quota 5
```

### 3b. Add GitHub secrets (one-time, ~2 min)

Settings → Secrets and variables → Actions → **New repository secret**:

| Name                              | Value                                                                |
|-----------------------------------|----------------------------------------------------------------------|
| `AZURE_CREDENTIALS`               | The full JSON output from `az ad sp create-for-rbac` above           |
| `ACR_NAME`                        | The ACR name you chose (without `.azurecr.io`)                       |
| `AZURE_RESOURCE_GROUP`            | `apg-analyzer-rg`                                                    |
| `AZURE_CONTAINER_APP_BACKEND`     | `apg-backend`                                                        |
| `AZURE_CONTAINER_APP_FRONTEND`    | `apg-frontend`                                                       |
| `AZURE_CONTAINER_APP_ENV`         | `apg-env`                                                            |
| `APP_JWT_SECRET`                  | 32+ random bytes (same generator as Codespaces)                      |
| `APP_ADMIN_USERNAME`              | `admin` (or whatever you want)                                       |
| `APP_ADMIN_PASSWORD`              | Your chosen initial admin password                                   |

### 3c. Create the Container Apps (first deploy)

The Actions workflow `deploy-azure.yml` updates existing Container Apps. For
the **first deploy**, you need the apps to exist. Run once:

```bash
# Backend
az containerapp create \
  -n "$BACKEND_APP" -g "$RG" --environment "$ENV" \
  --image "${ACR}.azurecr.io/apg-analyzer-backend:latest" \
  --target-port 8000 --ingress external \
  --registry-server "${ACR}.azurecr.io" \
  --secrets "jwt-secret=<your-jwt-secret>" "admin-pass=<your-admin-pass>" \
  --env-vars "APP_JWT_SECRET=secretref:jwt-secret" "APP_ADMIN_PASSWORD=secretref:admin-pass" \
             "APP_ADMIN_USERNAME=admin" \
  --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 3

# Frontend
az containerapp create \
  -n "$FRONTEND_APP" -g "$RG" --environment "$ENV" \
  --image "${ACR}.azurecr.io/apg-analyzer-frontend:latest" \
  --target-port 3000 --ingress external \
  --registry-server "${ACR}.azurecr.io" \
  --cpu 0.25 --memory 0.5Gi --min-replicas 1 --max-replicas 3
```

Until the first images exist in ACR, these commands fail. Two options:

1. **Trigger the workflow first** (preferred): Actions tab → **Deploy to Azure**
   → Run workflow. It'll build and push the images, the `deploy` job will fail
   because the apps don't exist yet, then you run the `az containerapp create`
   commands above.
2. **Build + push locally**: see §2b, then run the `create` commands.

### 3d. Run the workflow on every push

Subsequent deploys: go to **Actions** → **Deploy to Azure** → **Run workflow**.
Optionally enter a custom tag; defaults to the 12-char commit SHA.

Alternatively, adapt the workflow's `on:` trigger to fire on push to `main`
if you want continuous deployment. That's a one-line change in the workflow.

### 3e. Your permanent URL

After the first successful deploy:

```bash
az containerapp show -n "$FRONTEND_APP" -g "$RG" \
  --query 'properties.configuration.ingress.fqdn' -o tsv
# → apg-frontend.<random>.eastus.azurecontainerapps.io
```

Point your real domain (`apg.ircminc.example`) at this FQDN via CNAME. Azure
Container Apps will auto-provision a managed TLS certificate.

---

## 4. AWS (ECS Fargate + ALB)

### 4a. AWS (ECS Fargate + ALB)

**Architecture:**
- ECS Fargate service runs the backend container (2 tasks for HA)
- ECS Fargate service runs the frontend container (2 tasks)
- Application Load Balancer with ACM cert terminates TLS
- EFS filesystem for the SQLite DB + uploads (enable encryption at rest)
- Route 53 for DNS

**Key steps:**

1. Create an **ECR** repo per image and push the built images (see §3).
2. Create a **VPC** with two private subnets across AZs, plus two public
   subnets for the ALB.
3. Create an **EFS** filesystem. Enable encryption at rest. Create an
   access point mapping uid/gid 1000 to `/app/backend/data`.
4. Create an **ALB** with an ACM cert. Target groups for `/api/*` → backend
   task, catch-all `/` → frontend task. Set HTTPS listener (443) with
   TLS 1.2+, redirect HTTP → HTTPS.
5. Store secrets in **AWS Secrets Manager**. Reference them from the
   ECS task definition under `secrets:` so they mount as env vars.
6. Register task definitions using the platform-specific snippets below
   — container port `8000` (backend) and `3000` (frontend), EFS volume
   on the backend task, healthcheck pointing at `/api/health`.
7. Run the two init tasks (§4) using `aws ecs run-task` with
   `overrides.containerOverrides[].command` pointing at `init_db` / `init_admin`.

<details>
<summary><b>ECS task definition snippet — backend</b> (click to expand)</summary>

```json
{
  "family": "apg-analyzer-backend",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "512", "memory": "1024",
  "executionRoleArn": "arn:aws:iam::<ACCT>:role/ecsTaskExecutionRole",
  "taskRoleArn": "arn:aws:iam::<ACCT>:role/ecsAppTaskRole",
  "volumes": [{
    "name": "data",
    "efsVolumeConfiguration": {
      "fileSystemId": "fs-xxxx", "transitEncryption": "ENABLED",
      "authorizationConfig": { "accessPointId": "fsap-xxxx", "iam": "ENABLED" }
    }
  }],
  "containerDefinitions": [{
    "name": "backend",
    "image": "<ACCT>.dkr.ecr.us-east-1.amazonaws.com/apg-analyzer-backend:phase-5",
    "portMappings": [{ "containerPort": 8000, "protocol": "tcp" }],
    "mountPoints": [{ "sourceVolume": "data", "containerPath": "/app/backend/data" }],
    "environment": [
      { "name": "DATABASE_URL", "value": "sqlite+aiosqlite:///./backend/data/apg_analyzer.db" }
    ],
    "secrets": [
      { "name": "APP_JWT_SECRET", "valueFrom": "arn:aws:secretsmanager:us-east-1:<ACCT>:secret:apg/jwt-secret" }
    ],
    "healthCheck": {
      "command": ["CMD-SHELL", "curl -fsS http://127.0.0.1:8000/api/health || exit 1"],
      "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 30
    },
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/apg-analyzer-backend",
        "awslogs-region": "us-east-1",
        "awslogs-stream-prefix": "backend"
      }
    }
  }]
}
```
</details>

### (legacy) Azure Container Apps manual walk-through

**Architecture:**
- Azure Container Apps environment with two apps (backend + frontend)
- Azure File Share mounted for the SQLite DB + uploads (encryption at rest enabled by default)
- Azure Front Door or Application Gateway for TLS + routing
- Managed identity pulling secrets from Key Vault

**Key steps:**

1. Create a **Container Apps environment**, VNet-integrated for private
   egress.
2. Provision an **Azure Storage** account + File Share; mount as the
   backend's `/app/backend/data`.
3. Create **Key Vault** and store `APP_JWT_SECRET`. Enable Managed Identity
   for the backend container app and grant it Key Vault `Get` on the
   secret.
4. Push images to **ACR** (see §3).
5. Deploy backend with secret references:

   ```bash
   az containerapp create \
     --name apg-backend \
     --resource-group apg-rg \
     --environment apg-env \
     --image <ACR>.azurecr.io/apg-analyzer-backend:phase-5 \
     --target-port 8000 --ingress internal \
     --secrets jwt-secret=keyvaultref:<kv-uri>,identityref:<mi-id> \
     --env-vars APP_JWT_SECRET=secretref:jwt-secret \
     --cpu 0.5 --memory 1Gi \
     --min-replicas 2 --max-replicas 4
   ```

6. Run one-shot init tasks via `az containerapp job` (jobs API exists in
   Container Apps for exactly this).
7. Front Door + WAF for public traffic.

## 5. GCP Cloud Run

**Architecture:**
- Cloud Run service (backend) + Cloud Run service (frontend)
- Cloud SQL (Postgres) recommended — SQLite on GCE disk is fragile for
  Cloud Run's per-request containers. Flip `DATABASE_URL` to Postgres.
- Serverless VPC connector if the DB is private
- Secret Manager for `APP_JWT_SECRET`
- External HTTPS load balancer + Cloud Armor WAF

**Key steps:**

1. Provision **Cloud SQL** (Postgres 15). Enable Private IP. Enable CMEK.
2. Push images to **Artifact Registry**.
3. Create the secret: `gcloud secrets create apg-jwt-secret --data-file=-`
4. Deploy the backend:

   ```bash
   gcloud run deploy apg-backend \
     --image us-central1-docker.pkg.dev/<PROJ>/apg/apg-analyzer-backend:phase-5 \
     --region us-central1 --platform managed \
     --port 8000 --ingress internal-and-cloud-load-balancing \
     --vpc-connector apg-vpc-connector \
     --add-cloudsql-instances <PROJ>:us-central1:apg-db \
     --set-env-vars DATABASE_URL="postgresql+asyncpg://user:pass@/apg?host=/cloudsql/<PROJ>:us-central1:apg-db" \
     --set-secrets APP_JWT_SECRET=apg-jwt-secret:latest \
     --service-account apg-runtime@<PROJ>.iam.gserviceaccount.com \
     --min-instances 1 --max-instances 5
   ```

5. Run init tasks as Cloud Run jobs.
6. Front with a global external load balancer + managed cert.

---

## 6. Post-deploy verification (any cloud)

Once traffic is routed:

```bash
# 1. Health check (public)
curl https://<your-domain>/api/health

# 2. Login (should return a JWT)
curl -X POST https://<your-domain>/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<admin>","password":"<initial password>"}'

# 3. Fetch /me with the token
curl https://<your-domain>/api/auth/me \
  -H 'Authorization: Bearer <token from step 2>'
```

Then open `https://<your-domain>/` in a browser, sign in, change the admin
password immediately, and create individual analyst accounts for team members.

---

## 7. Ongoing operations

- **Rotate `APP_JWT_SECRET` quarterly** or on suspected compromise. This
  invalidates all outstanding sessions.
- **Back up the SQLite file** (or Postgres instance) daily. The audit log
  is critical evidence for HIPAA.
- **Monitor audit log** — `GET /api/admin/audit` (admin only) returns
  recent security events. Forward to your SIEM.
- **Disable departed users immediately** via Users page. The JWT they
  hold stops working on their next request (≤ 1 minute).
- **Review access quarterly** — list users, confirm each is still active
  and has the correct role.
