# Compose secrets

Create `secrets/postgres_password` locally with the PostgreSQL password before
running Compose. This directory is excluded from Git and Docker build context.
Do not place provider keys, session keys, or administrator passwords here for
Step 36: provider configuration, session authentication, and administrator
bootstrap are introduced in later P5 steps.
