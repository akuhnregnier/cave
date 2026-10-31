#!/bin/bash
# Replaces claude in the test image. Reports what the VM looks like, then leaves
# work behind like a real session. `claude.real` is the real binary.
if [ "$4" = stdin ]; then
  cat
  exit 0
fi
if [ "$4" = stubborn ] || [ "$4" = slow ]; then
  # Ignores SIGTERM, keeps a background process writing, and holds a stale git lock.
  [ "$4" = slow ] && touch /tmp/slow-git
  trap '' TERM
  (while :; do date >> background.txt; sleep 0.1; done) &
  touch .git/index.lock
  echo live > live.txt
  while :; do sleep 1; done
fi
if [ "$4" = hang ] || [ "$4" = locked ]; then
  [ "$4" = locked ] && touch .git/index.lock
  echo live > live.txt
  exec sleep 300
fi
if [ "${4#cave resolve}" != "$4" ]; then
  git fetch -q origin
  git rebase origin/main > /dev/null 2>&1
  echo resolved > file.txt
  git add -A
  GIT_EDITOR=true git rebase --continue > /dev/null
  exit 0
fi
echo "ARGS: $*"
echo "USER: $(id -un) PWD=$PWD"
echo "PLUGINS: ${CLAUDE_CODE_PLUGIN_DIRS:-unset}"
echo "SETTINGS: $(jq -c . ~/.claude/settings.json)"
echo "CLAUDEMD: $(cat ~/.claude/CLAUDE.md)"
echo "BRANCH: $(git branch --show-current) REMOTES: [$(git remote)] AUTHOR: $(git config user.name)"
echo "PLUGINLIST: $(claude.real plugin list 2>&1 | tr '\n' ' ')"
mkdir -p ~/.claude/projects/-workspace-project
echo '{}' > ~/.claude/projects/-workspace-project/2f75b9f7-cef5-417b-8933-cc966ac1cc27.jsonl
echo committed >> file.txt
git commit -qam 'committed by claude'
echo leftover > new.txt
echo edit >> /workspace/.cave-config/CLAUDE.md
[ -d nested ] && echo nested > nested/new.txt
exit 7
