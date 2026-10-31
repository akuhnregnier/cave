"""cave: run Claude Code in a disposable Linux micro-VM, one per sandbox (macOS).

  cave [run] [--name x] [--] [claude args]   start a sandbox from the current git repo
  cave ls                  list sandboxes, with what each running one is doing
  cave diff [-p] [target]  fetch a sandbox's work and show its commits and files
  cave stop [target]       stop a running sandbox, committing its work
  cave merge [-p] [--yes] [target]
                           review its work and land it on main; a stopped sandbox is then deleted
  cave resolve [target]    have Claude rebase a repo that could not land onto the current main
  cave discard [target]    delete it without merging
  cave add <path> [target] give a running sandbox a copy of another repo
  cave requests [target]   answer what a running sandbox has asked for
  cave prune               delete everything older than 30 days, landed or not
  cave token               create and store a long-lived Claude token
  cave build               rebuild the images
  cave init                scaffold the config repo

A target is a sandbox id or name, or a unique start of either. Without one, cave uses
the sandbox that holds the current repo, or that covers the current folder.
"""
import argparse
import subprocess
import sys

from cave import auth, collect, configrepo, container, git, image, launch, requests, sandbox, target, watch
from cave.errors import CaveError
from cave.log import clean, warn
from cave.prune import prune, warn_expiring


def parser():
    top = argparse.ArgumentParser(prog="cave", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = top.add_subparsers(dest="command", required=True)
    for name in ("ls", "prune", "token", "init"):
        commands.add_parser(name)
    for name in ("stop", "resolve", "discard", "requests"):
        commands.add_parser(name).add_argument("target", nargs="?")
    add = commands.add_parser("add")
    add.add_argument("path")
    add.add_argument("target", nargs="?")
    diff, merge = commands.add_parser("diff"), commands.add_parser("merge")
    for command in (diff, merge):
        command.add_argument("target", nargs="?")
        command.add_argument("-p", "--patch", action="store_true", help="show line-by-line diffs too")
    merge.add_argument("--force", action="store_true", help="delete a stopped sandbox despite work outside its repos")
    merge.add_argument("--yes", action="store_true", help="land without asking: the report still prints")
    commands.add_parser("build").add_argument("--no-cache", action="store_true")
    return top


def _launch_args(args):
    """(label, claude args) from `[--name x] [--] claude args`. All after `--` is Claude's."""
    end = args.index("--") if "--" in args else len(args)
    run = argparse.ArgumentParser(prog="cave run", add_help=False, allow_abbrev=False)
    run.add_argument("--name")
    known, rest = run.parse_known_args(args[:end])
    rest += args[end:]
    return known.name, rest[1:] if rest[:1] == ["--"] else rest


def _command(args):
    sid = None
    if args.command in ("diff", "stop", "merge", "resolve", "discard", "add", "requests"):
        sid = target.resolve(args.target, always_show=args.command == "discard")
    if args.command == "merge":
        return 0 if collect.merge(sid, args.force, args.yes, args.patch) else 1
    if args.command == "resolve":
        return collect.resolve(sid)
    if args.command == "diff":
        return 0 if collect.diff(sid, args.patch) else 1
    if args.command == "stop":
        collect.stop(sid)
    elif args.command == "discard":
        collect.discard(sid)
    elif args.command == "add":
        requests.add(sid, args.path)
    elif args.command == "requests":
        requests.walk(sid)
    elif args.command == "ls":
        listing = sandbox.table()
        if listing:
            print(clean(listing))
    elif args.command == "prune":
        prune()
    elif args.command == "token":
        auth.setup()
    elif args.command == "init":
        configrepo.init()
    elif args.command == "build":
        container.ensure_system()
        print(image.ensure(force=True, no_cache=args.no_cache))
    return 0


def dispatch(argv):
    if not argv or argv[0] in ("run", "--", "--name") or argv[0].startswith("--name="):
        git.require_version()
        label, claude_args = _launch_args(argv[1:] if argv[:1] == ["run"] else argv)
        warn_expiring()  # first, as Claude is about to take the terminal
        return launch.run(claude_args, label)
    args = parser().parse_args(argv)
    git.require_version()
    try:
        return _command(args)
    finally:
        watch.sweep()  # last, so that what a merge just landed goes out with it
        warn_expiring()  # and what it just landed needs no warning


def main():
    if sys.platform != "darwin":
        sys.exit("cave: macOS only")
    try:
        return dispatch(sys.argv[1:])
    except CaveError as error:
        warn(str(error))
    except subprocess.CalledProcessError as error:
        warn(f"command failed: {' '.join(map(str, error.cmd))}")
    except OSError as error:
        warn(str(error))
    return 1
