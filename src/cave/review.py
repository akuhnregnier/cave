"""The gate: what a sandbox's commits would do to the host, shown before anything lands.

The report comes from git alone, with colour, external diff and textconv off, so whatever
looks odd in it came from the VM. All of it is escaped before anything else sees it,
delta included. Every commit is listed with its own files, since the final diff would
hide a file added and later deleted.
"""
import shutil
import subprocess
import sys
import unicodedata
from collections import namedtuple

from cave import git, log
from cave.errors import CaveError

MAX_LINE = 2000
MAX_PATCH = 16 << 20  # bytes of patch in one report, for the pager and memory
SUMMARY_FLAGS = 12
DELETED, EXECUTABLE, SYMLINK, SUBMODULE = "000000", "100755", "120000", "160000"
DIFF = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=none",
        "--diff-merges=first-parent", "--root", "--no-commit-id", "-r")
PAGERS = ("delta --paging=never | less -FRX", "less -FX")  # constants: nothing from the VM reaches a shell

# Files that take effect without anyone running them, by file or folder name at any depth.
# A Makefile only runs when asked to, so it isn't here. `git config --add cave.flagged <path>` adds more.
FLAGGED = {
    "attributes, which apply on landing and can run filters defined in host config": (".gitattributes",),
    "git": (".gitmodules", ".githooks", ".husky", ".pre-commit-config.yaml", ".lfsconfig"),
    "agents": (".claude", "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".mcp.json", "GEMINI.md", ".gemini", ".cursor",
               ".cursorrules", ".windsurf", ".windsurfrules", ".clinerules", ".continue", ".aider.conf.yml",
               ".github/copilot-instructions.md", ".github/instructions", ".codex", ".agents"),
    "environment": (".envrc", ".mise.toml", "mise.toml", ".tool-versions", ".nvmrc", ".node-version", ".python-version",
                    ".ruby-version", ".java-version", "rust-toolchain", "rust-toolchain.toml",
                    ".npmrc", ".cargo/config.toml"),
    "editors": (".vscode", ".idea", ".zed", ".devcontainer", ".nvim.lua", ".exrc"),
    "ci": (".github/workflows", ".github/actions"),
}

Change = namedtuple("Change", "status path added removed tags")


class Rejected(CaveError):
    pass


def _decode(data):
    return data.decode("utf-8", "surrogateescape")  # log.escape shows the bytes that aren't UTF-8


def _parts(path):
    """A path as compared with the list: macOS filesystems ignore case and normalise."""
    return unicodedata.normalize("NFC", path).casefold().strip("/").split("/")


def _matches(path, pattern):
    parts, wanted = _parts(path), _parts(pattern)
    return any(parts[i:i + len(wanted)] == wanted for i in range(len(parts)))


def _tags(path, old, new, binary, flagged):
    tags = [label for label, patterns in flagged.items() if any(_matches(path, pattern) for pattern in patterns)]
    if SYMLINK in (old, new) and new != DELETED:
        tags.append("symlink")
    if SUBMODULE in (old, new):
        tags.append("submodule")
    if EXECUTABLE in (old, new) and old != new and new != DELETED:
        tags.append(f"executable bit, {old} to {new}")
    return tags + ["binary"] if binary else tags


def _changes(repo, commit, flagged):
    """What one commit touches, parsed by position since a filename can hold anything but NUL."""
    data = git.raw(repo, "diff-tree", *DIFF, "--raw", "--numstat", "-z", commit, limit=git.MAX_LISTING)
    if len(data) > git.MAX_LISTING:  # a cut listing would read as a shorter, complete one
        raise CaveError(f"commit {commit[:12]} changes too many files to list: over {git.MAX_LISTING >> 20} MiB of listing")
    tokens = _decode(data).split("\0")
    raw, index = [], 0
    while tokens[index].startswith(":"):
        old, new, _, _, status = tokens[index][1:].split()
        raw.append((old, new, status, tokens[index + 1]))
        index += 2
    counts = {path: (added, removed) for added, removed, path in
              (token.split("\t", 2) for token in tokens[index:] if token)}
    changes = []
    for old, new, status, path in raw:
        added, removed = counts[path]
        changes.append(Change(status, path, added, removed, _tags(path, old, new, added == "-", flagged)))
    return changes


