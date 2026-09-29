# adv-loop workspace image. Two stages: a Lean base that changes rarely, and the
# harness stage that adds Node and the pinned Claude Code binary on top.
#
#   docker build --target lean-base -t adv-loop-lean-base:v4.29.0 -f harness/container/Dockerfile .
#   docker build -t adv-loop-ws:v4.29.0-2 -f harness/container/Dockerfile .
#
# No credentials are ever baked in. The workspace is bind-mounted at its host
# absolute path so validator hooks run unchanged inside and outside.

ARG UBUNTU=24.04

FROM ubuntu:${UBUNTU} AS lean-base
ARG LEAN_VERSION=v4.29.0
ARG MATHLIB_REV=master
ARG Z3_VERSION=4.13.4
ARG UV_VERSION=0.8.4
ENV DEBIAN_FRONTEND=noninteractive \
    ELAN_HOME=/opt/elan \
    PATH=/opt/elan/bin:/opt/uv:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git python3 python3-venv build-essential \
        libgmp-dev unzip zstd jq \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user; claude refuses --dangerously-skip-permissions as root.
RUN userdel -r ubuntu 2>/dev/null || true \
    && groupadd -g 1000 advloop && useradd -m -u 1000 -g 1000 -s /bin/bash advloop

# uv (pinned) for Python sandboxes.
RUN mkdir -p /opt/uv && curl -LsSf "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
    | tar -xz -C /opt/uv --strip-components=1

# z3 (pinned) with its python bindings.
RUN curl -LsSf "https://github.com/Z3Prover/z3/releases/download/z3-${Z3_VERSION}/z3-${Z3_VERSION}-x64-glibc-2.35.zip" -o /tmp/z3.zip \
    && unzip -q /tmp/z3.zip -d /opt && mv /opt/z3-${Z3_VERSION}-x64-glibc-2.35 /opt/z3 \
    && ln -s /opt/z3/bin/z3 /usr/local/bin/z3 && rm /tmp/z3.zip

# elan + the pinned Lean toolchain, owned by advloop so lake can read caches.
RUN mkdir -p /opt/elan /opt/lean && chown -R advloop:advloop /opt/elan /opt/lean
USER advloop
RUN curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
    | sh -s -- -y --default-toolchain "leanprover/lean4:${LEAN_VERSION}" --no-modify-path

# Mathlib at the pinned revision with its olean cache, plus a bare mirror so
# workspace projects resolve the dependency locally instead of cloning GitHub.
WORKDIR /opt/lean
RUN git clone --filter=blob:none https://github.com/leanprover-community/mathlib4.git mathlib4 \
    && git -C mathlib4 checkout "${MATHLIB_REV}" \
    && git clone --bare mathlib4 mathlib4.git \
    && git config --global url."/opt/lean/mathlib4.git".insteadOf "https://github.com/leanprover-community/mathlib4" \
    && git config --global url."/opt/lean/mathlib4.git".insteadOf "https://github.com/leanprover-community/mathlib4.git"
RUN cd mathlib4 && lake exe cache get && lake build --no-build 2>/dev/null || true

# Template project every workspace sandbox starts from.
COPY --chown=advloop:advloop harness/container/lean-template /opt/lean/template
RUN cd /opt/lean/template && cp ../mathlib4/lean-toolchain . && lake update mathlib 2>&1 | tail -2 && lake exe cache get 2>&1 | tail -2

RUN printf '%s\n' "lean=${LEAN_VERSION}" "mathlib=$(git -C /opt/lean/mathlib4 rev-parse HEAD)" "z3=${Z3_VERSION}" "uv=${UV_VERSION}" > /opt/lean/PINS
WORKDIR /home/advloop

FROM lean-base AS harness
ARG NODE_MAJOR=22
ARG CLAUDE_CODE_VERSION=2.1.261
USER root
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && echo "claude-code=${CLAUDE_CODE_VERSION}" >> /opt/lean/PINS
COPY harness/container/adv-validate /usr/local/bin/adv-validate
COPY harness/container/adv-gpu-run /usr/local/bin/adv-gpu-run
COPY harness/container/toolchain_stamp.py /usr/local/lib/adv-loop/toolchain_stamp.py
COPY harness/container/ledger_hook.py /usr/local/lib/adv-loop/ledger_hook.py
RUN chmod 0755 /usr/local/bin/adv-validate /usr/local/bin/adv-gpu-run /usr/local/lib/adv-loop/*.py
USER advloop
# Headless defaults: no built-in sandbox, bypass mode, onboarding marked done.
RUN mkdir -p /home/advloop/.claude && printf '%s\n' \
    '{"hasCompletedOnboarding": true, "permissions": {"defaultMode": "bypassPermissions"}, "sandbox": {"enabled": false}}' \
    > /home/advloop/.claude/settings.json \
    && printf '%s\n' '{"hasCompletedOnboarding": true}' > /home/advloop/.claude.json
ENV PATH=/opt/elan/bin:/opt/uv:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /home/advloop
CMD ["sleep", "infinity"]
