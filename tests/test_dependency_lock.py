"""The lock file must describe the environment that actually runs the tests.

A lock file is only worth anything if it is checked. Left alone it silently
rots: pyproject.toml's open ranges keep resolving to newer code (praw has already
gone 7.x -> 8.0.3 here), and the lock keeps claiming versions nobody uses. So:

  * every declared dependency must be pinned in the lock, and
  * every pinned version must match what is actually installed.

This is a reproducibility guard, not a security scanner. It does not check for
known CVEs - no advisory database is consulted, and no network is used.
"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "requirements-lock.txt"
PYPROJECT = REPO / "pyproject.toml"

# Local packages are installed from this repository, not from an index.
LOCAL = {"news-tracker", "tracker-core"}


def _pinned() -> dict[str, str]:
    if not LOCK.is_file():
        pytest.fail("requirements-lock.txt is missing: the build is not reproducible")
    pins = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;]+)", line)
        assert match, f"lock line is not an exact pin: {line!r}"
        name, version = match.group(1).lower().replace("_", "-"), match.group(2)
        assert name not in pins, f"{name} is pinned twice"
        pins[name] = version
    return pins


def _declared() -> dict[str, str]:
    text = PYPROJECT.read_text(encoding="utf-8")
    deps = {}
    for group in re.finditer(r"dependencies\s*=\s*\[(.*?)\]", text, re.S):
        for entry in re.findall(r'"([^"]+)"', group.group(1)):
            name = re.split(r"[<>=!~[]", entry)[0].strip().lower().replace("_", "-")
            if name:
                deps[name] = entry
    return deps


def test_every_declared_dependency_is_pinned_exactly():
    pins = _pinned()
    missing = sorted(name for name in _declared() if name not in LOCAL and name not in pins)
    assert not missing, f"declared but not pinned, so installs are not reproducible: {missing}"


def test_lock_pins_are_exact_not_ranges():
    """A range in the lock is the whole problem this file exists to remove."""
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"lock entry is not pinned exactly: {line!r}"
        operators = (">=", "<=", "~=", "!=", ">", "<")
        assert not any(op in line for op in operators), (
            f"lock entry contains a range operator: {line!r}")


def test_installed_versions_match_the_lock():
    import importlib.metadata as metadata

    pins = _pinned()
    mismatched, absent = [], []
    for name, pinned in sorted(pins.items()):
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            # Every pin must be installed. CI installs the lock itself
            # (pip install -r requirements-lock.txt), so a pin being absent here
            # means the environment was not built from the lock at all.
            absent.append(name)
            continue
        if installed != pinned:
            mismatched.append(f"{name}: lock says {pinned}, installed {installed}")
    assert not absent, f"lock pins packages that are not installed: {absent}"
    assert not mismatched, ("the lock no longer describes the environment under test: "
                            + "; ".join(mismatched))
def test_lock_does_not_pin_local_packages():
    """Pinning a local package to a version invites installing it from an index."""
    pins = _pinned()
    assert not (set(pins) & LOCAL), "local repo packages must not be pinned in the lock"


def test_pyproject_still_declares_open_ranges():
    """If this ever fails, pyproject became strict and the lock may be redundant.

    Kept as an explicit, reviewable signal rather than a silent assumption: a
    strict pyproject plus a lock is a maintenance decision, not an accident.
    """
    open_ranges = [entry for entry in _declared().values()
                   if any(op in entry for op in (">=", "~=", "<", "!="))]
    assert open_ranges, ("pyproject no longer uses open ranges - decide whether to keep "
                         "both the ranges and the lock, or drop one")