#!/bin/sh
# Manage the kokoro-tts-server container behind the "http" backend.
#
# `up` is idempotent and states which of four situations it found: already
# serving, container stopped, image present but not started, or nothing local
# at all (the slow first-run case, which is announced before it begins).
set -e
REMOTE=${KOKORO_TTS_IMAGE:-ghcr.io/csabakecskemeti/kokoro-tts-server:latest}
LOCAL=${KOKORO_TTS_LOCAL_IMAGE:-kokoro-tts-server:latest}
NAME=${KOKORO_TTS_CONTAINER:-kokoro-tts}
# The build context ships with the plugin, so a failed pull is recoverable
# without hunting down a separate repository.
CONTEXT=$(CDPATH= cd -- "$(dirname -- "$0")/../server" 2>/dev/null && pwd || true)
PORT=${KOKORO_TTS_PORT:-42821}
HEALTH="http://localhost:$PORT/health"

have_docker() {
  command -v docker >/dev/null 2>&1 || { echo "docker not found on PATH. Install Docker Desktop." >&2; exit 1; }
  docker info >/dev/null 2>&1 || { echo "Docker is installed but not running. Start Docker Desktop." >&2; exit 1; }
}

healthy()      { curl -fsS "$HEALTH" >/dev/null 2>&1; }
has_image()    { docker image inspect "$1" >/dev/null 2>&1; }
container_is() { docker ps $2 --format '{{.Names}}' | grep -qx "$NAME"; }

# A locally built image wins over the published one: if you built it, you meant it.
pick_image() {
  if has_image "$LOCAL"; then echo "$LOCAL"
  elif has_image "$REMOTE"; then echo "$REMOTE"
  else echo ""; fi
}

wait_healthy() {
  printf 'waiting for the server to become healthy'
  i=0
  while [ $i -lt "${1:-120}" ]; do
    healthy && { echo; return 0; }
    printf '.'; sleep 1; i=$((i+1))
  done
  echo; return 1
}

case "${1:-status}" in
  up)
    have_docker
    if healthy; then
      echo "already serving on http://localhost:$PORT (reusing it)"
      exit 0
    fi
    if container_is "$NAME" ""; then
      echo "container '$NAME' is running but not healthy yet"
      wait_healthy 120 && { echo "ready on http://localhost:$PORT"; exit 0; }
      echo "still unhealthy; see: docker logs $NAME" >&2; exit 1
    fi
    if container_is "$NAME" "-a"; then
      echo "starting existing container '$NAME'"
      docker start "$NAME" >/dev/null
    else
      IMAGE=$(pick_image)
      if [ -z "$IMAGE" ]; then
        echo "FIRST-TIME SETUP: no local image found."
        echo "Pulling $REMOTE -- this is a multi-GB download and will take"
        echo "several minutes. It happens once; later starts are seconds."
        echo
        if docker pull "$REMOTE"; then
          IMAGE="$REMOTE"
        elif [ -n "$CONTEXT" ] && [ -f "$CONTEXT/Dockerfile" ]; then
          echo
          echo "Pull failed. Falling back to building from the bundled source"
          echo "at $CONTEXT -- slower than a pull (it installs torch and bakes"
          echo "the model), but needs no registry access."
          echo
          docker build -t "$LOCAL" "$CONTEXT" || {
            echo "build failed; see the output above" >&2; exit 1; }
          IMAGE="$LOCAL"
        else
          echo "Pull failed and no bundled build context was found." >&2
          exit 1
        fi
      fi
      echo "starting a new container '$NAME' from $IMAGE"
      docker run -d --name "$NAME" -p "$PORT:8080" --restart unless-stopped "$IMAGE" >/dev/null
    fi
    # The model loads on boot, so the first health check trails the start.
    wait_healthy 180 || { echo "server did not become healthy; see: docker logs $NAME" >&2; exit 1; }
    echo "ready on http://localhost:$PORT"
    curl -fsS "$HEALTH" 2>/dev/null && echo ;;
  pull)    have_docker; docker pull "$REMOTE" ;;
  build)   have_docker
           [ -n "$CONTEXT" ] && [ -f "$CONTEXT/Dockerfile" ] || {
             echo "no bundled build context at ../server" >&2; exit 1; }
           echo "building $LOCAL from $CONTEXT"
           docker build -t "$LOCAL" "$CONTEXT" ;;
  down)    have_docker; docker stop "$NAME" >/dev/null 2>&1 && echo "stopped $NAME" || echo "$NAME not running" ;;
  restart) have_docker; docker restart "$NAME" >/dev/null && wait_healthy 180 && echo "restarted $NAME" ;;
  rm)      have_docker; docker rm -f "$NAME" >/dev/null 2>&1 && echo "removed $NAME" || echo "$NAME not present" ;;
  logs)    have_docker; docker logs --tail "${2:-40}" "$NAME" ;;
  status)
    have_docker
    docker ps --filter "name=^${NAME}$" --format 'container: {{.Names}}  {{.Status}}  {{.Ports}}' | grep . \
      || echo "container: not running"
    IMAGE=$(pick_image); [ -n "$IMAGE" ] && echo "image    : $IMAGE" || echo "image    : not built or pulled yet (run: server up)"
    [ -n "$CONTEXT" ] && [ -f "$CONTEXT/Dockerfile" ] && echo "source   : bundled at $CONTEXT"
    if healthy; then printf 'health   : '; curl -fsS "$HEALTH"; echo
    else echo "health   : unreachable on port $PORT"; fi ;;
  *) echo "usage: server-ctl.sh {up|down|restart|rm|pull|build|logs [N]|status}" >&2; exit 2 ;;
esac
