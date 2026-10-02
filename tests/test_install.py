import pytest

from argus.desktop import ArgusError
from argus.install import _load_jsonc, is_temporary


def test_temporary_install_locations_are_refused():
    assert is_temporary(r"C:\Users\a\AppData\Local\uv\cache\archive-v0\x\Scripts\argus-mcp.exe")
    assert is_temporary("C:/Users/a/AppData/Local/Temp/x/argus-mcp.exe")
    assert not is_temporary(r"C:\Users\a\.local\bin\argus-mcp.exe")
    assert not is_temporary(r"D:\code\argus\.venv\Scripts\argus-mcp.exe")


def test_jsonc_with_comments_and_trailing_commas(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        '{\n  // user servers\n  "servers": {\n    "a": {"command": "x", "args": ["//not-a-comment"],},\n  },\n  /* block */\n}\n',
        encoding="utf-8",
    )
    assert _load_jsonc(path) == {"servers": {"a": {"command": "x", "args": ["//not-a-comment"]}}}


def test_missing_or_empty_files_are_empty_configs(tmp_path):
    assert _load_jsonc(tmp_path / "nope.json") == {}
    (tmp_path / "empty.json").write_text("  ", encoding="utf-8")
    assert _load_jsonc(tmp_path / "empty.json") == {}


def test_broken_json_is_reported_not_overwritten(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"servers": {', encoding="utf-8")
    with pytest.raises(ArgusError, match="isn't valid JSON"):
        _load_jsonc(path)


def test_install_defaults_to_prompting():
    from argus.cli import build_parser

    assert build_parser().parse_args(["install"]).trust is False
    assert build_parser().parse_args(["install", "--trust"]).trust is True
