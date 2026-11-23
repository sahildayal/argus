# Installing Argus on a locked-down Windows machine

For corporate laptops where the usual `uv tool install argus-screens --managed-python`
can't work: Python builds can't be downloaded from GitHub, PyPI is slow or blocked, or
you want every machine to run exactly the same, reviewed set of files.

Everything below was run against this repository's `uv.lock` (uv 0.12.5, Windows 11,
Python 3.13). Where a step is a recommendation rather than something that was run, the
text says so.

- [Which path to use](#which-path-to-use)
- [Prerequisites](#prerequisites)
- [Use an installed Python, not a downloaded one](#use-an-installed-python-not-a-downloaded-one)
- [Pinned install from PyPI](#pinned-install-from-pypi)
- [Offline install from a wheelhouse](#offline-install-from-a-wheelhouse)
- [Registering with your AI tools](#registering-with-your-ai-tools)
- [Network access](#network-access)
- [What Argus writes to disk](#what-argus-writes-to-disk)
- [For IT and EDR reviewers](#for-it-and-edr-reviewers)

## Which path to use

| Situation | Path |
| --- | --- |
| PyPI is reachable, but Python can't be downloaded | [Pinned install](#pinned-install-from-pypi) with the system Python |
| The machine has no internet at all, or you must review the exact files first | [Wheelhouse](#offline-install-from-a-wheelhouse), built once on a connected machine |
| No `uv` either | [Wheelhouse with pip](#without-uv-pip-and-venv) |

All of them end with the same two commands: `argus install` and `argus doctor`.

## Prerequisites

- Windows 10 (1903+) or 11, **64-bit x86**. The wheelhouse contains `win_amd64` wheels only.
- **Python 3.12 or 3.13 that is already installed**, from python.org, winget, or your
  IT-managed software center. Not the Microsoft Store build, see below. The tray app uses
  `tkinter` (for the brief outline it flashes on a marked window), so the installer must
  include it: `python -c "import tkinter"` should print nothing.
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (a single `uv.exe`).
  Without it, use the pip route.
- At least one MCP-capable AI tool (see the README for the list `argus install` knows).

### Not the Microsoft Store Python

The Store build runs with its writes under `%APPDATA%` redirected into a private
per-package folder. This was checked here: a script run by a Store-based Python wrote a
file to `%APPDATA%`, saw it there, and PowerShell did not (it had landed under
`%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.<version>\LocalCache\Roaming`).
`argus install` edits Claude Desktop's and VS Code's JSON config under `%APPDATA%`, so
on a Store Python those edits would never reach the apps. `argus install` prints a
warning when it detects this. A virtual environment created from a Store Python is
affected too, because it still runs the Store interpreter.

## Use an installed Python, not a downloaded one

By default uv may fetch a Python build from the internet when it can't find a suitable
one, and `--managed-python` (what the README's quick start uses) forces that. To make uv
use only what is already installed, pick one:

```powershell
# per command
uv tool install --no-managed-python --no-python-downloads ...

# or for the whole PowerShell session
$env:UV_PYTHON_PREFERENCE = "only-system"
$env:UV_PYTHON_DOWNLOADS  = "never"

# or one specific interpreter
uv tool install --python "C:\Program Files\Python313\python.exe" ...
```

To make it permanent for your user, put `python-preference = "only-system"` in
`%APPDATA%\uv\uv.toml` (uv's user-level config). If no installed Python satisfies the
request, uv fails with a message like *No interpreter found for Python 3.12 in search
path or registry* instead of downloading one.

### What the project's own setting did

`pyproject.toml` used to say `[tool.uv] python-preference = "only-managed"`. That setting
**never affected installs**: uv reads `[tool.uv]` from the project you run it in, not from
a package it installs, and `uv tool install` ignores it even when run inside a checkout
(checked with `uv -v tool install`: it searched "managed installations, search path, or
registry", i.e. the default order). It only forced `uv sync` / `uv run` in a clone to use a
downloaded Python, which blocks contributors on locked-down machines for no benefit. It
was removed; CI sets `UV_PYTHON_PREFERENCE=only-managed` itself to stay deterministic.

## Pinned install from PyPI

`uvx argus-screens` and a plain `uv tool install argus-screens` resolve dependencies
fresh from PyPI **and ignore `uv.lock`**, so two teammates can end up with different
dependency versions. To get the versions CI tested, give uv the constraints file in this
repository, [`constraints/requirements-lock.txt`](../constraints/requirements-lock.txt).
It is exported from `uv.lock` (versions plus file hashes) and CI fails if it goes stale.

Use the copy from the same release as the version you install, for example the file at
tag `v<version>` of the repository, so the pins match that release's dependencies.

```powershell
uv tool install argus-screens==<version> `
    --constraints constraints\requirements-lock.txt `
    --no-managed-python --no-python-downloads
uv tool update-shell        # once, if ~\.local\bin isn't on your PATH; then open a new terminal
argus --version
```

Compared with the lock, this installed exactly the same version of every one of the
41 dependencies. uv records the pins in the tool's receipt
(`%APPDATA%\uv\tools\argus-screens\uv-receipt.toml`), so the environment stays on them
for later `uv tool` operations too.

**What this does not do:** `uv tool install` takes the *versions* from the constraints file
but does **not** enforce the hashes in it. Test: a constraints file with a deliberately wrong
hash for a package installed without complaint, and `UV_REQUIRE_HASHES=1` made no
difference. (`uv tool install` has no `--require-hashes` option.) If you need hash
enforcement, use the wheelhouse below, where `uv pip install --require-hashes` is
available and was tested to reject a tampered hash.

`uvx argus-screens` is fine to try Argus but not to install it: see
[Registering with your AI tools](#registering-with-your-ai-tools).

### Updating

Pick the new version and its constraints file, then run the same command with `--reinstall`.
`uv tool upgrade` won't do it: the receipt pins `argus-screens==<old version>`.

## Offline install from a wheelhouse

A wheelhouse is a folder holding the Argus wheel and every dependency wheel. Build it
once on a machine with internet, review or scan it, copy it to the target machines, and
install with the network disabled.

### Build (on a connected machine)

From a checkout of the release you want (needs Python 3.12+ and `uv` on PATH; the script
itself uses only the standard library):

```powershell
python scripts\build_wheelhouse.py            # writes dist\wheelhouse
python scripts\build_wheelhouse.py --dry-run  # lists what it would fetch; no network
python scripts\build_wheelhouse.py --argus-wheel .\argus_screens-<version>-py3-none-any.whl
```

The last form uses an existing wheel, for example the one downloaded from PyPI, instead
of building one from the checkout. It does not resolve anything. It reads `uv.lock`,
keeps the wheels for `win_amd64` and CPython 3.12 and 3.13, downloads each from the URL
recorded there, and refuses to keep a file whose SHA-256 differs from the one in the
lock. A package with no suitable wheel stops the build, because an offline install can't
compile from source. Expect around 50 dependency wheels (roughly 40 MB) and a build of
under a minute. Downloads honour the usual `HTTPS_PROXY`
settings.

The folder contains:

| File | Purpose |
| --- | --- |
| `*.whl` | The Argus wheel and the dependency wheels |
| `requirements.txt` | Every wheel pinned with its SHA-256, for `--require-hashes` |
| `INSTALL.txt` | The commands below, for whoever receives the folder |
| `SHA256SUMS` | SHA-256 of every other file, in `sha256sum -c` format |

### Verify (on the target machine)

```powershell
cd <wheelhouse folder>
Get-Content SHA256SUMS | ForEach-Object {
    $h, $f = $_ -split ' \*'
    if ((Get-FileHash $f -Algorithm SHA256).Hash.ToLower() -ne $h) { "MISMATCH: $f" } }
```

It prints nothing when every file matches. With Python available,
`python scripts\build_wheelhouse.py --verify <folder>` does the same and also reports
missing or unlisted files.

`SHA256SUMS` travels with the files, so on its own it catches corruption and partial
copies, not a deliberate swap. To detect tampering, send the SHA-256 of `SHA256SUMS`
itself (`Get-FileHash SHA256SUMS`) through a different channel, such as the ticket or
chat message that tells people the folder is ready, and compare it before verifying.
`requirements.txt` carries the same hashes again for the strict install below.

### Install (no network)

```powershell
uv tool install --offline --no-index --find-links . `
    --no-managed-python --no-python-downloads argus-screens==<version>
uv tool update-shell     # once, if ~\.local\bin isn't on your PATH; then open a new terminal
argus --version
```

`--offline` makes uv fail instead of reaching out; `--no-index` ignores PyPI; `--find-links .`
points at the folder. All three were used for the tests here, with the system Python.

**Strict variant** that also enforces every hash in `requirements.txt` (a tampered or
substituted wheel is refused, tested):

```powershell
uv venv "$env:LOCALAPPDATA\Argus\venv" --no-managed-python --no-python-downloads
uv pip install --python "$env:LOCALAPPDATA\Argus\venv\Scripts\python.exe" `
    --offline --no-index --find-links . --require-hashes -r requirements.txt
& "$env:LOCALAPPDATA\Argus\venv\Scripts\argus.exe" install
```

The executables then live in that venv's `Scripts` folder rather than `~\.local\bin`.
Uninstalling is `argus uninstall`, then deleting the venv folder.

#### Without uv: pip and venv

```powershell
python -m venv "$env:LOCALAPPDATA\Argus\venv"
& "$env:LOCALAPPDATA\Argus\venv\Scripts\python.exe" -m pip install --no-index `
    --find-links . --require-hashes -r requirements.txt
& "$env:LOCALAPPDATA\Argus\venv\Scripts\argus.exe" install
```

### Updating and removing

For a new release, build a new wheelhouse and rerun the install command with `--reinstall`
(or recreate the venv). To remove: `argus uninstall [--purge]`, then
`uv tool uninstall argus-screens` (or delete the venv).

## Registering with your AI tools

`argus install` writes the *absolute path* of `argus-mcp.exe` into each AI tool's config,
so that path has to keep existing. It deliberately refuses to register one that lives in
uv's cache or a temp folder (what `uvx` produces), because the registration would break the
next time uv cleans up. The installs above are stable:

- `uv tool install` puts `argus.exe`, `argus-mcp.exe` and `argus-tray.exe` in uv's tool
  bin folder (`%USERPROFILE%\.local\bin` unless `UV_TOOL_BIN_DIR` is set), backed by an
  environment under `%APPDATA%\uv\tools\argus-screens`.
- The venv routes use `<venv>\Scripts`.

Both pass the check. This was confirmed by running `argus install --dry-run` against an
offline wheelhouse install, which printed the real path and no temporary-location
warning. Don't put the venv or `UV_TOOL_DIR` under `%TEMP%`, and don't register through
`uvx`.

`argus install` is the only step that changes anything outside Argus's own folders. It:

- registers an `argus` MCP server with each tool it finds (Claude Code, Codex, Gemini CLI
  and Antigravity through their own `mcp add` commands; Claude Desktop and VS Code by editing
  JSON under `%APPDATA%`, with a `*.bak-argus-<time>` backup first);
- creates `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Argus.lnk` so the tray
  app starts with Windows, and starts the tray now;
- with `--trust`, adds `mcp__argus` to Claude Code's allow list in
  `~\.claude\settings.json` (the default is to keep per-capture prompts on).

On a managed machine, preview with `argus install --dry-run`, limit it with
`--clients claude-code,vscode`, and skip the startup entry and tray with `--no-startup`.
`argus uninstall` reverses all of it, and `argus doctor` checks monitors, OCR, the tray and
each registration.

## Network access

**Argus needs none at runtime.**

- `src/argus` contains no use of `socket`, `urllib`, `http`, `requests`, `httpx`, `ssl`
  or a browser launcher (searched). The server speaks MCP over **stdio** only
  (`mcp.run("stdio")`); it opens no listening port.
- OCR is the Windows built-in engine, which runs locally, so no OCR service is called.
  It needs an OCR-capable language in the user's Windows profile (`argus doctor` shows
  what is available); Argus does not download language packs.
- The `mcp` dependency pulls in an HTTP stack (`httpx2`, `starlette`, `uvicorn`, ...).
  Those packages are installed, so a dependency scanner will list them, but Argus does
  not use them.
- Network is used only (a) while installing, by uv or pip, and (b) by your AI tool, which
  sends the images Argus returns to its own model provider. That second part is outside
  Argus and is the real data-flow question for a review.

A firewall rule blocking outbound traffic from `argus-mcp.exe` and `argus-tray.exe` should
therefore be harmless. That follows from the source search above; it was not tested
by actually running the tools behind such a rule.

## What Argus writes to disk

Argus's own data is under `%USERPROFILE%\.argus` (or the folder in `ARGUS_HOME`):

| Path | Contents |
| --- | --- |
| `config.toml` | Settings, created with comments on first run |
| `captures\<date>\*.png` | Every capture that was sent to an AI tool, full resolution, after privacy redaction, with metadata embedded |
| `marks\<id>.json`, `<id>.window.png`, `<id>.monitor.png` | Tray hotkey marks |
| `baselines\*.png` | Visual-regression baselines (`argus baseline save`) |
| `logs\server.log`, `logs\tray.log` | Rotating logs (1 MB x 2 backups and 500 KB x 1) |
| `paused.json` | Present while Argus is paused |

Captures and marks are deleted after 7 days by default (`retention_days` under
`[privacy]`); baselines are kept until you delete them. The images are screenshots, so
treat the folder with the same care as the screens themselves, and note that the
redaction lists in `config.toml` are what keep passwords and messaging windows out of
them. `argus uninstall --purge` deletes the folder.

Outside that folder:

- the uv tool environment and shims (above), or the venv you made;
- the AI tool configs and Startup shortcut that `argus install` changes (above);
- a named mutex `Local\ArgusTray` while the tray runs (single instance).

Argus **reads**, but does not write, your Windows Screenshots folder (usually
`Pictures\Screenshots`, resolved through the Known Folder API) and images on the
clipboard, to serve `latest_snip`; and the names, paths and version-info descriptions
of running programs, to describe windows and apply the privacy lists.

## For IT and EDR reviewers

Argus is Python plus a few native wheels (Pillow, psutil, pywin32 as a dependency of
`mcp`, and the `winrt-*` projection packages). Its own code calls Windows through `ctypes`
(`src/argus/win32.py`). The behaviours below may trip security tooling; each is a core
function, not a side effect.

| Behaviour | API | Used by |
| --- | --- | --- |
| Screen capture | `mss` (GDI `BitBlt`) per monitor; `GetDC`, `PrintWindow` with `PW_RENDERFULLCONTENT` to render a covered window | `argus-mcp`, `argus-tray`, `argus` |
| Enumerating windows and monitors | `EnumWindows`, `EnumDisplayMonitors`, `GetWindowTextW`, `DwmGetWindowAttribute`, `GetForegroundWindow`, `GetCursorPos` | all |
| Process information | `psutil` (name, exe path, PID of window owners); `GetFileVersionInfoW` for the program description | all |
| Foreground-change tracking | `SetWinEventHook(EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT)`; a message-only window with `AddClipboardFormatListener` (clipboard *change* notifications; image data is read through Pillow's `ImageGrab.grabclipboard` only when the `latest_snip` tool is called) | MCP server |
| Global hotkeys | `RegisterHotKey` / `UnregisterHotKey` (Ctrl+Alt+S, Ctrl+Alt+P by default, configurable). This registers hotkeys; it does **not** install a keyboard hook and doesn't log keystrokes | `argus-tray` |
| Tray icon and overlay | `Shell_NotifyIcon` via `pystray`; brief click-through, topmost, layered `tkinter` windows (`WS_EX_LAYERED \| WS_EX_TRANSPARENT \| WS_EX_NOACTIVATE \| WS_EX_TOOLWINDOW`) that outline a marked window | `argus-tray` |
| OCR | `Windows.Media.Ocr` through the `winrt-*` packages | MCP server, CLI |
| Single instance | named mutex `Local\ArgusTray` | `argus-tray` |
| DPI awareness | `SetProcessDpiAwarenessContext` (per-monitor v2) | all |

Not used: `SetWindowsHookEx` or any keyboard/mouse hook, `SendInput` or other input
injection, process injection, services, scheduled tasks or registry run keys (autostart is
a `.lnk` in the per-user Startup folder), or any network call.

Child processes are spawned only by `argus install` / `argus uninstall`: `powershell`
to create the Startup shortcut, each AI tool's own CLI (`claude`, `codex`, `gemini`,
`agy`), and `taskkill` to stop the tray; and by `argus install` to start `argus-tray.exe`.
The tray opens its captures, marks and settings folders with `os.startfile`.

Executables in a standard install: `argus.exe` (CLI), `argus-mcp.exe` (MCP stdio server,
started by the AI tool), `argus-tray.exe` (windowless tray app). Each is a launcher for the
Python in its environment, so security tooling will see `python.exe` or the launcher as the
process, not a signed Argus binary. The wheels are not code-signed by Argus. The checks
available are the SHA-256 values above, which come from `uv.lock` and the PyPI release.
