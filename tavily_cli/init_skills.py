"""Install Tavily's maintained agent skills.

The skills.sh CLI (``npx skills``) is preferred when Node.js is available, so
installs track the latest skills and are counted on skills.sh. The pinned,
checksum-verified archive is the fallback for machines without Node.js.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

import httpx

SKILLS_REPO = "tavily-ai/skills"
SKILLS_COMMIT = "778122e5f9c680f541eeceda5a5b36405eb7980c"
SKILLS_ARCHIVE_SHA256 = "c6d426ef1bb364d3538967d9fe24be4a248ff3ce9b984439f948b6dcf27383f0"
SKILLS_SOURCE = f"{SKILLS_REPO}@{SKILLS_COMMIT}"
SKILLS_ARCHIVE_URL = f"https://codeload.github.com/{SKILLS_REPO}/tar.gz/{SKILLS_COMMIT}"
# Exact version: npx runs whatever this resolves to with the user's permissions.
NPX_SKILLS_PACKAGE = "skills@1.7.4"
CORE_SKILLS = (
    "tavily-best-practices",
    "tavily-cli",
    "tavily-crawl",
    "tavily-dynamic-search",
    "tavily-extract",
    "tavily-map",
    "tavily-research",
    "tavily-search",
)

_MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
_MAX_FILE_BYTES = 5 * 1024 * 1024
_NPX_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class AgentSpec:
    marker: Path
    skills_dir: Path
    # Agents that load ``~/.agents/skills`` directly need no per-agent link.
    reads_shared_dir: bool = False


@dataclass(frozen=True)
class SkillsInstallResult:
    agents: tuple[str, ...]
    installed: int
    updated: int
    unchanged: int
    linked: int
    restart_required: bool
    installer: str = "archive"

    @property
    def total(self) -> int:
        return self.installed + self.updated + self.unchanged


def agent_specs(home: Path | None = None) -> dict[str, AgentSpec]:
    """Return supported agent locations rooted at the current user's home."""
    root = home or Path.home()
    if home is None:
        claude_root = Path(os.environ.get("CLAUDE_CONFIG_DIR", root / ".claude")).expanduser()
        codex_root = Path(os.environ.get("CODEX_HOME", root / ".codex")).expanduser()
    else:
        claude_root = root / ".claude"
        codex_root = root / ".codex"
    return {
        "claude-code": AgentSpec(claude_root, claude_root / "skills"),
        "codex": AgentSpec(codex_root, codex_root / "skills", reads_shared_dir=True),
        "cursor": AgentSpec(root / ".cursor", root / ".cursor" / "skills", reads_shared_dir=True),
    }


def _linked_skill_dirs(agents: tuple[str, ...], home: Path | None = None) -> tuple[Path, ...]:
    """Skill directories that need a link because the agent does not read ``~/.agents/skills``."""
    specs = agent_specs(home)
    return tuple(specs[name].skills_dir for name in agents if not specs[name].reads_shared_dir)


def detect_agents(home: Path | None = None) -> tuple[str, ...]:
    """Detect supported agents from their existing configuration directories."""
    return tuple(name for name, spec in agent_specs(home).items() if spec.marker.is_dir())


def download_skills_archive(*, client: httpx.Client | None = None) -> bytes:
    """Download the canonical skills archive with a conservative size limit."""
    http = client or httpx.Client(timeout=30.0, follow_redirects=True)
    close = client is None
    try:
        chunks: list[bytes] = []
        size = 0
        with http.stream("GET", SKILLS_ARCHIVE_URL) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > _MAX_ARCHIVE_BYTES:
                    raise ValueError("Tavily skills archive exceeds the 25 MB safety limit.")
                chunks.append(chunk)
        archive = b"".join(chunks)
        if hashlib.sha256(archive).hexdigest() != SKILLS_ARCHIVE_SHA256:
            raise ValueError("Downloaded Tavily skills archive failed integrity verification.")
        return archive
    finally:
        if close:
            http.close()


def install_agent_skills(agents: tuple[str, ...]) -> SkillsInstallResult:
    """Install skills with ``npx skills`` when possible, else from the pinned archive."""
    npx = shutil.which("npx")
    if npx and agents:
        result = _install_with_npx(npx, agents)
        if result is not None:
            return result
    return install_skills(agents)


