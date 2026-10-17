# QuotTracker

Quotation and request tracking service with spreadsheet import, PDF extraction,
AI search, and Excel export. The application does not create sample records or
demo spreadsheets.

## Configuration

Copy `.env.example` to `.env` for local use and replace both secret placeholders.
For cloud deployment, set the values in the platform's secret manager instead.
Do not commit `.env`, database files, uploads, or customer documents.

Required:

- `ADMIN_USERNAME`: the shared administrator login.
- `ADMIN_PASSWORD`: a unique password of at least 14 characters.
- `SESSION_SECRET`: a cryptographically random value of at least 32 characters.
- `APP_ENV=production` and `COOKIE_SECURE=true` for a public HTTPS deployment.
- `ALLOWED_HOSTS`: comma-separated exact public hostnames; do not use `*`.

`GROQ_API_KEY` is required for AI Search and PDF extraction only. These features
send the search query/record fields or extracted PDF text to Groq. If AI is
unavailable or returns invalid data, the request fails explicitly; it never
silently switches to rule-based output. Spreadsheet sync uses deterministic
field mapping and does not call the AI service.

`DB_URL` is optional and defaults to `quottracker.db` in this project folder.
Use a persistent, access-controlled disk for cloud deployment and back it up
with the application's write activity stopped.

## Local startup

Install dependencies and initialize an empty schema:

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Set ADMIN_PASSWORD and SESSION_SECRET in .env before starting the app.
python setup_db.py
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

The setup command creates tables only. It never seeds, imports, or overwrites
records. Open `http://127.0.0.1:8000` and sign in using the configured admin
credentials. Upload a spreadsheet or quotation PDF to add records.

## Public cloud deployment requirements

- Terminate TLS at a managed HTTPS load balancer/reverse proxy; do not expose
  the Uvicorn port directly to the internet.
- Set `ALLOWED_HOSTS` to the deployment's exact domain names.
- Set all required values through the cloud secret manager. Never use the
  example password or a committed environment file.
- Keep the SQLite database on persistent local block storage. This build is
  intended for one application instance and one Uvicorn worker. Do not use
  ephemeral/container storage or a network filesystem for the SQLite file.
- The login throttling state is process-local. For multiple workers or replicas,
  replace SQLite with a managed database and use shared rate-limit storage
  before scaling out.
- Configure platform-level request-size limits, monitoring, alerting, backups,
  and restore drills. The application rejects uploads larger than 10 MB.
- Rotate any Groq credential that may have been exposed, then set the new value
  only in the secret manager.

Run without `--reload` in production, bind the app to the container interface,
and trust forwarded headers only from the known proxy addresses, for example:

```sh
uvicorn api:app --host 0.0.0.0 --port 8000 --workers 1 \
  --proxy-headers --forwarded-allow-ips=127.0.0.1
```

Replace the trusted proxy address with the actual address(es) used by the
platform. `run.bat` is a local Windows launcher, not a cloud deployment
command.

## Tests

```powershell
python -m pip install -r test-requirements.txt
python -m pytest
```

The health endpoint is `GET /api/health`. It reports service/database
availability without exposing record counts or other tracker data.
