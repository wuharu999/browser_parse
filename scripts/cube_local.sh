#!/usr/bin/env bash
# Manage the disposable CubeSandbox development VM without host privilege changes.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CUBE_DATA="${CUBE_LOCAL_DATA:-${REPO_ROOT}/data/cube-local}"
CUBE_SOURCE="${CUBE_DATA}/source"
DEV_ENV="${CUBE_SOURCE}/dev-env"
VM_IMAGE="${CUBE_DATA}/OpenCloudOS-GenericCloud-9.6-20260514.2.x86_64.qcow2"
VM_HOST_IMAGE="${CUBE_VM_HOST_IMAGE:-robot-cube-vm-host:local}"
VM_CONTAINER="${CUBE_VM_CONTAINER:-robot-cube-vm}"
REGISTRY_CONTAINER="${CUBE_REGISTRY_CONTAINER:-robot-cube-registry}"
KNOWN_HOSTS="${CUBE_DATA}/known_hosts"
ASKPASS="${CUBE_DATA}/.ssh-askpass.sh"
SSH_PORT="${CUBE_SSH_PORT:-10022}"

die() { printf 'cube-local: %s\n' "$*" >&2; exit 1; }
container_exists() { docker container inspect "$1" >/dev/null 2>&1; }
container_running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" = true ]; }

write_askpass() {
  mkdir -p "$CUBE_DATA"
  umask 077
  printf '#!/usr/bin/env bash\nprintf '\''%%s\\n'\'' %q\n' "${CUBE_VM_PASSWORD:-opencloudos}" >"$ASKPASS"
  chmod 700 "$ASKPASS"
  touch "$KNOWN_HOSTS"
  chmod 600 "$KNOWN_HOSTS"
}

ssh_guest() {
  write_askpass
  local remote="sudo -n -i"
  if [ "$#" -gt 0 ]; then
    remote="sudo -n --"
    local arg quoted
    for arg in "$@"; do printf -v quoted '%q' "$arg"; remote+=" $quoted"; done
  fi
  DISPLAY="${DISPLAY:-cube-local}" SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force \
    setsid -w ssh -t \
      -o StrictHostKeyChecking=accept-new \
      -o UserKnownHostsFile="$KNOWN_HOSTS" \
      -o PreferredAuthentications=password \
      -o PubkeyAuthentication=no \
      -p "$SSH_PORT" opencloudos@127.0.0.1 "$remote"
}

start() {
  command -v docker >/dev/null || die "docker is required"
  [ -c /dev/kvm ] || die "/dev/kvm is unavailable"
  [ -d "$DEV_ENV" ] || die "official CubeSandbox dev-env is missing: $DEV_ENV"
  [ -f "$VM_IMAGE" ] || die "prepared qcow2 is missing: $VM_IMAGE"
  docker image inspect "$VM_HOST_IMAGE" >/dev/null 2>&1 || die "QEMU tools image is missing: $VM_HOST_IMAGE"

  if container_exists "$REGISTRY_CONTAINER" && ! container_running "$REGISTRY_CONTAINER"; then
    docker start "$REGISTRY_CONTAINER" >/dev/null
  fi

  if container_running "$VM_CONTAINER"; then
    printf '%s is already running.\n' "$VM_CONTAINER"
    return
  fi
  if container_exists "$VM_CONTAINER"; then
    docker start "$VM_CONTAINER" >/dev/null
  else
    local kvm_gid
    kvm_gid="$(stat -c '%g' /dev/kvm)"
    docker run -d --name "$VM_CONTAINER" \
      --user 1000:1000 \
      --group-add "$kvm_gid" \
      --cap-drop ALL \
      --security-opt no-new-privileges \
      --device /dev/kvm \
      --network host \
      --cpus 4 \
      --memory 10g \
      --mount "type=bind,src=${CUBE_DATA},dst=/cube-data" \
      --workdir /cube-data/source/dev-env \
      --env WORK_DIR=/cube-data \
      --env IMAGE_PATH="/cube-data/$(basename "$VM_IMAGE")" \
      --env VM_MEMORY_MB=8192 \
      --env VM_CPUS=4 \
      "$VM_HOST_IMAGE" bash ./run_vm.sh >/dev/null
  fi
  printf '%s started; SSH is 127.0.0.1:%s and CubeAPI is 127.0.0.1:13000.\n' "$VM_CONTAINER" "$SSH_PORT"
}

status() {
  for name in "$VM_CONTAINER" "$REGISTRY_CONTAINER"; do
    if container_exists "$name"; then
      docker inspect -f '{{.Name}}: {{.State.Status}}' "$name" | sed 's#^/##'
    else
      printf '%s: absent\n' "$name"
    fi
  done
  if curl -fsS --max-time 2 http://127.0.0.1:13000/health >/dev/null 2>&1; then
    printf 'CubeAPI: healthy\n'
  else
    printf 'CubeAPI: not ready\n'
  fi
}

stop() {
  if ! container_running "$VM_CONTAINER"; then
    printf '%s is not running.\n' "$VM_CONTAINER"
    return
  fi
  printf 'Requesting a graceful guest poweroff...\n'
  if ! ssh_guest poweroff; then
    die "guest poweroff request failed; VM was left running (no forced-stop fallback)"
  fi
  local attempt
  for attempt in $(seq 1 60); do
    container_running "$VM_CONTAINER" || { printf '%s stopped cleanly.\n' "$VM_CONTAINER"; return; }
    sleep 1
  done
  die "guest did not stop within 60 seconds; VM was left running (no forced-stop fallback)"
}

usage() {
  printf 'Usage: %s {start|status|stop|ssh [command ...]}\n' "${0##*/}"
}

case "${1:-}" in
  start) [ "$#" -eq 1 ] || die "start takes no arguments"; start ;;
  status) [ "$#" -eq 1 ] || die "status takes no arguments"; status ;;
  stop) [ "$#" -eq 1 ] || die "stop takes no arguments"; stop ;;
  ssh) shift; ssh_guest "$@" ;;
  *) usage; exit 2 ;;
esac