def _install_with_npx(npx: str, agents: tuple[str, ...]) -> SkillsInstallResult | None:
    """Run the skills.sh CLI non-interactively; return None so the caller can fall back."""
    shared_root = Path.home() / ".agents" / "skills"
    link_dirs = _linked_skill_dirs(agents)
    before = _snapshot_skills(shared_root)
    existing_links = {d / name for d in link_dirs for name in CORE_SKILLS if (d / name).exists()}

    # Keep every argument free of quotes and spaces: on Windows npx is a .cmd
    # shim, so the command line is re-parsed by cmd.exe.
    command = [
        npx, "--yes", NPX_SKILLS_PACKAGE, "add", SKILLS_REPO,
        "--skill", "*", "--global", "--agent", *agents, "--yes",
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_NPX_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not verify_skills(agents):
        return None

    after = _snapshot_skills(shared_root)
    installed = sum(1 for name in CORE_SKILLS if before[name] is None)
    updated = sum(1 for name in CORE_SKILLS if before[name] is not None and before[name] != after[name])
    linked = sum(1 for d in link_dirs for name in CORE_SKILLS if d / name not in existing_links)
    return SkillsInstallResult(
        agents=agents,
        installed=installed,
        updated=updated,
        unchanged=len(CORE_SKILLS) - installed - updated,
        linked=linked,
        restart_required=installed > 0 or updated > 0 or linked > 0,
        installer="npx",
    )


def _snapshot_skills(shared_root: Path) -> dict[str, str | None]:
    return {
        name: _directory_digest(shared_root / name) if (shared_root / name).is_dir() else None
        for name in CORE_SKILLS
    }


def install_skills(
    agents: tuple[str, ...],
    *,
    home: Path | None = None,
    archive: bytes | None = None,
) -> SkillsInstallResult:
    """Install the eight core skills from the pinned archive and expose them to selected agents.

    Archive scripts and references are copied as files but are never executed.
    Only the exact Tavily skill names in ``CORE_SKILLS`` are managed.
    """
    root = home or Path.home()
    specs = agent_specs(home)
    unsupported = sorted(set(agents) - set(specs))
    if unsupported:
        raise ValueError(f"Unsupported agent(s): {', '.join(unsupported)}")

    shared_root = root / ".agents" / "skills"
    shared_root.mkdir(parents=True, exist_ok=True)

    installed = 0
    updated = 0
    unchanged = 0
    linked = 0
    archive_data = archive if archive is not None else download_skills_archive()

    with tempfile.TemporaryDirectory(prefix="tavily-skills-") as temp_dir:
        extracted_root = Path(temp_dir)
        _extract_core_skills(archive_data, extracted_root)
        _preflight_install(shared_root, _linked_skill_dirs(agents, home))

        for skill_name in CORE_SKILLS:
            source = extracted_root / skill_name
            destination = shared_root / skill_name
            state = _replace_if_changed(source, destination)
            if state == "installed":
                installed += 1
            elif state == "updated":
                updated += 1
            else:
                unchanged += 1

        for skills_dir in _linked_skill_dirs(agents, home):
            skills_dir.mkdir(parents=True, exist_ok=True)
            for skill_name in CORE_SKILLS:
                if _expose_skill(shared_root / skill_name, skills_dir / skill_name):
                    linked += 1

    changed = installed > 0 or updated > 0 or linked > 0
    return SkillsInstallResult(
        agents=agents,
        installed=installed,
        updated=updated,
        unchanged=unchanged,
        linked=linked,
        restart_required=changed,
    )


def verify_skills(agents: tuple[str, ...], *, home: Path | None = None) -> bool:
    """Verify shared skill files and the links for agents that need them are present."""
    root = home or Path.home()
    specs = agent_specs(home)
    if any(name not in specs for name in agents):
        return False
    shared_root = root / ".agents" / "skills"
    skill_dirs = (shared_root, *_linked_skill_dirs(agents, home))
    return all((d / skill_name / "SKILL.md").is_file() for d in skill_dirs for skill_name in CORE_SKILLS)


def _extract_core_skills(archive_data: bytes, destination: Path) -> None:
    """Extract only approved skill directories from an untrusted tar archive."""
    found: set[str] = set()
    total_file_bytes = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz")
    except tarfile.TarError as exc:
        raise ValueError("Downloaded Tavily skills archive is invalid.") from exc

    with archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            parts = path.parts
            if path.is_absolute() or ".." in parts or len(parts) < 4:
                continue
            if parts[1] != "skills" or parts[2] not in CORE_SKILLS:
                continue

            skill_name = parts[2]
            relative_parts = parts[3:]
            if not relative_parts:
                continue
            if any(part in ("", ".", "..") or "\\" in part or ":" in part for part in relative_parts):
                raise ValueError(f"Tavily skill {skill_name} contains an unsafe archive path.")
            target = destination / skill_name / Path(*relative_parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"Tavily skill {skill_name} contains an unsupported archive entry.")
            if member.size > _MAX_FILE_BYTES:
                raise ValueError(f"Tavily skill {skill_name} contains a file larger than 5 MB.")
            total_file_bytes += member.size
            if total_file_bytes > _MAX_ARCHIVE_BYTES:
                raise ValueError("Extracted Tavily skills exceed the 25 MB safety limit.")

            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Could not read {member.name} from the Tavily skills archive.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())
            # Skill payloads are data consumed by agents, not trusted programs.
            # Never propagate executable bits supplied by the archive.
            target.chmod(0o644)
            found.add(skill_name)

    missing = [name for name in CORE_SKILLS if name not in found or not (destination / name / "SKILL.md").is_file()]
    if missing:
        raise ValueError(f"Tavily skills archive is missing: {', '.join(missing)}")


