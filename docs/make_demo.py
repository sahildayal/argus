"""Build docs/demo.gif from real Argus output.

Everything in the GIF comes from Argus working on throwaway windows that this
script opens itself, never your own screen: the window capture (rendered
offscreen with PrintWindow), the labeled grid, the zoom, the OCR text, the
privacy blackout and look's reasoning. The assistant's reply is a real Claude
answer about that same capture. It's cached in docs/demo_reply.txt;
`--ask-claude` refreshes it, which needs the `claude` CLI and costs a few cents.

    uv run python docs/make_demo.py [--ask-claude]

Windows only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import textwrap
import time
import tkinter as tk
from pathlib import Path

# Keep this run's captures out of the user's real ~/.argus.
os.environ["ARGUS_HOME"] = tempfile.mkdtemp(prefix="argus-demo-")

from PIL import Image, ImageDraw, ImageFont, ImageTk  # noqa: E402

from argus import ocr, win32  # noqa: E402,F401  (win32 first: DPI awareness)
from argus.capture import render_window  # noqa: E402
from argus.config import settings  # noqa: E402
from argus.desktop import NO_HOST, Desktop, Window, set_host, snapshot  # noqa: E402
from argus.geometry import Rect  # noqa: E402
from argus.imaging import render  # noqa: E402
from argus.looking import choose  # noqa: E402
from argus.privacy import redact  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "demo.gif"
REPLY = HERE / "demo_reply.txt"
CHECKOUT_TITLE = "Checkout – localhost:3000"
VAULT_TITLE = "Bitwarden"
PROMPT = "look at what I'm looking at. Why is checkout broken?"

W, H = 1200, 675
CYAN = (0, 209, 255)


def font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    names = {"regular": "segoeui.ttf", "semibold": "seguisb.ttf", "bold": "segoeuib.ttf", "mono": "consola.ttf"}
    return ImageFont.truetype(names[weight], size)


# ------------------------------------------------------------- fake app pages


def checkout_page() -> Image.Image:
    """A small web checkout page with two visible bugs: the Pay button sits on
    top of the card field, and a JavaScript error banner."""
    w, h = 960, 600
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    # browser chrome
    d.rectangle([0, 0, w, 74], fill=(222, 225, 230))
    d.rounded_rectangle([12, 8, 230, 38], radius=8, fill="white")
    d.text((26, 13), "Checkout", fill=(32, 33, 36), font=font(14))
    d.rounded_rectangle([12, 42, w - 14, 68], radius=13, fill="white")
    d.text((30, 46), "localhost:3000/checkout", fill=(60, 64, 67), font=font(14))
    # site header
    d.text((40, 92), "acme", fill=(17, 24, 39), font=font(26, "bold"))
    for i, label in enumerate(("Shop", "Deals", "Account")):
        d.text((640 + i * 92, 100), label, fill=(75, 85, 99), font=font(15))
    d.line([40, 140, w - 40, 140], fill=(229, 231, 235), width=1)
    d.text((40, 156), "Checkout", fill=(17, 24, 39), font=font(30, "semibold"))

    def field(y: int, label: str, value: str, width: int = 470) -> None:
        d.text((40, y), label, fill=(55, 65, 81), font=font(14, "semibold"))
        d.rounded_rectangle([40, y + 22, 40 + width, y + 62], radius=8, outline=(209, 213, 219), width=2, fill="white")
        d.text((54, y + 31), value, fill=(17, 24, 39), font=font(16))

    field(214, "Email", "sam@example.com")
    field(292, "Card number", "4242 4242 4242 4242")
    field(370, "Expiry", "09 / 28", width=220)
    d.text((290, 370), "CVC", fill=(55, 65, 81), font=font(14, "semibold"))
    d.rounded_rectangle([290, 392, 510, 432], radius=8, outline=(209, 213, 219), width=2, fill="white")
    d.text((304, 401), "•••", fill=(17, 24, 39), font=font(16))
    # The bug: the Pay button floats up over the card number field.
    d.rounded_rectangle([290, 300, 510, 346], radius=10, fill=(79, 70, 229))
    d.text((360, 311), "Pay now", fill="white", font=font(18, "semibold"))
    # order summary
    d.rounded_rectangle([560, 214, w - 40, 432], radius=12, fill=(249, 250, 251), outline=(229, 231, 235))
    d.text((584, 232), "Order summary", fill=(17, 24, 39), font=font(17, "semibold"))
    for i, (item, price) in enumerate((("Mechanical keyboard", "$129.00"), ("USB-C cable", "$12.00"))):
        d.text((584, 274 + i * 34), item, fill=(55, 65, 81), font=font(15))
        d.text((w - 64, 274 + i * 34), price, fill=(55, 65, 81), font=font(15), anchor="ra")
    d.line([584, 352, w - 64, 352], fill=(229, 231, 235))
    d.text((584, 366), "Total", fill=(17, 24, 39), font=font(16, "semibold"))
    d.text((w - 64, 366), "NaN", fill=(220, 38, 38), font=font(16, "semibold"), anchor="ra")
    # console error banner
    d.rounded_rectangle([40, 470, w - 40, 560], radius=10, fill=(254, 242, 242), outline=(248, 113, 113), width=2)
    d.text((60, 484), "Uncaught TypeError: Cannot read properties of undefined (reading 'total')",
           fill=(153, 27, 27), font=font(16, "mono"))
    d.text((60, 516), "    at updateTotals (checkout.js:42:31)", fill=(185, 28, 28), font=font(15, "mono"))
    return img


def vault_page() -> Image.Image:
    w, h = 380, 300
    img = Image.new("RGB", (w, h), (24, 32, 52))
    d = ImageDraw.Draw(img)
    d.text((22, 18), "My vault", fill="white", font=font(20, "semibold"))
    for i, name in enumerate(("GitHub", "Bank of Example", "Personal email", "Wi-Fi")):
        y = 70 + i * 54
        d.rounded_rectangle([18, y, w - 18, y + 44], radius=8, fill=(36, 48, 76))
        d.text((32, y + 4), name, fill="white", font=font(14, "semibold"))
        d.text((32, y + 22), "•••••••••••••", fill=(160, 174, 200), font=font(13))
    return img


def app_icon(colour) -> Image.Image:
    icon = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    ImageDraw.Draw(icon).ellipse([4, 4, 28, 28], fill=colour)
    return icon


# ----------------------------------------------------------- windows + capture


class Stage:
    """Our own topmost windows, so nothing of the user's ever gets near a capture."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.keep = []

    def show(self, title: str, page: Image.Image, x: int, y: int, colour) -> None:
        top = tk.Toplevel(self.root)
        top.title(title)
        top.geometry(f"{page.width}x{page.height}+{x}+{y}")
        top.resizable(False, False)
        top.attributes("-topmost", True)
        photo, icon = ImageTk.PhotoImage(page), ImageTk.PhotoImage(app_icon(colour))
        top.iconphoto(False, icon)
        tk.Label(top, image=photo, bd=0, highlightthickness=0).pack()
        self.keep += [photo, icon, top]
        self.pump(0.8)

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.02)

    def close(self) -> None:
        self.root.destroy()


