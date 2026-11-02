# Compose secrets

This directory holds secret files mounted into Compose services.
It is excluded from Git (via `.gitignore`) and from the Docker build context
(via `.dockerignore`). Never commit real secrets here.

## Required files before `docker compose up`

Create each file with its value and no trailing newline:

### `postgres_password`

PostgreSQL superuser password for the `water_agent` database.

```bash
printf 'your-strong-postgres-password' > secrets/postgres_password
```

### `admin_password`

Initial password for the bootstrap administrator account.
Read only by the `bootstrap-admin` one-shot service via `/run/secrets/admin_password`.
Must be at least 12 characters.  After the first login you will be prompted to
change it; this file is no longer used once the password has been changed.

```bash
printf 'your-strong-admin-password' > secrets/admin_password
```

### `deepseek_api_key` and `dashscope_api_key`

LLM credentials.  **Both files must exist before `docker compose up`** — Compose
resolves every `file:` entry when it parses `compose.yml`, so a missing file
fails `docker compose config`.  An *empty* file is fine: the loader treats it as
"not configured", and the app then refuses runs with `424
WEB_MODEL_NOT_CONFIGURED` instead of pretending to answer.

```bash
touch secrets/deepseek_api_key secrets/dashscope_api_key   # placeholders are enough
printf 'sk-your-deepseek-key' > secrets/deepseek_api_key   # or fill them in
printf 'sk-your-dashscope-key' > secrets/dashscope_api_key
```

`deepseek_api_key` backs chat generation; `dashscope_api_key` backs embedding,
reranking and the vision review of text-less PDFs.

## Where a credential may come from

`water_agent/config/credentials.py` resolves each key from three places, highest
priority first:

| Priority | Source | Notes |
|---|---|---|
| 1 | The environment variable itself (`DEEPSEEK_API_KEY`) | What the CLI and the harness use |
| 2 | `{ENV}_FILE` → the files above | Mounted read-only at `/run/secrets/`, wired in `compose.yml` |
| 3 | The `app_credentials` volume | Written by the Web settings panel, read-write |

Priority 3 lives at `/var/lib/water-agent-credentials`, deliberately *outside*
`/var/lib/water-agent`: `scripts/backup.sh` archives that directory wholesale, so
a credential stored inside it would land in `user_files.tar` in plain text.
Set `WATER_AGENT_CREDENTIALS_DIR` to relocate it (tests and bare `uvicorn` runs do).

Every consumer (chat model, embedding, rerank, vision review) resolves the key
through this order, so a key written through the settings panel takes effect on
the next run without a restart and survives restarts — as long as no higher
priority source is set.  When the environment variable or a non-empty secret
file is present, it keeps winning: the panel still stores what you type, but the
status it reports is `env` / `secret_file`, and clearing the key in the panel
only removes the volume copy.  It never unsets an operator's environment
variable and never hides a secret file.

## Optional files (not yet wired)

| File | Used by |
|---|---|
| `session_signing_key` | Step 38+ session integrity — 32+ random bytes |

## Permissions

On Linux/macOS, restrict each file to owner-read only:

```bash
chmod 600 secrets/*
```
