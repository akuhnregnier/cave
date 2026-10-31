#!/bin/bash
# Runs in the VM. Clones the repos, prepares ~/.claude, runs claude, then commits what is left.
set -u
config=/workspace/.cave-config
token_file=/run/cave/token

mkdir -p "$config" "$CAVE_WORKSPACE"
cave-companion setup

# The repo's settings.json and CLAUDE.md are copied in; cave's required settings win.
settings=/dev/null
[ -f "$config/settings.json" ] && settings="$config/settings.json"
jq -s 'add // {} | . * {
  enableAllProjectMcpServers: true,
  skipDangerousModePermissionPrompt: true,
  sandbox: {enabled: false}
}' "$settings" > ~/.claude/settings.json
[ -f "$config/CLAUDE.md" ] && cp "$config/CLAUDE.md" ~/.claude/CLAUDE.md
jq -n --arg dir "$CAVE_WORKSPACE" '{
  hasCompletedOnboarding: true,
  bypassPermissionsModeAccepted: true,
  projects: {($dir): {hasTrustDialogAccepted: true}}
}' > ~/.claude.json

shopt -s nullglob
plugins=("$config"/plugins/*/)
if [ "${#plugins[@]}" -gt 0 ]; then
  CLAUDE_CODE_PLUGIN_DIRS=$(IFS=:; echo "${plugins[*]%/}")
  export CLAUDE_CODE_PLUGIN_DIRS
fi

finish() {
  local status=$?
  trap - EXIT
  : > ~/.cave-final # first: a bundle that starts after this is refused, and one before dies next
  kill -KILL -1 2>/dev/null # whatever the session left running would race the snapshot
  cave-companion --final
  exit "$status"
}
trap finish EXIT

CLAUDE_STOP_SECONDS=5

cave-companion watch < /dev/null > /dev/null 2>&1 & # killed with the rest of the session

cd "$CAVE_WORKSPACE"
if [ -n "${CAVE_TOKEN_EXPECTED:-}" ]; then
  for _ in $(seq 1 400); do
    [ -s "$token_file" ] && break
    sleep 0.1
  done
  if [ -s "$token_file" ]; then
    CLAUDE_CODE_OAUTH_TOKEN=$(cat "$token_file")
    export CLAUDE_CODE_OAUTH_TOKEN
    rm -f "$token_file"
  fi
fi
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "cave: no token stored, starting logged out. Run 'cave token' on the host." >&2
fi

# On a stop signal, give Claude $1 seconds to save its session, then kill every process
# the session started, so that nothing writes during the final commit. Whatever called
# stop must allow this and the commit: `cave stop` allows 20 seconds in total.
stop_session() {
  trap '' TERM HUP USR1
  kill -TERM "$pid" 2>/dev/null
  for _ in $(seq 1 $(($1 * 10))); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  kill -KILL -1 2>/dev/null
}

# In the background so that bash can act on signals while claude runs. Traps run
# only between commands, so a foreground claude would delay them until it exits.
# With no job control, `&` would give claude /dev/null as stdin, hence the redirect.
claude --dangerously-skip-permissions --name "$CAVE_NAME" "$@" <&0 &
pid=$!
trap 'stop_session "$CLAUDE_STOP_SECONDS"' USR1 # `cave stop`
# Anything else, like a plain `container stop`, may allow only 5 seconds, so the commit gets them.
trap 'stop_session 0' TERM HUP
trap : INT # a handler rather than '' so that claude still gets the default action
while :; do
  wait "$pid"
  status=$?
  kill -0 "$pid" 2>/dev/null || break
done
exit "$status"