def my_window(title: str) -> Window:
    for w in snapshot(NO_HOST).windows:
        if w.title == title and w.pid == os.getpid():
            return w
    raise SystemExit(f"demo window {title!r} not found")


def offscreen(win: Window) -> Image.Image:
    img = render_window(win)
    if img is None:  # never fall back to a screen grab: that could include the user's windows
        raise SystemExit(f"{win.title!r} wouldn't render offscreen")
    return img


def ask_claude() -> str:
    config = Path(tempfile.mkdtemp()) / "argus-only.json"
    config.write_text(json.dumps({"mcpServers": {"argus": {"command": "argus-mcp", "args": []}}}), encoding="utf-8")
    prompt = (
        f'Use the argus screenshot tool with window "{CHECKOUT_TITLE}" (no other tools). Then, as plain text with no '
        "markdown and at most 2 short sentences, say what is visibly broken on this checkout page and the likely fix."
    )
    out = subprocess.run(
        ["claude", "-p", prompt, "--strict-mcp-config", "--mcp-config", str(config), "--allowedTools",
         "mcp__argus__screenshot", "--model", "sonnet", "--output-format", "json"],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    result = json.loads(out.stdout)
    if result.get("is_error"):
        raise SystemExit(f"claude failed: {result}")
    return result["result"].strip()


# ------------------------------------------------------------------ drawing


def base(title: str, subtitle: str = "") -> Image.Image:
    img = Image.new("RGB", (W, H), (13, 15, 20))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([20, 20, 768, H - 20], radius=16, fill=(24, 28, 37))
    d.text((40, 32), title.upper(), fill=(120, 130, 150), font=font(12, "semibold"))
    if subtitle:
        d.text((748, 32), subtitle, fill=(120, 130, 150), font=font(12), anchor="ra")
    d.rounded_rectangle([786, 20, W - 20, H - 20], radius=16, fill=(21, 23, 29), outline=(42, 46, 56))
    d.text((806, 32), "CLAUDE  +  ARGUS", fill=(120, 130, 150), font=font(12, "semibold"))
    return img


def place(canvas: Image.Image, shot: Image.Image, box=(40, 58, 748, H - 40)) -> tuple[float, tuple[int, int]]:
    """Fit a capture into the left panel; returns (scale, top-left)."""
    bw, bh = box[2] - box[0], box[3] - box[1]
    s = min(bw / shot.width, bh / shot.height)
    img = shot.resize((round(shot.width * s), round(shot.height * s)), Image.Resampling.LANCZOS)
    x, y = box[0] + (bw - img.width) // 2, box[1] + (bh - img.height) // 2
    canvas.paste(img, (x, y))
    return s, (x, y)


class Chat:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def add(self, kind: str, text: str) -> None:
        self.items.append((kind, text))

    def draw(self, canvas: Image.Image, partial: str | None = None) -> None:
        d = ImageDraw.Draw(canvas)
        y = 62
        items = list(self.items)
        if partial is not None:
            kind, _ = items[-1]
            items[-1] = (kind, partial)
        for kind, text in items:
            if kind == "you":
                lines = textwrap.wrap(text, 34) or [""]
                h = 22 * len(lines) + 34
                d.rounded_rectangle([880, y, W - 34, y + h], radius=12, fill=(46, 54, 74))
                d.text((894, y + 8), "You", fill=(150, 170, 210), font=font(12, "semibold"))
                for i, line in enumerate(lines):
                    d.text((894, y + 26 + i * 22), line, fill="white", font=font(15))
            elif kind == "tool":
                head, _, body = text.partition("\n")
                lines = textwrap.wrap(body, 44)
                h = 20 * len(lines) + 34
                d.rounded_rectangle([800, y, W - 34, y + h], radius=10, fill=(9, 40, 50), outline=(0, 120, 150))
                d.text((814, y + 7), head, fill=CYAN, font=font(13, "mono"))
                for i, line in enumerate(lines):
                    d.text((814, y + 28 + i * 20), line, fill=(170, 215, 230), font=font(13))
            elif kind == "note":
                lines = textwrap.wrap(text, 44)
                h = 20 * len(lines) + 12
                for i, line in enumerate(lines):
                    d.text((806, y + 4 + i * 20), line, fill=(150, 160, 180), font=font(13))
            else:  # claude
                lines = textwrap.wrap(text, 40) or [""]
                h = 22 * len(lines) + 34
                d.rounded_rectangle([800, y, W - 70, y + h], radius=12, fill=(32, 35, 43))
                d.text((814, y + 8), "Claude", fill=(230, 160, 120), font=font(12, "semibold"))
                for i, line in enumerate(lines):
                    d.text((814, y + 26 + i * 22), line, fill=(235, 235, 240), font=font(15))
            y += h + 12


def outline(canvas: Image.Image, rect: tuple[int, int, int, int], width: int = 4) -> None:
    ImageDraw.Draw(canvas).rectangle(rect, outline=CYAN, width=width)


# -------------------------------------------------------------------- build


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ask-claude", action="store_true", help="refresh the cached Claude reply")
    args = parser.parse_args()
    set_host(NO_HOST)

    stage = Stage()
    try:
        stage.show(CHECKOUT_TITLE, checkout_page(), 40, 40, (79, 70, 229))
        checkout = my_window(CHECKOUT_TITLE)
        page = offscreen(checkout)
        if args.ask_claude or not REPLY.exists():
            REPLY.write_text(ask_claude() + "\n", encoding="utf-8")
        stage.show(VAULT_TITLE, vault_page(), checkout.rect.x + 560, checkout.rect.y + 250, (23, 93, 220))
        vault = my_window(VAULT_TITLE)
        vault_img = offscreen(vault)
    finally:
        stage.close()
    reply = REPLY.read_text(encoding="utf-8").strip()

    # look's real reasoning, for the moment you switched from this page to your terminal.
    terminal = Window(hwnd=1, title="claude", cls="CASCADIA_HOSTING_WINDOW_CLASS", pid=1, process="WindowsTerminal.exe",
                      app="Windows Terminal", rect=Rect(0, 0, 1, 1), z=0, minimized=False, maximized=False, topmost=False,
                      see_through=False, is_app=True, owner=0, monitor=1, host=True)
    desk = Desktop((), (terminal, checkout), (0, 0), terminal.hwnd, 0, NO_HOST, 1000.0)
    picked = choose(desk, [(999.0, terminal.hwnd), (996.0, checkout.hwnd)])
    look_text = f'argus · look\nPicked "{CHECKOUT_TITLE}" because {picked.reason}.'

    # Real grid + OCR + zoom on the capture.
    grid_view, grid_img = render(page, max_side=2000, grid=True)
    lines = asyncio.run(ocr.read(page))
    error = [ln for ln in lines if "TypeError" in ln.text or "updateTotals" in ln.text]
    ex0, ey0 = min(ln.box[0] for ln in error), min(ln.box[1] for ln in error)
    ex1, ey1 = max(ln.box[2] for ln in error), max(ln.box[3] for ln in error)
    g = grid_view.grid
    # +60px below: include the stack-trace line under it even if OCR misreads it
    cells = f"{g.cell_at(ex0 - 4, ey0 - 4)}:{g.cell_at(min(ex1 + 4, page.width - 1), min(ey1 + 60, page.height - 1))}"
    gx0, gy0, gx1, gy1 = g.cells(cells)
    zx0, zy0, zx1, zy1 = round(gx0), round(gy0), min(page.width, round(gx1)), min(page.height, round(gy1))
    zoom_view, zoom_img = render(page.crop((zx0, zy0, zx1, zy1)), max_side=2000, upscale_to=1200)
    zoom_lines = asyncio.run(ocr.read(page.crop((zx0, zy0, zx1, zy1))))
    error_text = next((ln.text for ln in zoom_lines if "TypeError" in ln.text), error[0].text)

    # Real redaction: the vault window over the page, as Argus would send it.
    screen = page.copy()
    vx, vy = vault.rect.x - checkout.rect.x, vault.rect.y - checkout.rect.y
    screen.paste(vault_img, (vx, vy))
    sent = screen.copy()
    vault_like = Window(hwnd=2, title=VAULT_TITLE, cls="Bitwarden", pid=2, process="Bitwarden.exe", app="Bitwarden",
                        rect=vault.rect, z=0, minimized=False, maximized=False, topmost=True, see_through=False,
                        is_app=True, owner=0, monitor=1)
    page_like = Window(hwnd=3, title=CHECKOUT_TITLE, cls="Chrome_WidgetWin_1", pid=3, process="chrome.exe",
                       app="Google Chrome", rect=checkout.rect, z=1, minimized=False, maximized=False, topmost=False,
                       see_through=False, is_app=True, owner=0, monitor=1)
    redact(sent, (checkout.rect.x, checkout.rect.y), Desktop((), (vault_like, page_like), (0, 0), 0, 0, NO_HOST, 0.0), settings())

    frames: list[tuple[Image.Image, int]] = []

    def frame(img: Image.Image, ms: int) -> None:
        frames.append((img, ms))

    chat = Chat()

    def scene(left: Image.Image, label: str, sub: str = "") -> tuple[Image.Image, float, tuple[int, int]]:
        canvas = base(label, sub)
        s, pos = place(canvas, left)
        return canvas, s, pos

    # 1. You're looking at a broken page; ask about it.
    canvas, s, (px, py) = scene(page, "Your screen", f"window capture, {page.width} x {page.height}")
    frame(canvas.copy(), 500)
    chat.add("you", "")
    for i in range(0, len(PROMPT) + 1, 4):
        c = canvas.copy()
        chat.draw(c, PROMPT[:i])
        frame(c, 40)
    chat.items[-1] = ("you", PROMPT)
    c = canvas.copy()
    chat.draw(c)
    frame(c, 400)

    # 2. look picks the page; the capture flashes.
    chat.add("tool", look_text)
    box = (px - 3, py - 3, px + round(page.width * s) + 2, py + round(page.height * s) + 2)
    for on in (True, False, True, False, True):
        c = canvas.copy()
        if on:
            outline(c, box)
        chat.draw(c)
        frame(c, 140)
    c = canvas.copy()
    chat.draw(c)
    frame(c, 700)

    # 3. Claude's real answer.
    chat.add("claude", "")
    for i in range(0, len(reply) + 1, 9):
        c = canvas.copy()
        chat.draw(c, reply[:i])
        frame(c, 35)
    chat.items[-1] = ("claude", reply)
    c = canvas.copy()
    chat.draw(c)
    frame(c, 2000)

    # 4. Zoom into the error by grid cell.
    chat = Chat()
    chat.add("you", "zoom in on that error")
    chat.add("tool", f'argus · zoom(cell="{cells}")\n{error_text}')
    canvas, s, (px, py) = scene(grid_img, "Capture with grid", f"{grid_view.grid.cols} x {grid_view.grid.rows} cells")
    c = canvas.copy()
    Chat.draw(_only(chat, 1), c)  # just the question for now
    frame(c, 700)
    cell_box = (px + round(zx0 * s), py + round(zy0 * s), px + round(zx1 * s), py + round(zy1 * s))
    c = canvas.copy()
    outline(c, cell_box, 3)
    Chat.draw(_only(chat, 1), c)
    frame(c, 500)
    for k in range(1, 9):  # animate from the full capture into the zoom
        t = k / 8
        crop = (round(zx0 * t), round(zy0 * t), round(page.width + (zx1 - page.width) * t), round(page.height + (zy1 - page.height) * t))
        c = base("Zoom (full resolution)", "")
        place(c, grid_img.crop(crop))
        Chat.draw(_only(chat, 1), c)
        frame(c, 60)
    c = base("Zoom (full resolution)", f"{zoom_img.width} x {zoom_img.height}")
    place(c, zoom_img)
    chat.draw(c)
    frame(c, 2000)

    # 5. Privacy: what's on screen vs what the AI receives.
    chat = Chat()
    chat.add("note", "Your password manager pops up over the page...")
    canvas, s, (px, py) = scene(screen, "Your screen")
    c = canvas.copy()
    chat.draw(c)
    frame(c, 1100)
    after = base("What the AI receives")
    place(after, sent)
    chat.add("tool", "privacy list\nBitwarden is blacked out before anything leaves your machine, and its title is never sent.")
    for k in range(1, 9):  # wipe left to right
        c = canvas.copy()
        cut = round(W * k / 8)
        c.paste(after.crop((0, 0, cut, H)), (0, 0))
        chat.draw(c)
        frame(c, 55)
    c = after.copy()
    chat.draw(c)
    frame(c, 2000)

    # 6. End card.
    end = Image.new("RGB", (W, H), (13, 15, 20))
    d = ImageDraw.Draw(end)
    d.text((W // 2, 200), "Argus", fill="white", font=font(72, "bold"), anchor="mm")
    d.text((W // 2, 268), "Eyes for your AI tools across every monitor", fill=(190, 198, 212), font=font(24), anchor="mm")
    d.rounded_rectangle([W // 2 - 300, 320, W // 2 + 300, 372], radius=12, fill=(24, 28, 37), outline=(0, 120, 150))
    d.text((W // 2, 346), "uv tool install argus-screens", fill=CYAN, font=font(22, "mono"), anchor="mm")
    d.text((W // 2, 420), "Claude Code · Claude Desktop · VS Code · Codex · Gemini CLI · Antigravity",
           fill=(150, 160, 180), font=font(17), anchor="mm")
    d.text((W // 2, 456), "look · screenshot · zoom · OCR · visual diffs · wait_for · privacy blackout · pause hotkey",
           fill=(110, 120, 140), font=font(15), anchor="mm")
    d.text((W // 2, H - 40), "Built from real Argus output on demo windows (focus history simulated) and a real Claude reply.",
           fill=(90, 98, 115), font=font(13), anchor="mm")
    frame(end, 2800)

    write_gif(frames)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.2f} MB, {len(frames)} frames, "
          f"{sum(ms for _, ms in frames) / 1000:.1f}s)")


def _only(chat: Chat, n: int) -> Chat:
    c = Chat()
    c.items = chat.items[:n]
    return c


def write_gif(frames: list[tuple[Image.Image, int]]) -> None:
    # One shared palette (from a sample of frames) keeps colours steady and lets
    # the encoder store only what changed between frames.
    picks = [frames[i][0] for i in range(0, len(frames), max(1, len(frames) // 12))] + [frames[-1][0]]
    sheet = Image.new("RGB", (W // 2, (H // 2) * len(picks)))
    for i, im in enumerate(picks):
        sheet.paste(im.resize((W // 2, H // 2)), (0, i * (H // 2)))
    palette = sheet.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    images = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im, _ in frames]
    images[0].save(OUT, save_all=True, append_images=images[1:], duration=[ms for _, ms in frames], loop=0,
                   optimize=False, disposal=1)


if __name__ == "__main__":
    main()
