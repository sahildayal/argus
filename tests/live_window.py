"""A throwaway window for live tests: known text, solid colour blocks and a
button, so tests can capture something without touching the user's own windows."""

import argparse
import ctypes
import tkinter as tk

ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

parser = argparse.ArgumentParser()
parser.add_argument("--title", default="Argus Test Window")
parser.add_argument("--geometry", default="900x560+120+120")
parser.add_argument("--topmost", action="store_true")
parser.add_argument("--change-after", type=float, default=0, help="seconds until the status line changes")
parser.add_argument("--close-after", type=float, default=60, help="safety net: never outlive a test run")
args = parser.parse_args()

root = tk.Tk()
root.title(args.title)
root.geometry(args.geometry)
root.configure(bg="#ffffff")
if args.topmost:
    root.attributes("-topmost", True)
tk.Label(root, text="ARGUS TEST WINDOW", font=("Segoe UI", 26, "bold"), bg="#ffffff").place(x=30, y=16)
tk.Label(root, text="error TS2304: Cannot find name 'foo'", font=("Consolas", 18), bg="#ffffff", fg="#b00020").place(x=30, y=90)
for i, colour in enumerate(("#ff0000", "#00c000", "#0000ff")):
    tk.Frame(root, bg=colour, width=120, height=80).place(x=30 + i * 140, y=150)
tk.Button(root, text="Submit", font=("Segoe UI", 16)).place(x=30, y=260)
status = tk.Label(root, text="Building...", font=("Segoe UI", 20), bg="#ffffff")
status.place(x=30, y=350)
if args.change_after:
    root.after(int(args.change_after * 1000), lambda: status.config(text="Compiled successfully", fg="#007000"))
root.after(int(args.close_after * 1000), root.destroy)
root.mainloop()
