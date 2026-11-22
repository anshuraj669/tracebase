"""Adapt code copied from an old project to the new one, deterministically (no LLM):

  identity     the old project's name in all its spellings (clinic-app, clinic_app, ClinicApp, CLINIC_APP…)
               becomes the new project's name, in the same spelling
  renames      domain words the developer asks for (Doctor=Staff): DoctorController -> StaffController,
               isDoctor -> isStaff, doctors -> staffs, DOCTOR_ROLE -> STAFF_ROLE, file names too
  go module    import paths of the old Go module -> the new project's module path
  java package com.acme.clinic.auth -> the new project's base package (and the file's folder with it)

Every change is reported, so the developer sees exactly what was renamed."""
from __future__ import annotations

import json
import posixpath
import re
from collections import Counter

# ------------------------------------------------------------------ words & spellings


def words(name: str) -> list[str]:
    """'ClinicApp' / 'clinic-app' / 'clinic_app' / 'clinic app' / 'CLINIC_APP' -> ['clinic', 'app']."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    return [w.lower() for w in re.split(r"[\s_\-./]+", name) if w]


def spellings(ws: list[str]) -> dict[str, str]:
    """style -> spelling of the words in that style."""
    cap = [w[:1].upper() + w[1:] for w in ws]
    return {
        "pascal": "".join(cap), "camel": ws[0] + "".join(cap[1:]), "snake": "_".join(ws), "kebab": "-".join(ws),
        "upper": "_".join(ws).upper(), "lower_space": " ".join(ws), "title_space": " ".join(cap),
        "upper_flat": "".join(ws).upper(), "flat": "".join(ws),
    }


def _pattern(spelled: str, style: str) -> str:
    """Regex for one spelling: whole identifier part, allowing a trailing plural 's'/'es' and camelCase joins."""
    plural = r"(?:e?s)?" if style not in ("upper", "upper_flat") else r"(?:E?S)?"
    if style in ("pascal", "title_space"):
        # may follow a lowercase letter (isDoctor) but not continue a lowercase word (Doctorate)
        return rf"(?<![A-Z]){re.escape(spelled)}{plural}(?![a-z])"
    if style in ("upper", "upper_flat"):
        return rf"(?<![A-Z0-9]){re.escape(spelled)}{plural}(?![A-Z0-9])"
    return rf"(?<![A-Za-z0-9]){re.escape(spelled)}{plural}(?![a-z0-9])"


class Renamer:
    """Applies several old->new renames in one pass, keeping each occurrence's spelling style."""

    SINGLE = ("snake", "pascal", "upper")  # for one word: doctor / Doctor / DOCTOR

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self.table: dict[str, tuple[str, str, str]] = {}   # old spelling -> (new spelling, label, style)
        for old, new in pairs:
            ow, nw = words(old), words(new)
            if not ow or not nw or ow == nw:
                continue
            news = spellings(nw)
            for style, spelled in spellings(ow).items():
                if len(ow) == 1 and style not in self.SINGLE:
                    continue
                self.table.setdefault(spelled, (news[style], f"{old} → {new}", style))
        self.parts = sorted(self.table, key=len, reverse=True)
        self.rx = re.compile("|".join(f"(?P<g{i}>{_pattern(s, self.table[s][2])})" for i, s in enumerate(self.parts))) \
            if self.parts else None
        self.counts: Counter = Counter()

    def __call__(self, text: str) -> str:
        if not self.rx:
            return text

        def sub(m: re.Match) -> str:
            old = self.parts[int(m.lastgroup[1:])]
            new, label, _ = self.table[old]
            self.counts[label] += 1
            return new + m.group(0)[len(old):]  # keep the plural suffix
        return self.rx.sub(sub, text)


# ------------------------------------------------------------------ project identity


def _manifest_name(files: dict[str, str]) -> str | None:
    for name, key in (("composer.json", "name"), ("package.json", "name")):
        try:
            v = json.loads(files.get(name) or "{}").get(key)
        except ValueError:
            v = None
        if v:
            return v.split("/")[-1].lstrip("@")
    m = re.search(r'(?m)^name\s*=\s*["\']([^"\']+)', files.get("pyproject.toml") or "")
    if m:
        return m.group(1)
    m = re.search(r"(?m)^module\s+(\S+)", files.get("go.mod") or "")
    if m:
        return m.group(1).rstrip("/").split("/")[-1]
    m = re.search(r"<artifactId>([^<]+)</artifactId>", re.sub(r"(?s)<parent>.*?</parent>|<dependencies>.*", "", files.get("pom.xml") or ""))
    return m.group(1) if m else None


