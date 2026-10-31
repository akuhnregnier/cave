#!/bin/bash
# Stands in for Apple `container`. A volume is a folder. A "VM" runs the real
# cave-companion against the inbox, then $FAKE_VM_SCRIPT in the workspace. FAKE_CRASH
# skips the final snapshot, FAKE_LOST_BUNDLE fails the clone of a repo with that tail,
# FAKE_RECOVERY_FAILS fails a recovery VM before it does anything, and FAKE_LISTINGS_FAIL
# fails every listing. A network is a folder too: FAKE_NETWORK_CREATE_FAILS=<n> fails that many creates,
# and FAKE_NETWORK_DELETE_FAILS fails every delete.
cat <<<"$*" >> "$FAKE_LOG" # in one write, as a VM's token can be on its way alongside a long `run`
volumes="${FAKE_LOG%/*}/volumes"
networks="${FAKE_LOG%/*}/networks"

# What the companion needs to see for sandbox $1. Each gets its own VM home.
vm_side() {
  local box="$HOME/Library/Application Support/cave/sandboxes/$1"
  export CAVE_VM_HOME="$CAVE_VM_HOME/$1" CAVE_OUTBOX="$box/outbox" CAVE_INBOX="$box/inbox"
  mkdir -p "$CAVE_VM_HOME"
  [ -z "${2:-}" ] || echo "$2" > "$CAVE_VM_HOME/.workspace"
  CAVE_WORKSPACE=$(cat "$CAVE_VM_HOME/.workspace")
  export CAVE_WORKSPACE
}

case "$1" in
  ls)
    [ -z "${FAKE_LISTINGS_FAIL:-}" ] || exit 1
    if [ "$2" = --format ]; then echo '[]'; else cat "$FAKE_RUNNING" 2>/dev/null; fi
    ;;
  image)
    # Images are lines in a file, and a fake build adds nothing, so tags stay exact.
    case "$2" in
      ls) cat "$FAKE_IMAGES" 2>/dev/null ;;
      tag) echo "$4" >> "$FAKE_IMAGES" ;;
      rm) grep -vxF "$3" "$FAKE_IMAGES" > "$FAKE_IMAGES.new"; mv "$FAKE_IMAGES.new" "$FAKE_IMAGES" ;;
    esac
    ;;
  volume)
    case "$2" in
      create) mkdir -p "$volumes/${!#}"; echo "${4#cave=}" > "$volumes/.label-${!#}" ;;
      delete) chmod -R u+rwx "$volumes/$3" 2>/dev/null; rm -rf "${volumes:?}/$3" ;;
      ls)
        [ -z "${FAKE_LISTINGS_FAIL:-}" ] || exit 1
        if [ "$3" = --format ]; then
          for volume in "$volumes"/*/; do
            volume=$(basename "$volume")
            jq -n --arg name "$volume" --arg label "$(cat "$volumes/.label-$volume" 2>/dev/null)" \
              '{configuration: {name: $name, labels: {cave: $label}}}'
          done | jq -s .
        else
          ls "$volumes" 2>/dev/null
        fi
        ;;
    esac
    ;;
  network)
    case "$2" in
      create)
        failed=$(cat "$networks.failed" 2>/dev/null || echo 0)
        if [ "$failed" -lt "${FAKE_NETWORK_CREATE_FAILS:-0}" ]; then echo $((failed + 1)) > "$networks.failed"; exit 1; fi
        [ ! -d "$networks/${!#}" ] || exit 1
        mkdir -p "$networks/${!#}"; echo "${4#cave=}" > "$networks/.label-${!#}" ;;
      delete) [ -z "${FAKE_NETWORK_DELETE_FAILS:-}" ] || exit 1
              rm -rf "${networks:?}/$3" ;;
      ls)
        [ -z "${FAKE_LISTINGS_FAIL:-}" ] || exit 1
        for network in "$networks"/*/; do
          [ -d "$network" ] || continue
          network=$(basename "$network")
          jq -n --arg name "$network" --arg label "$(cat "$networks/.label-$network" 2>/dev/null)" \
            '{configuration: {name: $name, labels: {cave: $label}}}'
        done | jq -s . ;;
    esac
    ;;
  stop)
    [ -z "${FAKE_STOP_FAILS:-}" ] || exit 1
    vm_side "${!#}"
    bash "$CAVE_COMPANION" --final
    ;;
  exec) # exec [-i] --user claude <id> cave-companion <mode> [args]
    [ -z "${FAKE_EXEC_FAILS:-}" ] || exit 1
    if [ "${!#}" = token ]; then cat > "${FAKE_LOG%/*}/token"; exit 0; fi
    while [ "$3" != cave-companion ]; do shift; done
    id=$2
    shift 3
    vm_side "$id"
    args=("${@//\/workspace/$volumes/cave-$id}") # VM paths, as `run` translates them
    bash "$CAVE_COMPANION" "${args[@]//\/cave\/inbox/$CAVE_INBOX}"
    ;;
  run)
    last=${!#}
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        --network) network=$2; shift ;;
        -e) case "$2" in CAVE_ID=*) id=${2#CAVE_ID=} ;; CAVE_REPOS=*) repos=${2#CAVE_REPOS=} ;; CAVE_WORKSPACE=*) workspace=${2#CAVE_WORKSPACE=} ;; CAVE_RESUME=*) export CAVE_RESUME=1 ;; esac; shift ;;
      esac
      shift
    done
    volume="$volumes/cave-$id"
    [ -d "$volume" ] || exit 125
    [ -z "${network:-}" ] || [ -d "$networks/$network" ] || exit 125 # a VM can only join a network that exists
    vm_side "$id" "$volume${workspace#/workspace}"
    rm -f "$CAVE_VM_HOME/.cave-final" # a new VM's home holds nothing of the last one
    mkdir -p "$CAVE_WORKSPACE"
    export CAVE_VOLUME="$volume"
    # Translate VM paths to the fake volume and the inbox, then do what the entrypoint would.
    CAVE_REPOS=$(python3 -c '
import json, os, sys
volume, inbox = sys.argv[2:]
places = {"/workspace": volume, "/cave/inbox": inbox}
def local(path):
    return next(host + path[len(vm):] for vm, host in places.items() if path.startswith(vm))
def bundle(repo):
    lost = os.environ.get("FAKE_LOST_BUNDLE")
    return "/nowhere" if lost and repo["path"].endswith(lost) else local(repo["bundle"])
print(json.dumps([dict(r, path=local(r["path"]), bundle=bundle(r)) for r in json.loads(sys.argv[1])]))
' "$repos" "$volume" "$CAVE_INBOX")
    if [ "$last" = recover ]; then
      [ -z "${FAKE_RECOVERY_FAILS:-}" ] || { echo "the recovery went wrong" >&2; exit 1; }
      CAVE_ID=$id CAVE_REPOS=$CAVE_REPOS bash "$CAVE_COMPANION" recover
      exit
    fi
    CAVE_ID=$id CAVE_REPOS=$CAVE_REPOS bash "$CAVE_COMPANION" setup
    if [ -n "${FAKE_VM_SCRIPT:-}" ]; then (cd "$CAVE_WORKSPACE" && CONFIG="$volume/.cave-config" bash -c "$FAKE_VM_SCRIPT"); fi
    [ -n "${FAKE_CRASH:-}" ] && exit 137
    bash "$CAVE_COMPANION" --final
    ;;
esac
exit 0
