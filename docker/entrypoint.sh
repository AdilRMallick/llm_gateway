#!/usr/bin/env sh
set -e

# The gateway image also runs the mock provider (same deps, one build).
# Only the gateway needs migrations.
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "running alembic upgrade head"
  alembic upgrade head
fi

exec "$@"
