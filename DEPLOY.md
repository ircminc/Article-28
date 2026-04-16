# Deployment guide

This guide covers deploying the APG 835/837 Rate Analyzer to a HIPAA-eligible
cloud — **AWS**, **Azure**, or **GCP**. The Docker image is platform-agnostic;
the platform-specific sections below describe how to get traffic to it.

> **Scope reminder.** Phase 5 added per-user login, role-based access control,
> and audit logging. The app is *technically* ready for a hosted deployment.
> It is **not** a substitute for the organisational controls HIPAA requires
> (BAA, risk assessment, data backup policy, incident response plan, training,
> etc.). Deploy real PHI only after those are in place.

---

## 1. What you need before deploying

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

## 2. Secrets to provision

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

## 3. Build + push the image

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

## 4. First-run bootstrap (any platform)

The app needs **two one-shot tasks** to run before the backend accepts traffic:

### 4a. Load the reference data

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

### 4b. Create the first admin

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

## 5. Platform-specific runbooks

### 5a. AWS (ECS Fargate + ALB)

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

### 5b. Azure Container Apps

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

### 5c. GCP Cloud Run

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

## 6. Post-deploy verification

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
