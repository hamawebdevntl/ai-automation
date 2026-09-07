#!/usr/bin/env bash
#
# Deploy to the VPS. Run it on the box.
#
# This replaces: a GitHub Actions workflow assuming an OIDC role to push four
# images to ECR, then `terraform apply -var image_tag=<sha>` against 17 .tf
# files. Images are built here instead, which is slower per deploy and removes
# a registry, a role, and the question of which tag is live.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Pulling"
git pull --ff-only
git submodule update --init --recursive

echo "==> Building the approval app"
( cd apps/web && bun install --frozen-lockfile && bun run build )

echo "==> Building images"
docker compose build

# `up -d` sends SIGTERM to what it replaces. The worker handles it: it finishes
# the step it is on and releases its leases, so a redeploy costs seconds of
# latency rather than a full lease TTL of a stalled queue. Do not use `kill`.
echo "==> Starting"
docker compose up -d --remove-orphans

echo "==> Waiting for health"
docker compose ps

cat <<'NOTE'

Deployed.

  docker compose logs -f worker     what the driver is doing
  docker compose logs -f mpt        renders

If a production is stuck, it is parked and waiting for a person:

  select id, status, error, run_state->>'step' from productions where status = 'parked';

NOTE
