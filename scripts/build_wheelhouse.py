"""Build an offline "wheelhouse" for Argus: the argus-screens wheel plus every pinned
runtime dependency wheel for Windows x64 (win_amd64), CPython 3.12 and 3.13.

    python scripts/build_wheelhouse.py                       # build argus from this checkout
    python scripts/build_wheelhouse.py --argus-wheel X.whl   # use an existing wheel (e.g. the PyPI one)
    python scripts/build_wheelhouse.py --dry-run             # print the plan; no build, no network
    python scripts/build_wheelhouse.py --verify DIR          # re-check a wheelhouse against its SHA256SUMS

Run it on a machine that can reach files.pythonhosted.org (and, unless --argus-wheel is
given, PyPI for the uv_build backend). Copy the output folder to the locked-down machine
and install from it with no network; see docs/enterprise-install.md.

What it does not do: it never resolves anything. The wheels come from uv.lock, so the
versions are exactly the ones CI tested, and every download is checked against the
sha256 that uv.lock records before it is kept. Standard library only; uv is needed to
build the argus wheel.

Output folder (default dist/wheelhouse):
    *.whl             argus-screens and its dependencies
    requirements.txt  every wheel pinned with its sha256, for `uv pip install --require-hashes`
    INSTALL.txt       the two install commands
    SHA256SUMS        sha256 of all of the above (sha256sum / Get-FileHash compatible)
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_PACKAGE = "argus-screens"
PLATFORM_TAGS = {"win_amd64", "any"}
DEFAULT_PYTHONS = ("3.12", "3.13")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ------------------------------------------------------------------ environment markers
#
# uv.lock puts a PEP 508 marker on each dependency edge that is platform or Python
# specific (e.g. `sys_platform == 'win32'`). Only the shapes uv.lock actually emits are
# supported: `and` / `or` of `variable <op> 'literal'` comparisons, no parentheses.
# Anything else stops the build rather than guessing, since a wrong guess would silently
# drop a dependency from the wheelhouse.

_ATOM = re.compile(r"^\s*([a-z_]+)\s*(==|!=|<=|>=|<|>)\s*(['\"])(.*?)\3\s*$")
_VERSION_VARS = {"python_full_version", "python_version"}


def target_env(python: str) -> dict[str, str]:
    """Marker variables for 64-bit Windows CPython `python` (e.g. "3.13")."""
    return {
        "sys_platform": "win32", "platform_system": "Windows", "os_name": "nt", "platform_machine": "AMD64",
        "platform_python_implementation": "CPython", "implementation_name": "cpython",
        "python_version": python, "python_full_version": f"{python}.0",
    }


def _version(text: str) -> tuple[int, ...]:
    parts = tuple(int(p) for p in text.split("."))
    return parts + (0,) * (3 - len(parts))


def evaluate_marker(marker: str | None, env: dict[str, str]) -> bool:
    if not marker:
        return True
    if "(" in marker or ")" in marker:
        raise SystemExit(f"Unsupported marker with parentheses in uv.lock: {marker!r}")
    for alternative in marker.split(" or "):
        results = []
        for atom in alternative.split(" and "):
            m = _ATOM.match(atom)
            if not m or m.group(1) not in env:
                raise SystemExit(f"Unsupported marker {atom.strip()!r} in uv.lock (extend scripts/build_wheelhouse.py)")
            var, op, _, literal = m.groups()
            left, right = (_version(env[var]), _version(literal)) if var in _VERSION_VARS else (env[var], literal)
            results.append({"==": left == right, "!=": left != right, "<": left < right,
                            "<=": left <= right, ">": left > right, ">=": left >= right}[op])
        if all(results):
            return True
    return False


# ---------------------------------------------------------------------------- wheels


@dataclass(frozen=True)
class Wheel:
    package: str
    version: str
    filename: str
    url: str
    sha256: str
    size: int | None


def wheel_rank(filename: str, minor: int) -> tuple[int, int] | None:
    """How well a wheel fits 64-bit Windows CPython 3.<minor>: None if it can't be
    installed there, else a sortable preference (version-specific beats abi3 beats
    pure Python, as pip and uv order them). Free-threaded builds (`cp313t`) are not matched."""
    stem = filename.removesuffix(".whl").split("-")
    if len(stem) < 5:
        return None
    pythons, abis, platforms = (t.split(".") for t in stem[-3:])
    best: tuple[int, int] | None = None
    for platform in platforms:
        if platform not in PLATFORM_TAGS:
            continue
        for py in pythons:
            for abi in abis:
                rank = None
                if py == "py3" or (py.startswith("py3") and py[3:].isdigit() and int(py[3:]) <= minor):
                    if abi == "none":
                        rank = (0, 0)
                elif py.startswith("cp3") and py[3:].isdigit():
                    py_minor = int(py[3:])
                    if abi == "abi3" and py_minor <= minor:
                        rank = (1, py_minor)
                    elif py_minor == minor and abi in (py, "none"):
                        rank = (2, py_minor)
                if rank and (best is None or rank > best):
                    best = rank
    return best


def read_lock(lock_path: Path) -> dict[str, dict]:
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    return {normalize(p["name"]): p for p in data["package"]}


def runtime_packages(packages: dict[str, dict], python: str) -> set[str]:
    """Names reachable from argus-screens' runtime dependencies on Windows CPython `python`."""
    env = target_env(python)
    if ROOT_PACKAGE not in packages:
        raise SystemExit(f"{ROOT_PACKAGE} not found in uv.lock")
    wanted: set[str] = set()
    todo = [ROOT_PACKAGE]
    while todo:
        pkg = packages[todo.pop()]
        edges = list(pkg.get("dependencies", []))
        for dep in list(edges):
            for extra in dep.get("extra", []):
                edges += packages[normalize(dep["name"])].get("optional-dependencies", {}).get(extra, [])
        for dep in edges:
            name = normalize(dep["name"])
            if name not in wanted and evaluate_marker(dep.get("marker"), env):
                wanted.add(name)
                todo.append(name)
    return wanted


