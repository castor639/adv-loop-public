#!/usr/bin/env bash
# Box-side bootstrap for adv-gpu-box, pushed to /srv/adv-loop/repo and run as root over ssh by
# GpuBox.bootstrap():  sudo -E bash /srv/adv-loop/repo/harness/aws/gpu-box-bootstrap.sh
# Idempotent. Installs the agent, the watchdog timer, and the pinned GPU image, and records
# the hash of what it installed so the controller can tell a stale box from a current one.
# Required env: ADV_GPU_IMAGE (image tag), ADV_GPU_BOOTSTRAP_HASH (sha256 the controller computed).
# Optional: ADV_GPU_FORCE_BUILD=1, ADV_GPU_IDLE_MINUTES (15), ADV_GPU_MAX_UPTIME_MINUTES (720).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
REPO="${ADV_GPU_REPO:-/srv/adv-loop/repo}"
IMAGE="${ADV_GPU_IMAGE:?set ADV_GPU_IMAGE}"
HASH="${ADV_GPU_BOOTSTRAP_HASH:?set ADV_GPU_BOOTSTRAP_HASH}"
BOOTSTRAP_FILE="${ADV_GPU_BOOTSTRAP_FILE:-/srv/adv-loop/.bootstrap.json}"
AGENT_DIR="$REPO/harness/gpu/agent"

[ "$(id -u)" = "0" ] || { echo "run as root" >&2; exit 2; }

if ! command -v rsync >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q --no-install-recommends rsync jq
fi
# Unattended upgrades would restart docker or the NVIDIA driver under a job.
for unit in apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service unattended-upgrades.service; do
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
done

if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
  command -v nvidia-ctk >/dev/null 2>&1 || { echo "nvidia-ctk missing; not a DLAMI with the container toolkit" >&2; exit 3; }
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
  docker info --format '{{json .Runtimes}}' | grep -q nvidia || { echo "docker still has no nvidia runtime" >&2; exit 3; }
fi
[ "$(id -u ubuntu)" = "1000" ] || { echo "ubuntu is not uid 1000; the mirror ownership contract breaks" >&2; exit 3; }
id -nG ubuntu | tr ' ' '\n' | grep -qx docker || usermod -aG docker ubuntu

install -d -o 1000 -g 1000 /srv/adv-loop /srv/adv-loop/workspaces /srv/adv-loop/repo /srv/adv-loop/jobs /srv/adv-loop/cache
install -m 0755 "$AGENT_DIR/adv-gpu-agent" /usr/local/bin/adv-gpu-agent
install -m 0755 "$AGENT_DIR/adv-gpu-watchdog" /usr/local/bin/adv-gpu-watchdog
install -m 0644 "$AGENT_DIR/adv-gpu-watchdog.service" /etc/systemd/system/adv-gpu-watchdog.service
install -m 0644 "$AGENT_DIR/adv-gpu-watchdog.timer" /etc/systemd/system/adv-gpu-watchdog.timer
install -m 0644 "$AGENT_DIR/adv-gpu.tmpfiles.conf" /etc/tmpfiles.d/adv-gpu.conf
systemd-tmpfiles --create /etc/tmpfiles.d/adv-gpu.conf
install -d -m 0755 /etc/adv-gpu
printf 'ADV_GPU_IDLE_MINUTES=%s\nADV_GPU_MAX_UPTIME_MINUTES=%s\nADV_GPU_IMAGE=%s\n' \
  "${ADV_GPU_IDLE_MINUTES:-15}" "${ADV_GPU_MAX_UPTIME_MINUTES:-720}" "$IMAGE" > /etc/adv-gpu/env
systemctl daemon-reload
systemctl enable --now adv-gpu-watchdog.timer >/dev/null

previous="$(jq -r '.hash // empty' "$BOOTSTRAP_FILE" 2>/dev/null || true)"
if [ "${ADV_GPU_FORCE_BUILD:-0}" = "1" ] || [ "$previous" != "$HASH" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  [ -f "$REPO/harness/container/Dockerfile.gpu" ] || { echo "$REPO/harness/container/Dockerfile.gpu missing" >&2; exit 4; }
  docker build -t "$IMAGE" -f "$REPO/harness/container/Dockerfile.gpu" "$REPO"
fi
docker run --rm --gpus all --user 1000:1000 "$IMAGE" nvidia-smi -L >/dev/null

digest="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n 1 | tr -d ' ')"
jq -n --arg hash "$HASH" --arg image "$IMAGE" --arg digest "$digest" --arg driver "$driver" \
  --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{hash: $hash, image: $image, image_digest: $digest, driver: $driver, at: $at}' > "$BOOTSTRAP_FILE.tmp"
mv "$BOOTSTRAP_FILE.tmp" "$BOOTSTRAP_FILE"
chown 1000:1000 "$BOOTSTRAP_FILE"
chmod 0644 "$BOOTSTRAP_FILE"
cat "$BOOTSTRAP_FILE"
