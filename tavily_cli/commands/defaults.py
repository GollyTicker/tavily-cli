"""Make Tavily the default web tool by disabling agents' built-in web search."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from tavily_cli.common import json_option

CLAUDE_DENIED_TOOLS = ("WebSearch", "WebFetch")
CODEX_WEB_SEARCH_LINE = 'web_search = "disabled"'

_TOML_TABLE_HEADER = re.compile(r"^\s*\[")
_TOML_WEB_SEARCH = re.compile(r"^\s*web_search\s*=")
_TOML_WEB_SEARCH_DISABLED = re.compile(r"""^\s*web_search\s*=\s*["']disabled["']\s*(#.*)?$""")


def claude_settings_path() -> Path:
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
    return root / "settings.json"


def codex_config_path() -> Path:
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return root / "config.toml"


# ── Claude Code: ~/.claude/settings.json ─────────────────────────────


def _load_claude_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(settings, dict):
        raise click.ClickException(f"{path} must contain a JSON object.")
    return settings


def _deny_list(settings: dict[str, Any], path: Path) -> list[Any]:
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise click.ClickException(f'"permissions" in {path} must be an object.')
    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        raise click.ClickException(f'"permissions.deny" in {path} must be an array.')
    return deny


def _write_claude_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def set_claude_default(path: Path) -> bool:
    """Deny Claude Code's built-in web tools. Returns True if the file changed."""
    settings = _load_claude_settings(path)
    deny = _deny_list(settings, path)
    missing = [tool for tool in CLAUDE_DENIED_TOOLS if tool not in deny]
    if not missing:
        return False
    deny.extend(missing)
    _write_claude_settings(path, settings)
    return True


def unset_claude_default(path: Path) -> bool:
    """Remove the web-tool denials added by set-default. Returns True if the file changed."""
    settings = _load_claude_settings(path)
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict) or not isinstance(permissions.get("deny"), list):
        return False
    deny = permissions["deny"]
    kept = [tool for tool in deny if tool not in CLAUDE_DENIED_TOOLS]
    if len(kept) == len(deny):
        return False
    if kept:
        permissions["deny"] = kept
    else:
        del permissions["deny"]
        if not permissions:
            del settings["permissions"]
    _write_claude_settings(path, settings)
    return True


# ── Codex: ~/.codex/config.toml ──────────────────────────────────────


def _top_level_end(lines: list[str]) -> int:
    """Index of the first table header; top-level keys must appear before it."""
    for index, line in enumerate(lines):
        if _TOML_TABLE_HEADER.match(line):
            return index
    return len(lines)


def set_codex_default(path: Path) -> bool:
    """Disable Codex's built-in web search. Returns True if the file changed."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    end = _top_level_end(lines)

    for index in range(end):
        if _TOML_WEB_SEARCH.match(lines[index]):
            if _TOML_WEB_SEARCH_DISABLED.match(lines[index].rstrip("\r\n")):
                return False
            lines[index] = CODEX_WEB_SEARCH_LINE + "\n"
            break
    else:
        lines.insert(0, CODEX_WEB_SEARCH_LINE + "\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return True


def unset_codex_default(path: Path) -> bool:
    """Remove the web_search setting added by set-default. Returns True if the file changed."""
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    end = _top_level_end(lines)
    kept = [
        line for index, line in enumerate(lines)
        if index >= end or not _TOML_WEB_SEARCH_DISABLED.match(line.rstrip("\r\n"))
    ]
    if len(kept) == len(lines):
        return False
    path.write_text("".join(kept), encoding="utf-8")
    return True


# ── Commands ─────────────────────────────────────────────────────────


_TARGETS: dict[str, tuple[str, Callable[[], Path]]] = {
    "claude": ("Claude Code", claude_settings_path),
    "codex": ("Codex", codex_config_path),
}


def _select_targets(claude: bool, codex: bool, verb: str, json_output: bool) -> list[str]:
    if claude or codex:
        return [name for name, flag in (("claude", claude), ("codex", codex)) if flag]
    if json_output or not sys.stdin.isatty():
        raise click.UsageError("Pass --claude and/or --codex in non-interactive runs.")
    return [
        name for name, (label, _) in _TARGETS.items()
        if click.confirm(f"{verb} for {label}?", default=True)
    ]


def _run(
    targets: list[str],
    actions: dict[str, Callable[[Path], bool]],
    *,
    changed_message: str,
    unchanged_message: str,
    json_output: bool,
) -> None:
    results = []
    for name in targets:
        label, path_fn = _TARGETS[name]
        path = path_fn()
        changed = actions[name](path)
        results.append({"agent": name, "label": label, "path": str(path), "changed": changed})

    if json_output:
        click.echo(json.dumps({"ok": True, "results": results}, sort_keys=True))
        return

    from tavily_cli.theme import console

    if not results:
        console.print("  [dim]Nothing selected.[/dim]")
        return
    for result in results:
        message = changed_message if result["changed"] else unchanged_message
        mark = "[#9BC0AE]✓[/#9BC0AE]" if result["changed"] else "[dim]•[/dim]"
        console.print(f"  {mark} {result['label']}: {message} [dim]({result['path']})[/dim]")
    if any(result["changed"] for result in results):
        console.print("  [dim]Restart the agent for the change to take effect.[/dim]")


@click.command("set-default")
@click.option("--claude", is_flag=True, help="Deny Claude Code's built-in WebSearch and WebFetch tools.")
@click.option("--codex", is_flag=True, help="Disable Codex's built-in web search.")
@json_option
def set_default(claude: bool, codex: bool, json_output: bool) -> None:
    """Make Tavily the default web tool by disabling agents' built-in web search."""
    targets = _select_targets(claude, codex, "Make Tavily the default web tool", json_output)
    _run(
        targets,
        {"claude": set_claude_default, "codex": set_codex_default},
        changed_message="built-in web search disabled",
        unchanged_message="built-in web search already disabled",
        json_output=json_output,
    )


@click.command("unset-default")
@click.option("--claude", is_flag=True, help="Re-allow Claude Code's built-in WebSearch and WebFetch tools.")
@click.option("--codex", is_flag=True, help="Re-enable Codex's built-in web search.")
@json_option
def unset_default(claude: bool, codex: bool, json_output: bool) -> None:
    """Undo set-default and restore agents' built-in web search."""
    targets = _select_targets(claude, codex, "Restore built-in web search", json_output)
    _run(
        targets,
        {"claude": unset_claude_default, "codex": unset_codex_default},
        changed_message="built-in web search restored",
        unchanged_message="nothing to undo",
        json_output=json_output,
    )
