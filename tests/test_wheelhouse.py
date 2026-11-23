"""scripts/build_wheelhouse.py: the parts that decide *what* goes in an offline
install. No network, no build: they read the real uv.lock and temp folders."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_wheelhouse.py"
spec = importlib.util.spec_from_file_location("build_wheelhouse", SCRIPT)
bw = importlib.util.module_from_spec(spec)
sys.modules["build_wheelhouse"] = bw  # dataclasses needs the module registered
spec.loader.exec_module(bw)


def test_markers_follow_the_target_environment():
    win313, win312 = bw.target_env("3.13"), bw.target_env("3.12")
    assert bw.evaluate_marker(None, win313)
    assert bw.evaluate_marker("sys_platform == 'win32'", win313)
    assert not bw.evaluate_marker("sys_platform == 'darwin'", win313)
    assert bw.evaluate_marker("sys_platform != 'emscripten'", win313)
    assert bw.evaluate_marker("sys_platform == 'linux' or sys_platform == 'win32'", win313)
    assert not bw.evaluate_marker("sys_platform == 'win32' and platform_python_implementation == 'PyPy'", win313)
    assert bw.evaluate_marker("python_full_version < '3.13'", win312)
    assert not bw.evaluate_marker("python_full_version < '3.13'", win313)
    assert bw.evaluate_marker("python_full_version < '3.15'", win313)


def test_markers_it_does_not_understand_stop_the_build():
    env = bw.target_env("3.13")
    with pytest.raises(SystemExit):
        bw.evaluate_marker("extra == 'cli'", env)
    with pytest.raises(SystemExit):
        bw.evaluate_marker("(sys_platform == 'win32')", env)


@pytest.mark.parametrize(
    ("filename", "minor", "usable"),
    [
        ("mss-10.2.0-py3-none-any.whl", 13, True),
        ("pystray-0.19.5-py2.py3-none-any.whl", 12, True),
        ("pillow-12.3.0-cp313-cp313-win_amd64.whl", 13, True),
        ("pillow-12.3.0-cp312-cp312-win_amd64.whl", 13, False),  # wrong interpreter
        ("pillow-12.3.0-cp313-cp313-win32.whl", 13, False),  # 32-bit
        ("pillow-12.3.0-cp313-cp313-win_arm64.whl", 13, False),
        ("pillow-12.3.0-cp313-cp313-manylinux_2_28_x86_64.whl", 13, False),
        ("psutil-7.2.2-cp37-abi3-win_amd64.whl", 12, True),
        ("psutil-7.2.2-cp313-cp313t-win_amd64.whl", 13, False),  # free-threaded build
        ("cryptography-50.0.2-cp311-abi3-win_amd64.whl", 13, True),
        ("cryptography-50.0.2-cp315-abi3.abi3t-win_amd64.whl", 13, False),  # needs a newer Python
        ("x-1-cp313-none-win_amd64.whl", 13, True),
    ],
)
def test_wheel_compatibility_for_windows_x64(filename, minor, usable):
    assert (bw.wheel_rank(filename, minor) is not None) is usable


def test_version_specific_wheels_beat_abi3_beats_pure_python():
    rank = bw.wheel_rank
    assert rank("x-1-cp313-cp313-win_amd64.whl", 13) > rank("x-1-cp39-abi3-win_amd64.whl", 13) > rank("x-1-py3-none-any.whl", 13)
    assert rank("x-1-cp311-abi3-win_amd64.whl", 13) > rank("x-1-cp39-abi3-win_amd64.whl", 13)


@pytest.fixture(scope="module")
def plan():
    return bw.plan_wheels(bw.read_lock(bw.ROOT / "uv.lock"), bw.DEFAULT_PYTHONS)


def test_the_real_lock_yields_a_windows_only_wheel_set(plan):
    names = {w.package for w in plan}
    assert {"mcp", "mss", "pillow", "psutil", "pystray", "pywin32", "winrt-runtime", "winrt-windows-media-ocr"} <= names
    assert not names & {"pyobjc-core", "python-xlib", "httpx2-jsfetch", "pytest", "argus-screens"}
    assert all(w.filename.endswith(".whl") and len(w.sha256) == 64 for w in plan)
    assert all(w.url.startswith("https://") for w in plan)


def test_every_native_package_has_a_wheel_for_each_target_python(plan):
    by_pkg = {}
    for w in plan:
        by_pkg.setdefault(w.package, []).append(w.filename)
    for minor in (12, 13):
        for name, files in by_pkg.items():
            assert any(bw.wheel_rank(f, minor) is not None for f in files), f"{name} has no wheel for 3.{minor}"
    assert len(by_pkg["pillow"]) == 2  # cp312 and cp313 builds
    assert len(by_pkg["psutil"]) == 1  # one abi3 wheel serves both


def test_requirements_pin_every_wheel_with_hashes(plan, tmp_path):
    argus = tmp_path / "argus_screens-9.9.9-py3-none-any.whl"
    argus.write_bytes(b"fake wheel")
    text = bw.requirements_text(plan, argus)
    assert f"argus-screens==9.9.9 \\\n    --hash=sha256:{bw.sha256_of(argus)}" in text
    pillow = [w for w in plan if w.package == "pillow"]
    assert text.count("pillow==") == 1
    assert all(f"--hash=sha256:{w.sha256}" in text for w in pillow)
    assert ";" not in text  # no markers: the list is already filtered for Windows x64


def test_sums_round_trip_and_catch_tampering(tmp_path, capsys):
    (tmp_path / "a.whl").write_bytes(b"one")
    (tmp_path / "requirements.txt").write_text("two", encoding="utf-8")
    assert bw.write_sums(tmp_path) == ["a.whl", "requirements.txt"]
    first = (tmp_path / "SHA256SUMS").read_text(encoding="ascii").splitlines()[0]
    assert first == f"{bw.sha256_of(tmp_path / 'a.whl')} *a.whl"  # `sha256sum -c` binary-mode format
    assert bw.verify(tmp_path) == 0
    (tmp_path / "a.whl").write_bytes(b"changed")
    (tmp_path / "extra.whl").write_bytes(b"added")
    assert bw.verify(tmp_path) == 1
    out = capsys.readouterr().out
    assert "MISMATCH: a.whl" in out and "UNLISTED: extra.whl" in out