def _summary(called_out, files, commits, cut=False):
    """The flags in a line or two, to sit next to the prompt where the report may have scrolled away."""
    shown = [f"{tag}: {path}" for tag in sorted(called_out) for path in sorted(called_out[tag])]
    more = f", and {len(shown) - SUMMARY_FLAGS} more above" if len(shown) > SUMMARY_FLAGS else ""
    flags = "; ".join(shown[:SUMMARY_FLAGS]) + more if shown else "nothing"
    note = f" PATCHES CUT at {MAX_PATCH >> 20} MiB, so the report is NOT complete." if cut else ""
    return f"{files} file changes in {commits} commits.{note} flagged: {flags}"


def _report(meta, clone, start, tip, patch, note, summary=None):
    """The escaped report and what escaping found. A list passed as summary gets the one-line summary."""
    found, files = set(), 0

    def field(data):
        text, hits = log.escape(data, one_line=True)
        found.update(hits)
        return text

    flagged = {**FLAGGED, "flagged in git config": git.config_values(clone.repo, "cave.flagged")}
    commits = git.strict(clone.repo, "rev-list", "--reverse", f"{start}..{tip}").split()
    body, called_out, budget, cut = [f"commits, {note}:"], {}, MAX_PATCH, False
    for commit in commits:
        title = git.raw(clone.repo, "log", "-1", "--no-color", "--no-show-signature", "--format=%h %an <%ae>  %s", commit)
        body.append(field(_decode(title).rstrip("\n")))
        for change in _changes(clone.repo, commit, flagged):
            tags = "".join(f"  [{tag}]" for tag in change.tags)
            files += 1
            body.append(f"    {change.status} {field(change.path)}  +{change.added} -{change.removed}{tags}")
            for tag in change.tags:
                called_out.setdefault(tag, set()).add(field(change.path))
        if patch:
            data = git.raw(clone.repo, "diff-tree", *DIFF, "-p", commit, limit=budget)
            if len(data) > budget:
                data, budget, cut = data[:budget], 0, True
                data += f"\n[cave: patch cut here at {MAX_PATCH >> 20} MiB, the rest of the patches are not shown]".encode()
            body.append(_decode(data))
            budget = max(budget - len(data), 0)
    head = [f"sandbox {field(str(meta.get('name', '?')))}", f"root    {field(str(meta.get('root', '?')))}",
            f"repo    {field(clone.name)} ({field(str(clone.repo))})", f"base    {field(clone.base)}"]
    if cut:
        head.append(f"warning: patches are cut at {MAX_PATCH >> 20} MiB, so this report is NOT complete (the file list is)")
    head.append("flagged:" if called_out else "flagged: nothing")
    for tag in sorted(called_out):
        head += [f"  {tag}:"] + [f"    {path}" for path in sorted(called_out[tag])]
    text, hits = log.escape("\n".join(head + body), MAX_LINE)
    if summary is not None:
        summary.append(_summary(called_out, files, len(commits), cut))
    return text, sorted(found.union(hits))


def on_terminal():
    return all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))


def _page(text):
    pager = PAGERS[0] if shutil.which("delta") else PAGERS[1]
    subprocess.run(["sh", "-c", pager], input=text.encode(), check=False)


def show(meta, clone, start, tip, patch, note):
    """Print the report for start..tip, with its warning line first and again at the end.
    Returns the summary of what is flagged."""
    summary = []
    text, findings = _report(meta, clone, start, tip, patch, note, summary)
    warning = log.warning_line(findings)
    text = f"{warning}\n{text}" if warning else text
    if patch and sys.stdout.isatty():
        _page(text)
    else:
        print(text, flush=True)
    if warning:
        log.warn(warning)
    return summary[0]


def ask(question):
    """Only a typed yes counts."""
    print(log.clean(f"{question} [y/N] "), end="", file=sys.stderr, flush=True)
    try:
        return sys.stdin.readline().strip().lower() in ("y", "yes")
    except KeyboardInterrupt:
        return False


def gate(meta, clone, onto, rebased, yes, patch):
    """Show what landing would do, and return only if it may go ahead."""
    summary = show(meta, clone, onto, rebased, patch, f"rebased onto {clone.base} at {onto[:12]}")
    print(log.clean(f"cave: {summary}"), file=sys.stderr, flush=True)  # next to the prompt, past any scrollback
    if yes:
        return
    if not on_terminal():
        raise CaveError("nothing here can approve it. Run cave merge in a terminal, or pass --yes to land "
                        "without review. Everything is kept")
    if not ask(f"land this on {clone.base} in {clone.repo}?"):
        raise Rejected("you rejected it, so its fetched commits were discarded. The sandbox is kept")
