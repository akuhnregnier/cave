FROM cave-base:latest
USER root
RUN mv "$(readlink -f /usr/local/bin/claude)" /usr/local/bin/claude.real; rm -f /usr/local/bin/claude
COPY stub /usr/local/bin/claude
COPY slow_git /usr/local/bin/git
RUN chmod +x /usr/local/bin/claude /usr/local/bin/git
USER claude