def identity(name: str, files: dict[str, str]) -> list[str]:
    """Names a project goes by: its folder name and the name in its manifest."""
    out = []
    for n in (name.rstrip("/").split("/")[-1], _manifest_name(files)):
        if n and len(n) >= 3 and words(n) and n.lower() not in {"app", "api", "backend", "server", "src", "project"}:
            if words(n) not in [words(o) for o in out]:
                out.append(n)
    return out


def go_module(files: dict[str, str]) -> str | None:
    m = re.search(r"(?m)^module\s+(\S+)", files.get("go.mod") or "")
    return m.group(1) if m else None


def java_base(paths: list[str]) -> str | None:
    """Base package of a Java project, from its file tree (the deepest folder holding all its main classes)."""
    pk = [p.split("src/main/java/", 1)[1].rsplit("/", 1)[0] for p in paths if "src/main/java/" in p and p.endswith(".java")]
    if not pk:
        return None
    common = posixpath.commonpath(pk) if len(pk) > 1 else pk[0]
    return common.replace("/", ".") or None


# ------------------------------------------------------------------ apply to a bundle


def adapt(files: list[dict], old: dict, new: dict, renames: list[tuple[str, str]] | None = None) -> list[str]:
    """Rewrite bundle `files` in place for the new project. `old` / `new`: {"name", "files", "tree"}.
    Returns a summary like ["clinic-app → booking-api (4)", "Go module x → y (6)"]."""
    summary: Counter = Counter()

    # Go: module path prefix in import paths
    om, nm = go_module(old.get("files") or {}), go_module(new.get("files") or {})
    if om and nm and om != nm:
        rx = re.compile(rf'"{re.escape(om)}(/[^"]*)?"')
        for f in files:
            if f["path"].endswith(".go") or f["path"] == "go.mod":
                f["content"], n = rx.subn(lambda m: f'"{nm}{m.group(1) or ""}"', f["content"])
                summary[f"Go module {om} → {nm}"] += n
                for sn in f.get("snippets") or []:
                    sn["line"] = rx.sub(lambda m: f'"{nm}{m.group(1) or ""}"', sn["line"])

    # Java: base package, in code and in the folder layout
    ob, nb = java_base(old.get("tree") or []), java_base(new.get("tree") or [])
    if ob and nb and ob != nb:
        rx = re.compile(rf"\b{re.escape(ob)}(?=[.;\s*])")
        for f in files:
            if f["path"].endswith((".java", ".kt", ".xml", ".properties", ".yml", ".yaml")):
                f["content"], n = rx.subn(nb, f["content"])
                summary[f"Java package {ob} → {nb}"] += n
            od, nd = "src/main/java/" + ob.replace(".", "/"), "src/main/java/" + nb.replace(".", "/")
            for key in ("target",):
                if f[key].startswith(od + "/"):
                    f[key] = nd + f[key][len(od):]

    # project name + the developer's renames
    pairs = []
    olds, news = identity(old.get("name") or "", old.get("files") or {}), identity(new.get("name") or "", new.get("files") or {})
    if olds and news:
        pairs += [(o, news[0]) for o in olds]
    pairs += [(a, b) for a, b in renames or [] if a and b]
    ren = Renamer(pairs)
    for f in files:
        f["content"] = ren(f["content"])
        for s in f.get("snippets") or []:
            s["line"] = ren(s["line"])
            s["before"] = [ren(b) for b in s.get("before") or []]
            if s.get("inside"):
                s["inside"] = ren(s["inside"])
        d, base = posixpath.split(f["target"])
        new_base = ren(base)
        if new_base != base:
            f["target"] = posixpath.join(d, new_base)
    summary.update(ren.counts)
    for f in files:  # the explanation shown next to each file uses the new names too (not counted as changes)
        if f.get("reason"):
            f["reason"] = ren(f["reason"])
    return [f"{k} ({v})" for k, v in summary.items() if v]