def _directory_digest(path: Path) -> str:
    digest = hashlib.sha256()
    entries = sorted(path.rglob("*"))
    if any(item.is_symlink() for item in entries):
        raise ValueError(f"Cannot compare a skill containing links: {path}")
    for file_path in (item for item in entries if item.is_file()):
        relative = file_path.relative_to(path).as_posix().encode("utf-8")
        content = file_path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update((file_path.stat().st_mode & 0o111).to_bytes(2, "big"))
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _replace_if_changed(source: Path, destination: Path) -> str:
    existed = destination.exists() or destination.is_symlink()
    if destination.is_dir() and not destination.is_symlink():
        if _directory_digest(source) == _directory_digest(destination):
            return "unchanged"
    elif existed:
        raise ValueError(f"Cannot install Tavily skill over non-directory path: {destination}")

    token = uuid4().hex
    staging = destination.parent / f".{destination.name}.tavily-new-{token}"
    backup = destination.parent / f".{destination.name}.tavily-backup-{token}"

    shutil.copytree(source, staging)
    try:
        if existed:
            destination.replace(backup)
        staging.replace(destination)
    except Exception:
        if not destination.exists() and backup.exists():
            backup.replace(destination)
        raise
    else:
        if backup.exists() or backup.is_symlink():
            try:
                _remove_path(backup)
            except OSError:
                pass
    finally:
        if staging.exists() or staging.is_symlink():
            _remove_path(staging)
    return "updated" if existed else "installed"


def _expose_skill(source: Path, destination: Path) -> bool:
    if destination.is_symlink():
        if destination.resolve(strict=False) == source.resolve():
            return False
        raise ValueError(f"Cannot replace existing skill link: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError(f"Cannot replace non-directory skill path: {destination}")
        return _replace_if_changed(source, destination) != "unchanged"

    relative_source = os.path.relpath(source, start=destination.parent)
    try:
        destination.symlink_to(relative_source, target_is_directory=True)
    except OSError:
        shutil.copytree(source, destination)
    return True


def _preflight_install(shared_root: Path, link_dirs: tuple[Path, ...]) -> None:
    """Reject path conflicts before changing any installed skill."""
    for skill_name in CORE_SKILLS:
        destination = shared_root / skill_name
        if (destination.exists() or destination.is_symlink()) and (
            destination.is_symlink() or not destination.is_dir()
        ):
            raise ValueError(f"Cannot install Tavily skill over non-directory path: {destination}")

    for skills_dir in link_dirs:
        for skill_name in CORE_SKILLS:
            source = shared_root / skill_name
            destination = skills_dir / skill_name
            if destination.is_symlink():
                if destination.resolve(strict=False) != source.resolve(strict=False):
                    raise ValueError(f"Cannot replace existing skill link: {destination}")
            elif destination.exists() and not destination.is_dir():
                raise ValueError(f"Cannot replace non-directory skill path: {destination}")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
