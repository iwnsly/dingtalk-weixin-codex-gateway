#!/bin/zsh

set -u

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
DOCKER_APP="/Applications/Docker.app"
DOCKER_BIN="${DOCKER_BIN:-$(command -v docker)}"

if [[ ! -d "$DOCKER_APP" ]]; then
  print -u2 "Docker Desktop is not installed at $DOCKER_APP"
  exit 1
fi

if [[ -z "$DOCKER_BIN" || ! -x "$DOCKER_BIN" ]]; then
  print -u2 "Docker CLI was not found in PATH"
  exit 1
fi

/usr/bin/open -gja "$DOCKER_APP"

for attempt in {1..90}; do
  if "$DOCKER_BIN" --context desktop-linux info >/dev/null 2>&1; then
    exec "$DOCKER_BIN" --context desktop-linux compose \
      --project-directory "$PROJECT_DIR" \
      -f "$PROJECT_DIR/docker-compose.yml" \
      up -d
  fi
  /bin/sleep 2
done

print -u2 "Docker Desktop did not become ready within 180 seconds"
exit 1