def plan_wheels(packages: dict[str, dict], pythons: tuple[str, ...]) -> list[Wheel]:
    chosen: dict[str, Wheel] = {}
    for python in pythons:
        minor = int(python.split(".")[1])
        for name in sorted(runtime_packages(packages, python)):
            pkg = packages[name]
            ranked = []
            for w in pkg.get("wheels", []):
                filename = w["url"].rsplit("/", 1)[1]
                rank = wheel_rank(filename, minor)
                if rank is not None:
                    ranked.append((rank, filename, w))
            if not ranked:
                raise SystemExit(
                    f"{name} {pkg['version']} has no win_amd64 (or pure-Python) wheel for CPython {python} in uv.lock. "
                    "Building from source isn't supported for an offline install."
                )
            _, filename, w = max(ranked, key=lambda r: r[:2])
            chosen[filename] = Wheel(name, pkg["version"], filename, w["url"], w["hash"].removeprefix("sha256:"), w.get("size"))
    return sorted(chosen.values(), key=lambda w: (w.package, w.filename))


# ------------------------------------------------------------------------- files


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(wheel: Wheel, folder: Path) -> str:
    dest = folder / wheel.filename
    if dest.exists() and sha256_of(dest) == wheel.sha256:
        return "cached"
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(wheel.url, headers={"User-Agent": "argus-build-wheelhouse"})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as out:  # honours HTTPS_PROXY
        for block in iter(lambda: response.read(1 << 20), b""):
            digest.update(block)
            out.write(block)
    if digest.hexdigest() != wheel.sha256:
        part.unlink(missing_ok=True)
        raise SystemExit(f"sha256 mismatch for {wheel.filename}: uv.lock says {wheel.sha256}, got {digest.hexdigest()}")
    part.replace(dest)
    return "downloaded"


def build_argus_wheel(out: Path) -> Path:
    if shutil.which("uv") is None:
        raise SystemExit("uv is not on PATH (needed to build argus-screens). Install it, or pass --argus-wheel.")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["uv", "build", "--wheel", "--out-dir", tmp, str(ROOT)], check=True)
        built = list(Path(tmp).glob("*.whl"))
        if len(built) != 1:
            raise SystemExit(f"expected one wheel from `uv build`, found {[b.name for b in built]}")
        return Path(shutil.copy2(built[0], out / built[0].name))


def requirements_text(wheels: list[Wheel], argus: Path | None) -> str:
    """`name==version` plus every hash of the wheels chosen for it, ready for
    `uv pip install --require-hashes -r`. No markers: the list is already filtered
    for Windows x64 and the target Pythons."""
    hashes: dict[tuple[str, str], list[str]] = {}
    for w in wheels:
        hashes.setdefault((w.package, w.version), []).append(w.sha256)
    if argus is not None:
        name, version = argus.name.split("-")[:2]
        hashes[(normalize(name), version)] = [sha256_of(argus)]
    lines = ["# Generated by scripts/build_wheelhouse.py. Install with:",
             "#   uv pip install --require-hashes --no-index --find-links . -r requirements.txt"]
    for (name, version), digests in sorted(hashes.items()):
        lines.append(f"{name}=={version} \\")
        lines.append(" \\\n".join(f"    --hash=sha256:{d}" for d in sorted(set(digests))))
    return "\n".join(lines) + "\n"


