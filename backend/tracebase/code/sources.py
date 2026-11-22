"""Repository discovery and file access for plain folders, Git clones and .zip archives."""
from __future__ import annotations

import hashlib
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pathspec

from ..config import settings

SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", "__pycache__", ".venv", "venv", "env",
    ".idea", ".vscode", ".next", ".nuxt", "target", "coverage", ".cache", ".pytest_cache",
    ".mypy_cache", "bower_components", ".gradle", "Pods", ".terraform", "storage/framework",
}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock", "poetry.lock",
              "Cargo.lock", "Gemfile.lock", "go.sum", ".DS_Store"}
SKIP_EXT = {".min.js", ".min.css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
            ".gz", ".tar", ".jar", ".class", ".so", ".dll", ".exe", ".woff", ".woff2", ".ttf", ".eot",
            ".mp4", ".mp3", ".webp", ".svg", ".sqlite", ".db", ".bin", ".pyc", ".o", ".a", ".lock"}
PROJECT_MARKERS = {"package.json", "composer.json", "pyproject.toml", "requirements.txt", "go.mod",
                   "Cargo.toml", "pom.xml", "build.gradle", "Gemfile", "setup.py", "Makefile",
                   "Dockerfile", "docker-compose.yml", "artisan", "manage.py", ".gitignore"}


@dataclass
class FileInfo:
    path: str  # repo-relative, forward slashes
    mtime: float
    size: int


def _skip_name(name: str) -> bool:
    low = name.lower()
    return name in SKIP_FILES or any(low.endswith(e) for e in SKIP_EXT)


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


class DirSource:
    kind = "folder"

    def __init__(self, name: str, root: Path) -> None:
        self.name, self.root = name, root
        if (root / ".git").exists():
            self.kind = "git"
        gi = root / ".gitignore"
        lines = gi.read_text(errors="replace").splitlines() if gi.exists() else []
        self.spec = pathspec.PathSpec.from_lines("gitignore", lines)

    def files(self) -> Iterator[FileInfo]:
        max_bytes = settings.max_file_kb * 1024
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = os.path.relpath(dirpath, self.root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                           and not self.spec.match_file(f"{rel_dir}/{d}/".lstrip("/"))]
            for fn in filenames:
                rel = f"{rel_dir}/{fn}".lstrip("/")
                if _skip_name(fn) or self.spec.match_file(rel):
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, fn))
                except OSError:
                    continue
                if st.st_size > max_bytes or st.st_size == 0:
                    continue
                yield FileInfo(rel, st.st_mtime, st.st_size)

    def read(self, rel: str) -> str | None:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            return None
        p = (self.root / rel).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            return None
        try:
            data = p.read_bytes()
        except OSError:
            return None
        if _looks_binary(data):
            return None
        return data.decode("utf-8", "replace")


class ZipSource:
    kind = "zip"

    def __init__(self, name: str, root: Path) -> None:
        self.name, self.root = name, root
        self._prefix = self._common_prefix()

    def _common_prefix(self) -> str:
        try:
            with zipfile.ZipFile(self.root) as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
        except zipfile.BadZipFile:
            return ""
        if not names:
            return ""
        first = names[0].split("/")[0] + "/"
        return first if all(n.startswith(first) for n in names) and len(names) > 0 and "/" in names[0] else ""

    def files(self) -> Iterator[FileInfo]:
        max_bytes = settings.max_file_kb * 1024
        try:
            z = zipfile.ZipFile(self.root)
        except zipfile.BadZipFile:
            return
        with z:
            mtime = self.root.stat().st_mtime
            for info in z.infolist():
                if info.is_dir():
                    continue
                rel = info.filename[len(self._prefix):] if info.filename.startswith(self._prefix) else info.filename
                parts = rel.split("/")
                if any(p in SKIP_DIRS for p in parts[:-1]) or _skip_name(parts[-1]):
                    continue
                if info.file_size > max_bytes or info.file_size == 0:
                    continue
                yield FileInfo(rel, mtime, info.file_size)

    def read(self, rel: str) -> str | None:
        try:
            with zipfile.ZipFile(self.root) as z:
                data = z.read(self._prefix + rel)
        except (KeyError, zipfile.BadZipFile, OSError):
            return None
        if _looks_binary(data):
            return None
        return data.decode("utf-8", "replace")


Source = DirSource | ZipSource


def _is_project(d: Path) -> bool:
    try:
        names = {p.name for p in d.iterdir()}
    except OSError:
        return False
    return bool(names & PROJECT_MARKERS) or ".git" in names


def discover(root: Path | None = None, max_depth: int = 3) -> dict[str, Source]:
    """Find repos under the repos dir. Name = path relative to root (e.g. 'freelance/clinic-app')."""
    root = root or settings.repos_dir
    found: dict[str, Source] = {}
    if not root.exists():
        return found

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP_DIRS:
                continue
            rel = e.relative_to(root).as_posix()
            if e.is_file() and e.suffix.lower() == ".zip":
                found[rel] = ZipSource(rel, e)
            elif e.is_dir():
                if e.name.endswith(".git") and (e / "HEAD").exists():
                    continue  # bare mirror, handled by git history only
                if _is_project(e) or depth >= max_depth:
                    found[rel] = DirSource(rel, e)
                else:
                    has_code_files = any(f.is_file() and not f.name.startswith(".") and f.suffix.lower() != ".zip" for f in e.iterdir())
                    has_sub = any(f.is_dir() for f in e.iterdir())
                    if has_code_files and not has_sub:
                        found[rel] = DirSource(rel, e)
                    else:
                        walk(e, depth + 1)

    walk(root, 1)
    return found


def get_source(repo: str) -> Source | None:
    root = settings.repos_dir
    # Block "../" traversal but allow repos that are symlinks (e.g. links to folders elsewhere on the NAS):
    # compare the *normalised* path, not the fully resolved one.
    if not repo or Path(repo).is_absolute() or ".." in Path(repo).parts:
        return None
    from ..privacy import is_excluded_repo
    from ..team import can
    if not can(repo) or is_excluded_repo(repo):  # team mode: outside this member's repos / forgotten repo
        return None
    p = root / repo
    if not os.path.normpath(p).startswith(os.path.normpath(root) + os.sep):
        return None
    if p.is_file() and p.suffix.lower() == ".zip":
        return ZipSource(repo, p)
    if p.is_dir():
        return DirSource(repo, p)
    return None


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
