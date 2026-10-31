FROM docker.io/library/node:22-bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git jq less procps python3 ripgrep \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
RUN groupmod -n claude node \
 && usermod -l claude -d /home/claude -m node \
 && mkdir -p /home/claude/.claude/projects /workspace /cave \
 && chown -R claude:claude /home/claude /workspace /cave
COPY cave-init /usr/local/bin/cave-init
COPY entrypoint.sh /usr/local/bin/cave-entrypoint
COPY cave-companion /usr/local/bin/cave-companion
COPY cave-hook /usr/local/bin/cave-hook
COPY managed-settings.json /etc/claude-code/managed-settings.json
RUN chmod +x /usr/local/bin/cave-init /usr/local/bin/cave-entrypoint /usr/local/bin/cave-companion /usr/local/bin/cave-hook
ENV LANG=C.UTF-8 DISABLE_AUTOUPDATER=1
USER claude
ENTRYPOINT ["/usr/local/bin/cave-init"]
