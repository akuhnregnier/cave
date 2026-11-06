# cave

Run Claude Code in a disposable Linux micro-VM, one per sandbox, with
`--dangerously-skip-permissions` on. Claude works on its own clone of your repo,
and you review and merge the result from the host.

cave is a Python package with no runtime dependencies. It wraps Apple
[`container`](https://github.com/apple/container) and git.

## Requirements

- macOS 26+ on Apple Silicon
- `brew install container`
- [uv](https://docs.astral.sh/uv/), Python 3.9+, and git 2.50.1+
- Claude Code on the host, for `cave token` only
- [delta](https://github.com/dandavison/delta), optional, for highlighted diffs in `-p`

## Setup

```sh
uv tool install .   # install cave on your PATH
cave init           # create the config repo
cave token          # create and store a long-lived token
```

`cave token` runs `claude setup-token`, then asks you to paste the token. It is
stored in the Keychain item `cave-claude-token`. Without a token, sandboxes start
logged out.

## Usage

Run these in a git repo, or in a directory that has git repos under it:

```sh
cave                       # start a sandbox and an interactive session
cave --name auth-fix       # the same, with a name of your own
cave run -- -p "fix it"    # pass arguments to claude
cave run -- --continue     # continue this project's last session
cave ls                    # list every sandbox, and what the running ones are doing
cave diff [-p] [target]    # show a sandbox's commits and files, without landing them
cave merge [-p] [target]   # review its work and land it on main
cave stop [target]         # stop a running sandbox, committing its work
cave resolve [target]      # have Claude rebase a repo that couldn't land
cave add <path> [target]   # give a running sandbox a copy of another repo
cave requests [target]     # answer what a running sandbox has asked for
cave discard [target]      # delete it without merging (stops the VM if needed)
cave prune                 # delete anything older than 30 days
cave build [--no-cache]    # rebuild images, which also updates Claude Code
```

cave finds every git repo in and under that directory (or under the repo root, if
you're inside a repo) and recreates each one at the same relative path in the VM.
It lists them at startup, along with anything it skipped. Repos need a commit on
`main` or `master`, and repos inside dependency folders such as `node_modules` aren't
included. A repo that can't be bundled or cloned is skipped rather than stopping the
launch. Each clone holds only the history of the base branch, with no other branches
or tags. Submodules stay empty and Git LFS files stay pointers, and cave says so at
startup. cave can't start from a linked worktree.

A sandbox starts from the last commit on `main` in each repo, so uncommitted host
changes aren't included. Claude commits to `claude/<id>`. When Claude exits, the
VM commits anything left over as `WIP`, in every repo. A repo partway through a merge
or rebase, or left on another branch, is skipped, listed, and held back until `cave
resolve` finishes it.

### Choosing a sandbox

Each sandbox has an id and a name such as `cave-myproject-3fa2-auth-fix`, made of the
start folder, a short hash of its path, and `--name` or a generated label. Claude
Code shows the name too.

A target is an id or a name, or a unique start of either. Without one, cave uses the
sandbox that holds the current repo or, in a plain folder, one rooted here or in a
parent. Sandboxes below the current folder are never picked, only listed by `cave ls`.
With several matches, cave offers a picker, nearest first, or lists them and stops if
there's no terminal. It says which sandbox it chose unless you're in its root, and
`cave discard` always says.

Sandboxes can nest, for example to show a second one fewer repos. cave warns at startup
if a parent sandbox already holds these repos.

### Reviewing and merging

`cave diff` and `cave merge` work across all the repos in a sandbox, and they skip
repos with no changes. Each repo lands independently, so a problem in one doesn't
hold the others back. Run the command again once it's sorted out.

The commits arrive as a bundle, held under `refs/cave/<id>/` in the host repo, and
`main` moves only after you approve them. Each change set is rebased onto `main` in
a scratch clone, never your working tree, then fast-forwarded, so history stays linear.
Merges of a repo wait for each other, and the first to land wins.

Before landing, `cave merge` shows, per repo, the sandbox's name and root, then every
commit with the files it touched, so that a file added and later deleted still shows.
`-p` adds the line-by-line diffs. At the top it groups what deserves a closer look:

- files that take effect without being run: `.gitattributes`, git hooks, Claude and other
  assistants' files, tool-version files such as `.envrc`, editor settings and CI workflows.
  `git config --add cave.flagged <path>` adds to the list for a repo
- symlinks, submodule pointers, executable-bit changes and binary files

Everything in the report is escaped, so what a VM wrote can't act on your terminal:
control, bidi and zero-width characters and invalid UTF-8 show as visible text, with
a warning line. Very long lines are cut with a note. Binary files are listed, not
printed.

Only a typed `y` or `yes` lands a repo. Rejecting it discards the fetched refs and keeps the
sandbox. Without a terminal, `cave merge` refuses unless you pass `--yes`, which lands
without asking and still prints the report.

What `cave merge` does next depends on the sandbox. A running one lands committed work
only, as snapshotting a live Claude would race it, and stays up. Each merge takes only
what the last one didn't. A stopped one is deleted once everything has landed.

A sandbox that stopped before its final commit, which `cave ls` shows as `crashed`, needs
nothing special: `cave diff` and `cave merge` boot a short-lived recovery VM from the same
image, with no Claude and no token, which clears stale locks and takes the snapshot. A repo
it can't snapshot is held back.

A repo is held back, with the reason shown and nothing lost, if:

- its base branch has gone
- the host repo has uncommitted changes, or a rebase, merge or cherry-pick in progress
- the new commits would overwrite an untracked or ignored host file, such as `.env`
- its history has a merge commit, which rebasing would drop
- Claude rewrote commits that an earlier merge already landed
- a merge or rebase is under way in the VM, or a session ended off `claude/<id>`
- its bundle is missing, malformed, over the size cap, or holds anything but its branch

`cave merge` also keeps a stopped sandbox if the VM holds work no repo can carry, meaning
repos created in the VM or files outside any repo. Copy out what you need (see below), then use
`--force` or `cave discard`. `--force` deletes the sandbox despite that work, once it's
named. It skips no review, and never drops a repo held back. Only `cave discard` does.

On a rebase conflict, cave discards the scratch clone, leaves the host repo as it was,
and keeps the commits. `cave resolve` then gives Claude the current `main`: a running
sandbox gets a note, and a stopped one a new session on the same volume, briefed to rebase.
Run `cave merge` again afterwards, and the result goes through the same review. Any repo
`cave merge` held back for something only the VM can fix goes the same way: a merge commit,
rewritten commits, a missing final snapshot, or a merge or rebase under way. A session like
this has every repo, so what it changes in one already merged lands too.

### Copying work out of a sandbox

The volume `cave-<id>` can't be read from macOS, so a short-lived container copies it out. `cave merge`
prints this command with your sandbox's id and image filled in. The volume is mounted read-only
(`-v`, as a comma in `$HOME` would split `--mount`), `--entrypoint bash` means nothing from the volume
runs, and only regular files, folders and symlinks come out: no FIFO or device node, and no setuid or
setgid bit. Symlinks stay symlinks. Change the folder if you like, and use the image recorded in
`sandboxes/<id>/meta.json`:

```sh
mkdir -p "$HOME/cave-rescue/<id>" && container run --rm --user root --entrypoint bash -v cave-<id>:/workspace:ro -v "$HOME/cave-rescue/<id>":/out <image> -o pipefail -c 'cd /workspace && find . -type d -empty -print0 | xargs -0 -r -I{} mkdir -p -- /out/{} && find . \( -type f -o -type l \) -print0 | xargs -0 -r cp -P --parents -t /out -- && find /out ! -type l -perm /6000 -exec chmod ug-s -- {} +'
```

Treat everything that comes out as untrusted: the VM wrote it. Don't run it, and don't run git in a
copied repo, as its `.git` can hold hooks and config. Symlinks can point anywhere, so inspect before following them.

### Inside a running sandbox

When `main` moves, cave delivers it to every running sandbox holding that repo, after any
landing, on any cave command, and every 30 seconds while a `cave` session runs in the
foreground. Only the clone's `origin` moves. Claude is told through hooks, and rebases
when it suits.

Claude can ask for another repo or for a review. `cave ls` shows how many requests are
waiting, and nothing happens until you say yes to each in `cave requests`. A repo it
asks for must be under the sandbox's root. `cave add` takes any repo, as you typed it.
`cave ls` also shows whether Claude is working or idle, and how many files are
uncommitted. All of that comes from the VM, so treat it as a hint.

### Cleaning up

`cave prune` also runs on every launch. It deletes sandboxes over 30 days old, unless
running, along with their volumes, stopped containers and refs. It decides by the
timestamp in the id. Session transcripts have UUID names with no timestamp, so for those
it uses the modification time. It leaves Claude's `memory/` folders alone.

**This deletes unlanded work.** From a week before that, every cave command prints a
warning naming each stopped sandbox at risk and its unlanded repos. Run `cave merge` or
`cave discard` to settle it.

## Stopping a sandbox

Ending Claude normally has no time limit: the VM commits leftovers and exits. To stop
a sandbox from another terminal, even after the one that started it has closed, use
`cave stop`. It gives Claude 5 seconds to exit, kills whatever is still running, and
commits leftovers, with 20 seconds allowed in total. A plain `container stop`, or macOS
shutting down, allows only 5 seconds, so Claude is killed at once and the commit runs
straight away. That is best-effort: a heavy commit can overrun, and the sandbox then
shows as `crashed`, with its work safe on the volume for a recovery VM.

## Config repo

The config repo is at `~/.config/cave/config`, or at `$CAVE_CONFIG`. It is a local
plugin marketplace, and `cave init` creates it:

```
.claude-plugin/marketplace.json
plugins/cave-config/       skills, agents, commands, hooks, .mcp.json, assets
cave-settings.json         merged into the VM's ~/.claude/settings.json; cave's required settings win
host-settings.json         not used by cave; symlink the host's ~/.claude/settings.json to it
CLAUDE.md                  copied to the VM's ~/.claude
Dockerfile                 your image layer, FROM cave-base:latest
```

To use the same config on the host:
`claude plugin marketplace add ~/.config/cave/config`.
To share host settings, symlink `~/.claude/settings.json` to `host-settings.json`. Host Claude
Code writes to that file, so those writes land in the config checkout: commit or revert
them before `cave merge` can land into it.

Each sandbox gets its own clone of this repo and loads every folder in `plugins/`.
Changes come back through `cave merge`, like any other repo's, and the host loads them
from the next session. Claude wrote them in an unrestricted VM, so review them closely,
especially `plugins/`, `cave-settings.json`, `host-settings.json` and `Dockerfile`, which the report flags through
the `cave.flagged` git config that `cave init` sets.

Images rebuild when the `Dockerfile` changes. They are tagged `cave:<build date>-<hash>`,
and each launch deletes those built over 7 days ago, except the image it is using, any
a running sandbox uses, and any a sandbox started from, which recovery needs.

cave always overrides three settings: `sandbox.enabled: false`,
`enableAllProjectMcpServers: true` and `skipDangerousModePermissionPrompt: true`.

MCP servers must be unauthenticated. Define them in the plugin's `.mcp.json`, and
preinstall stdio servers in the `Dockerfile` (arm64 builds).

## Settings

| Variable          | Default                 |                               |
| ----------------- | ----------------------- | ----------------------------- |
| `CAVE_CONFIG`     | `~/.config/cave/config` | Config repo path              |
| `CAVE_CPUS`       | `4`                     | CPUs per VM                   |
| `CAVE_MEMORY`     | `4G`                    | Memory per VM                 |
| `CAVE_MAX_BUNDLE` | `1073741824`            | Largest bundle taken, in bytes |

## Files

State lives under `~/Library/Application Support/cave/`:

| Path                                | |
| ----------------------------------- | ---------------------------------------------- |
| `sandboxes/<id>/meta.json`          | Host-only sandbox details                      |
| `sandboxes/<id>/inbox/`             | Bundles for the VM, mounted read-only          |
| `sandboxes/<id>/outbox/`            | What the VM reports, mounted rw, never trusted |
| `sandboxes/<id>/staging/`           | Host-only, for files on their way in or out    |
| `sessions/<project>-<hash>/`        | Transcripts, at `~/.claude/projects`           |

The clones are on the named volume `cave-<id>`, mounted at `/workspace`. It outlives the
container, and macOS can't read it, so getting anything from it takes a VM.

## Security

cave requires git 2.50.1 or newer, the version its bundle safety was tested on.

The VM can write anything to its clones, including `.git`, so the host never runs git
against one, never pushes into one, and can't even read one. Everything crosses as a git
bundle. The host bundles only each repo's base branch into a read-only inbox, so no
remote URLs or credentials are needed in the VM. A VM's bundle is copied out of the
outbox, capped in size, verified, and accepted only if it holds exactly the expected
branch, then imported with object checks into `refs/cave`. Anything else the VM says is
a report, which proves nothing, and missing reports never count as "no changes".
All host git commands use `-c core.hooksPath=/dev/null`, and rebases run in a scratch
clone that sees no host git config, so a sandbox's `.gitattributes` has no driver to name.
The host's `~/.claude` is never mounted, and the host's `Claude Code-credentials` item is
never read. The token reaches a session over stdin, so it's in no argv or stored container
config, and recovery VMs don't get it.

Each sandbox runs on a network of its own (`cave-<id>`, deleted with the sandbox), so its VM
cannot reach another sandbox's VM, even through the routed gateway (checked on `container` 1.1.0, with a
control showing that two VMs on one network do reach each other). That is all that is isolated: a VM can still
reach the internet, the host through its network's gateway (so any host service listening on all interfaces,
0.0.0.0). Nothing here is claimed for `container`'s image-builder VM, which was not tested. The VM holds your Claude token while it runs.
It can read the full history of each repo's base branch. Nothing is landed on your behalf
without a command from you, but `--yes` skips the prompt, and the report only shows what
changed: it's on you to read it.

Sessions are shared by every sandbox started from the same directory, and a VM can write
to them, so a session or memory file written by one is read by the next. Discarding a
sandbox does not remove it.

Accepted limits. While a session runs, the VM's output reaches your terminal as it is,
because Claude Code is a full-screen program: escape sequences are not filtered then (what
they can do depends on your terminal, for example setting the title, or the clipboard where
OSC 52 is on). Everything cave prints itself is filtered. The outbox and sessions
mounts are plain host folders with no quota, so a VM can fill the host disk through them
while it runs. Neither APFS nor `container` 1.1.0 can limit a mounted folder, and cave
reads and copies only a capped amount from them. A bundle is bounded
too: the commits' checked-out size is counted before any rebase, and git steps have timeouts.

## Tests

```sh
make tests                        # runs `make unit` and `make integration`
make unit
make integration                  # boots real VMs if `container` is running
make test-integration-container   # real Claude sessions; uses real tokens
```

`make test-integration-container` needs a token from `cave token`, and it asks you
to acknowledge the usage before it starts. It uses `claude-haiku-5-5`, with each
Claude run capped at $0.10. Those tests live in `claude_api_tests/`, outside
`tests/`, so nothing collects them by default. They fail unless
`CAVE_TEST_SPEND_TOKENS=1` is set, which the target does only after you type `yes`.
