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

## Optional files (later P5 steps)

| File | Used by |
|---|---|
| `deepseek_api_key` | Step 42+ real LLM calls — set `DEEPSEEK_API_KEY_FILE` |
| `dashscope_api_key` | Step 42+ embedding/rerank — set `DASHSCOPE_API_KEY_FILE` |
| `session_signing_key` | Step 38+ session integrity — 32+ random bytes |

These are not required for Step 37 local development.

## Permissions

On Linux/macOS, restrict each file to owner-read only:

```bash
chmod 600 secrets/*
```
