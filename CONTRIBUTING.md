# Contributing to Argus

Thanks for helping. Bug reports, feature ideas and pull requests are all welcome.

## Set up

```powershell
git clone https://github.com/sahildayal/argus
cd argus
uv sync                                   # creates .venv with Python 3.13 (uv-managed)
uv tool install --editable . --managed-python --python 3.13   # optional: global argus / argus-mcp / argus-tray
```

## Tests

```powershell
uv run pytest            # unit tests: fake desktops, never touch your screen
uv run pytest -m live    # opens throwaway "Argus Test" windows and captures only those
```

CI runs the unit tests on `windows-latest`. Please run the live tests locally if you
change capture, OCR, tray or server code.

## Ground rules

- **Tests never capture the real screen.** Unit tests build fake desktops
  (`tests/conftest.py`); live tests capture only their own topmost test windows
  (`tests/live_window.py`) or regions inside them. A contributor's screen can show
  anything, so keep it that way.
- **Privacy defaults stay conservative.** Redaction happens before an image leaves
  Argus, the pause switch must block every tool, and `argus install` keeps
  per-capture prompts on unless the user passes `--trust`.
- **Errors are for the AI to act on.** Raise `ArgusError` with a message that says
  what to do next (other exceptions reach the model as a generic failure).
- **Win32 stays in `win32.py`.** It also makes the process per-monitor DPI aware on
  import, so it has to be imported before anything that touches the screen.
- Windows only, for now. A macOS/Linux backend would live behind the same
  `desktop.py` / `capture.py` interfaces. Open an issue first if you want to try.

## Layout

| Module | Job |
| --- | --- |
| `win32.py` | ctypes bindings, DPI awareness, `PrintWindow`, monitor names |
| `desktop.py` | snapshot of monitors and windows, host detection, monitor/window selectors |
| `layout.py` | monitor numbering and position words |
| `capture.py` | screen crops (mss/GDI) and offscreen rendering of covered windows |
| `privacy.py` | redaction, pause, retention |
| `imaging.py` | resizing for the AI, grid, mouse marker, change boxes, encoding |
| `looking.py` + `focus.py` | how `look` decides; focus history and clipboard watcher |
| `snips.py`, `marks.py` | the user's own captures |
| `ocr.py` | Windows.Media.Ocr via pywinrt |
| `testing.py` | diffs, baselines, `wait_for` |
| `store.py` | capture registry, background PNG writes |
| `service.py` | everything the tools do; `server.py` (MCP), `cli.py` and `tray.py` are thin front ends |
| `install.py` | registering with AI tools, startup shortcut, `doctor` |

## Releasing

1. Bump `version` in `pyproject.toml` (and `src/argus/__init__.py`), then commit.
2. Publish a GitHub release tagged `vX.Y.Z` (for example `gh release create v0.2.0 --generate-notes`).
3. The `Publish to PyPI` workflow checks that the tag matches the version, runs the tests, builds,
   and uploads with PyPI Trusted Publishing. No API token is stored anywhere.

The demo GIF is regenerated with `uv run python docs/make_demo.py`. It opens its own demo windows and
captures only those. Pass `--ask-claude` to refresh the cached Claude reply.
