#!/usr/bin/env bash
# One-time bootstrap for harness-box (Ubuntu 24.04, m6i.xlarge, 200 GB gp3).
# Run as root once after first boot:  sudo bash box-bootstrap.sh <git-remote-url>
# Idempotent. Creates the advloop user, docker, the repo checkout at
# /srv/adv-loop/repo, the state and workspace directories, the workspace
# image, and the systemd unit. Never writes a credential; the owner pushes
# tokens with adv-token-push and drops the Azure key into /etc/adv-loop/env.d.
set -euo pipefail

REMOTE="${1:-https://github.com/castor639/adv-loop.git}"
BRANCH="${2:-main}"
IMAGE_TAG="${IMAGE_TAG:-adv-loop-ws:v4.29.0-2}"

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-venv jq unzip rsync
if ! command -v aws >/dev/null 2>&1; then
  # Ubuntu 24.04 has no awscli package; use the official v2 installer
  tmp="$(mktemp -d)"
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "$tmp/awscliv2.zip"
  unzip -q "$tmp/awscliv2.zip" -d "$tmp"
  "$tmp/aws/install" >/dev/null
  rm -rf "$tmp"
fi

if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
fi

id advloop >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash advloop || useradd -m -s /bin/bash advloop
usermod -aG docker advloop

install -d -o advloop -g advloop /srv/adv-loop /srv/adv-loop/state /srv/adv-loop/workspaces /srv/adv-loop/inbox
install -d -o root -g advloop -m 0750 /etc/adv-loop /etc/adv-loop/env.d

# SKIP_GIT=1 means the checkout was rsynced from the operator's machine (private repo, no token on the box).
if [ "${SKIP_GIT:-0}" = "1" ]; then
  [ -d /srv/adv-loop/repo ] || { echo "SKIP_GIT=1 but /srv/adv-loop/repo is missing"; exit 2; }
  chown -R advloop:advloop /srv/adv-loop/repo
elif [ ! -d /srv/adv-loop/repo/.git ]; then
  sudo -u advloop git clone --branch "$BRANCH" "$REMOTE" /srv/adv-loop/repo
else
  sudo -u advloop git -C /srv/adv-loop/repo fetch origin && sudo -u advloop git -C /srv/adv-loop/repo checkout "$BRANCH" && sudo -u advloop git -C /srv/adv-loop/repo pull --ff-only
fi
sudo -u advloop python3 -m venv /srv/adv-loop/venv
sudo -u advloop /srv/adv-loop/venv/bin/pip install -q -e /srv/adv-loop/repo

# The workspace image; the Lean stage downloads the Mathlib cache and takes a while the first time.
# BUILD_IMAGE=0 defers it (run later: docker build -t "$IMAGE_TAG" -f harness/container/Dockerfile /srv/adv-loop/repo).
if [ "${BUILD_IMAGE:-1}" = "1" ] && ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
  docker build -t "$IMAGE_TAG" -f /srv/adv-loop/repo/harness/container/Dockerfile /srv/adv-loop/repo
fi

install -m 0644 /srv/adv-loop/repo/harness/aws/adv-harness.service /etc/systemd/system/adv-harness.service
install -m 0755 /srv/adv-loop/repo/harness/aws/adv-sync-inbox /usr/local/bin/adv-sync-inbox
install -m 0644 /srv/adv-loop/repo/harness/aws/adv-aws-cost.service /etc/systemd/system/adv-aws-cost.service
install -m 0644 /srv/adv-loop/repo/harness/aws/adv-aws-cost.timer /etc/systemd/system/adv-aws-cost.timer
# The ssh identity harness-box uses for the GPU box; its public half goes into launch-gpu-box.sh.
if [ ! -f /srv/adv-loop/state/gpu-box.key ]; then
  sudo -u advloop ssh-keygen -q -t ed25519 -N "" -C adv-harness-box -f /srv/adv-loop/state/gpu-box.key
fi
# advloop may power the box off after an idle window, nothing else.
echo 'advloop ALL=(root) NOPASSWD: /sbin/shutdown -h +2' > /etc/sudoers.d/adv-harness
chmod 0440 /etc/sudoers.d/adv-harness
systemctl daemon-reload
systemctl enable adv-harness.service
systemctl enable --now adv-aws-cost.timer
echo "bootstrap complete; place the Azure key in /etc/adv-loop/env.d/azure (mode 0440 root:advloop), then: systemctl start adv-harness"
