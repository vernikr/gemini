"""Install the agent skill into the directories the loaders actually scan.

Why this lives in the package rather than in a shell script in the repo: the skill has to
survive "return in a month, install on a new machine". A script you must clone first does
not; `gemini skill install` does, because it ships inside the wheel next to the file
it copies.

An on-disk manifest records which directories this command manages, so uninstall never
touches an unrelated skill.
"""
from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

SKILL_NAME = "gemini"
# This filename is persistent state shared by install, status, and uninstall.
MANIFEST = ".gemini-mcp-skill.manifest"
ALTERNATE_SKILL_NAMES = ("gemini-mcp",)

# Loader order, later overriding earlier. ~/.agents/skills is the interoperable one
# (Codex, Cursor, Copilot, Gemini CLI, OpenCode, Freebuff); ~/.claude/skills is the only
# directory Claude Code reads. Installing to both makes one command cover every host.
TARGETS = (Path.home() / ".agents" / "skills", Path.home() / ".claude" / "skills")


_PLACEHOLDER = "{brevity_rule}"


def skill_text() -> str:
    """The skill body, read from the installed package data.

    The length policy is a placeholder rather than prose: it has to be the same sentence
    on every surface, and a skill that paraphrases it is exactly the drift the parity
    test exists to catch. Substituted by plain replace, not str.format — the rest of the
    document is prose and may contain braces.
    """
    from .prompts import BREVITY_RULE

    raw = (resources.files("gemini_mcp").joinpath("skill/SKILL.md")).read_text("utf-8")
    assert _PLACEHOLDER in raw, "SKILL.md lost the length-policy placeholder"
    return raw.replace(_PLACEHOLDER, BREVITY_RULE)


def _remove_duplicate_skills(dir_: Path) -> None:
    """Remove alternate skill folders before writing the managed copy.

    This keeps agent loaders from finding competing copies of the same skill.
    """
    manifest = dir_ / MANIFEST
    recorded = manifest.read_text("utf-8").split() if manifest.is_file() else []
    for name in dict.fromkeys((*recorded, *ALTERNATE_SKILL_NAMES)):
        if name == SKILL_NAME:
            continue
        target = dir_ / name
        if target.is_dir():
            shutil.rmtree(target)
            print(f"removed superseded {target}")


def _owned(dir_: Path) -> list[str]:
    """Return managed skill names recorded in this directory's manifest."""
    manifest = dir_ / MANIFEST
    if not manifest.is_file():
        return []
    return [n for n in manifest.read_text("utf-8").split() if (dir_ / n / "SKILL.md").is_file()]


def _installed(dir_: Path) -> bool:
    return bool(_owned(dir_))


def _write_manifest(dir_: Path) -> None:
    (dir_ / MANIFEST).write_text(SKILL_NAME + "\n", encoding="utf-8")


def install(*, print_only: bool = False) -> int:
    body = skill_text()
    for dir_ in TARGETS:
        dest = dir_ / SKILL_NAME
        if print_only:
            print(f"would write {dest}/SKILL.md ({len(body)} chars)")
            continue
        dir_.mkdir(parents=True, exist_ok=True)
        _remove_duplicate_skills(dir_)
        if dest.exists():
            shutil.rmtree(dest)          # a stale copy must not survive an upgrade
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text(body, encoding="utf-8")
        _write_manifest(dir_)
        print(f"installed {dest}/SKILL.md")
    if print_only:
        return 0
    print("\nDesktop agents read skills when a session starts: restart the app and open "
          "a new thread.\nCheck with: gemini skill status")
    return 0


def status() -> int:
    body = skill_text()
    print(f"packaged skill: {len(body)} chars ({SKILL_NAME})")
    for dir_ in TARGETS:
        f = dir_ / SKILL_NAME / "SKILL.md"
        if not dir_.is_dir():
            print(f"  {dir_}  missing")
        elif not f.is_file():
            print(f"  {dir_}  not installed here")
        else:
            ours = " (installed by this command)" if _installed(dir_) else " (NOT ours)"
            stale = f.read_text("utf-8") != body
            same = "  STALE — differs from the packaged copy" if stale else ""
            print(f"  {dir_}  installed{ours}{same}")
    print("\nloader order (later wins): ~/.claude/skills -> ~/.agents/skills -> "
          "<project>/.claude/skills -> <project>/.agents/skills")
    return 0


def uninstall() -> int:
    for dir_ in TARGETS:
        if not _installed(dir_):
            print(f"  {dir_}  nothing installed by this command")
            continue
        # the manifest, not a hardcoded name: remove exactly what we own and nothing more
        for name in (dir_ / MANIFEST).read_text("utf-8").split():
            target = dir_ / name
            if target.is_dir():
                shutil.rmtree(target)
                print(f"  removed {target}")
        (dir_ / MANIFEST).unlink(missing_ok=True)
    return 0


def run(args) -> int:
    action = getattr(args, "skill_action", "")
    print_only = getattr(args, "print_only", False)
    if action == "install":
        return install(print_only=print_only)
    if action == "uninstall":
        return uninstall()
    if action == "status":
        return status()
    # No subcommand. Answer with the state rather than guessing an action — but never
    # silently swallow `--print`, which is a promise that nothing will be changed.
    rc = status()
    print("\nactions: install | status | uninstall")
    return rc
