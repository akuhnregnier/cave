# Working on cave

cave is a macOS CLI that wraps Apple `container` and git. See `README.md` for what it does.

## Commands

```sh
make unit                         # fast: no VM
make integration                  # also boots real VMs; needs `container system start`
make tests                        # both of the above
make test-integration-container   # real Claude sessions; spends real tokens
```

Never run `make test-integration-container` or set `CAVE_TEST_SPEND_TOKENS` unless
the user has asked you to. Tests that spend tokens live in `claude_api_tests/`, never
in `tests/`. They are marked `claude_api`, use `ask_claude`, which fails unless that
variable is set, and stay on the cheap model with a budget cap.

Tests run in parallel through pytest-xdist (`-n auto` in `pyproject.toml`), and the
whole suite should finish in under 3 minutes. Workers and concurrent VMs scale with the
host: see `max_workers` and `max_vms` in `src/cave/_testing/vm.py`. Tests that need a VM
take a slot through the `real_vm` fixture, and images are built once through `build_once`.

Target Python 3.9, the macOS system version, and keep runtime dependencies at zero.

## Layout

- `src/cave/`: one module per concern. `cli.py` dispatches, `launch.py` starts a
  sandbox, `discover.py` finds its repos, `collect.py` handles diff, merge,
  resolve and discard, and `prune.py` cleans up. `transport.py` moves bundles in and out,
  `review.py` is the gate, `target.py` works out which sandbox a command means, `watch.py`
  keeps running sandboxes up to date, and `requests.py` answers what a sandbox asks for.
- `src/cave/assets/image/`: the base image. These files run in the VM, and changing
  them changes the image hash, which triggers a rebuild. Images are tagged
  `cave:<build date>-<hash>`, and launches delete those over 7 days old unless in use
  or recorded by a sandbox, as recovery needs its image.
- `src/cave/assets/config/`: the template that `cave init` copies.
- `src/cave/_testing/`: all test support (fixtures, harness, fakes). `tests/` holds
  only tests. Fixtures load with `-p cave._testing.fixtures`.

## Rules that must hold

- The VM controls everything in a clone, including `.git`. Clones are on a named volume
  the host can only delete, or copy out with the read-only recipe (`sandbox.copy_out_command`),
  treating the result as untrusted. The host must never run git against a clone or push into one. All
  it gets from a VM is the outbox, as untrusted files: bundles go through `transport.py`,
  and reports through `sandbox.open_report`. `container exec` only triggers the companion,
  whose answer arrives as files; the token is the one thing sent, over its stdin. Uncommitted work is snapshotted only once Claude is gone,
  by `cave-companion --final` or in a recovery VM.
- Host git goes through `cave.git`, which sets `core.hooksPath=/dev/null`. Use
  `git.strict` for anything a landing decision depends on: `git.query` returns ''
  on failure, which must never read as "no changes".
- All landing goes through `collect._land_repo`. Never delete a sandbox while a
  repo in it is unlanded, and never drop work without saying so. The exceptions are
  `cave discard`, which is asked for, and prune, which deletes a stopped sandbox past `MAX_AGE`
  even if unlanded, but only `prune.GRACE` (24 hours) after its first warning (`warned` in meta;
  starting a sandbox clears it). Every cave command
  warns first, from `prune.WARN_AHEAD` before, naming the sandbox and its unlanded repos.
- Meta entries are keyed on `clone`. `name` is only for display.
- Stopping must stay within `STOP_SECONDS`: on the USR1 from `container.stop`, the
  entrypoint gives Claude 5 seconds, kills the rest of the session, then commits. Any other
  stop signal skips the 5 seconds. Anything cave stops goes through `container.stop`, unless
  it is being deleted (`sandbox.delete`, and `_take_over`, via `container.force_delete`).
  The companion's whole-volume strays `find` is bounded to fit this budget: on timeout it writes
  no `strays`, and the host keeps the sandbox.
- Every sandbox VM, resume and recovery included, runs on its own `cave-<id>` network. Never fall
  back to the default one (an old sandbox whose meta has no `network` is the one case that does).
  Delete only networks labelled with this state dir (`container.own_networks`).
- `container` queries are time-bounded (`QUERY_SECONDS`), and a hung service raises `CaveError`.
- `git.MIN_VERSION` is the oldest git the bundle safety was tested on. Lower it only
  after re-running the hostile bundle checks on that version.
- Sandbox ids become paths that get deleted, so always go through `paths.box_path`.
- Tokens must stay out of argv: pass them by env var name or over stdin.
- Text from a VM can reach the terminal, so print through `cave.log` (`warn`, `say`, `clean`).
- The sessions mount is VM-writable too. Never follow symlinks in it.
- Prune must never block a launch.

## `container` 1.1.0 bugs

- `container build -q` hangs. Use `--progress plain`.
- `container build` sends an empty context for any path under `/private`, including
  a resolved `$TMPDIR` and pytest's `tmp_path`. Build contexts must live elsewhere,
  which is why `real_vm` uses `~/Library/Caches`.
