"""A console program that reports the console it was given, then waits.

Usage: ``console_probe.py [hold-seconds] [report-file]``. Prints one JSON
line, and writes it to the report file when one is given: its pid, its
console code page (0 when it has no console) and its console window handle
(0 when the console has no window). Standard library only.
"""

import ctypes
import json
import os
import sys
import time

kernel32 = ctypes.WinDLL("kernel32")
kernel32.GetConsoleWindow.restype = ctypes.c_void_p

report = json.dumps({
    "pid": os.getpid(),
    "console_cp": kernel32.GetConsoleCP(),
    "console_window": kernel32.GetConsoleWindow() or 0,
})
print(report, flush=True)
if len(sys.argv) > 2:
    with open(sys.argv[2], "w", encoding="utf-8") as handle:
        handle.write(report)
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 0.0)