INSTALL_TXT = """\
Argus offline install (Windows x64, Python 3.12 or 3.13 already on the machine).
Full guide: docs/enterprise-install.md in the Argus repository.

1. Verify the files (PowerShell, run inside this folder; prints nothing if all match):
   Get-Content SHA256SUMS | ForEach-Object {
       $h, $f = $_ -split ' \\*'
       if ((Get-FileHash $f -Algorithm SHA256).Hash.ToLower() -ne $h) { "MISMATCH: $f" } }

2. Install, using the system Python and no network:
   $env:UV_PYTHON_PREFERENCE = "only-system"
   uv tool install --offline --no-index --find-links . argus-screens

   Strict alternative that also enforces every hash in requirements.txt:
   uv venv "$env:LOCALAPPDATA\\Argus\\venv"
   uv pip install --python "$env:LOCALAPPDATA\\Argus\\venv\\Scripts\\python.exe" --offline --no-index --find-links . --require-hashes -r requirements.txt

3. Then: argus install   (strict alternative: "$env:LOCALAPPDATA\\Argus\\venv\\Scripts\\argus.exe" install)
"""


def write_sums(folder: Path) -> list[str]:
    names = sorted(p.name for p in folder.iterdir() if p.is_file() and p.name != "SHA256SUMS" and not p.name.endswith(".part"))
    lines = [f"{sha256_of(folder / n)} *{n}" for n in names]
    (folder / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return names


def verify(folder: Path) -> int:
    sums = folder / "SHA256SUMS"
    if not sums.exists():
        print(f"no SHA256SUMS in {folder}", file=sys.stderr)
        return 1
    listed, bad = set(), 0
    for line in sums.read_text(encoding="ascii").splitlines():
        digest, _, name = line.partition(" *")
        listed.add(name)
        target = folder / name
        if not target.exists():
            print(f"MISSING:  {name}")
            bad += 1
        elif sha256_of(target) != digest:
            print(f"MISMATCH: {name}")
            bad += 1
    for extra in sorted({p.name for p in folder.iterdir() if p.is_file()} - listed - {"SHA256SUMS"}):
        print(f"UNLISTED: {extra}")
        bad += 1
    print(f"{len(listed)} files checked, {bad} problem(s)")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "wheelhouse", help="output folder (default: dist/wheelhouse)")
    parser.add_argument("--argus-wheel", type=Path, help="use this argus-screens wheel instead of building one")
    parser.add_argument("--python", dest="pythons", action="append", metavar="3.X",
                        help=f"target CPython version; repeatable (default: {' '.join(DEFAULT_PYTHONS)})")
    parser.add_argument("--dry-run", action="store_true", help="print what would be built; no build, no network")
    parser.add_argument("--verify", type=Path, metavar="DIR", help="verify DIR against its SHA256SUMS and exit")
    args = parser.parse_args()

    if args.verify:
        return verify(args.verify)

    pythons = tuple(args.pythons or DEFAULT_PYTHONS)
    wheels = plan_wheels(read_lock(ROOT / "uv.lock"), pythons)
    total = sum(w.size or 0 for w in wheels)
    print(f"{len(wheels)} dependency wheels for win_amd64, CPython {', '.join(pythons)} (about {total / 1e6:.1f} MB):")
    for w in wheels:
        print(f"  {w.filename}")
    if args.dry_run:
        print(f"dry run: would also build argus-screens and write everything to {args.out}")
        return 0

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("argus_screens-*.whl"):
        stale.unlink()  # one argus version per wheelhouse, or `uv` could pick the wrong one
    for i, w in enumerate(wheels, 1):
        print(f"[{i}/{len(wheels)}] {w.filename}: {download(w, out)}")

    if args.argus_wheel:
        argus = Path(shutil.copy2(args.argus_wheel, out / args.argus_wheel.name))
    else:
        argus = build_argus_wheel(out)
    print(f"argus wheel: {argus.name}")

    (out / "requirements.txt").write_text(requirements_text(wheels, argus), encoding="utf-8", newline="\n")
    (out / "INSTALL.txt").write_text(INSTALL_TXT, encoding="utf-8", newline="\n")
    names = write_sums(out)
    print(f"wrote SHA256SUMS ({len(names)} files) in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
