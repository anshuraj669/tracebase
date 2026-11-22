"""Small terminal helpers: colors, a spinner, colored unified diffs and prompts. No dependencies."""
from __future__ import annotations

import difflib
import itertools
import os
import sys
import threading
import time

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def bold(t: str) -> str: return c(t, "1")
def dim(t: str) -> str: return c(t, "2")
def green(t: str) -> str: return c(t, "32")
def red(t: str) -> str: return c(t, "31")
def yellow(t: str) -> str: return c(t, "33")
def cyan(t: str) -> str: return c(t, "36")
def teal(t: str) -> str: return c(t, "38;5;37")


def header(text: str) -> None:
    print(f"\n{teal('●')} {bold(text)}")


def info(text: str) -> None:
    print(f"  {text}")


def warn(text: str) -> None:
    print(f"  {yellow('!')} {text}")


def error(text: str) -> None:
    print(f"{red('✗')} {text}", file=sys.stderr)


class Spinner:
    """`with Spinner("Searching your projects"):` — animated on a TTY, a single line otherwise."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._stop = threading.Event()
        self._t0 = time.time()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Spinner":
        if sys.stderr.isatty():
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            print(f"  {self.text}…", file=sys.stderr)
        return self

    def _spin(self) -> None:
        for ch in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if self._stop.is_set():
                break
            sys.stderr.write(f"\r  {teal(ch)} {self.text}… {dim(f'{time.time() - self._t0:.0f}s')}  ")
            sys.stderr.flush()
            time.sleep(0.1)

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()

    @property
    def elapsed(self) -> float:
        return time.time() - self._t0


def diff(path: str, old: str | None, new: str, max_lines: int = 400) -> str:
    a = (old or "").splitlines(keepends=False)
    b = new.splitlines(keepends=False)
    lines = list(difflib.unified_diff(a, b, fromfile=f"a/{path}" if old is not None else "/dev/null",
                                      tofile=f"b/{path}", lineterm="", n=3))
    out = []
    for ln in lines[:max_lines]:
        if ln.startswith(("+++", "---")):
            out.append(bold(ln))
        elif ln.startswith("@@"):
            out.append(cyan(ln))
        elif ln.startswith("+"):
            out.append(green(ln))
        elif ln.startswith("-"):
            out.append(red(ln))
        else:
            out.append(ln)
    if len(lines) > max_lines:
        out.append(dim(f"… {len(lines) - max_lines} more diff lines (use [e] to open the full file)"))
    return "\n".join(out)


def ask(prompt: str, choices: str, default: str) -> str:
    """Single-letter prompt, e.g. ask("Apply?", "ynaeq", "y")."""
    shown = "/".join(ch.upper() if ch == default else ch for ch in choices)
    while True:
        try:
            ans = input(f"{prompt} [{shown}] ").strip().lower()
        except EOFError:
            return "q"
        if not ans:
            return default
        if ans[0] in choices:
            return ans[0]
