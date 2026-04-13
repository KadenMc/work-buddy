#!/usr/bin/env python3
"""Register the Work Buddy native messaging host with Chrome.

Supports Windows, macOS, and Linux. Each platform uses a different
mechanism to register native messaging hosts:

- **Windows**: .bat wrapper + registry entry at
  ``HKCU\\SOFTWARE\\Google\\Chrome\\NativeMessagingHosts``
- **macOS**: Shell wrapper + manifest in
  ``~/Library/Application Support/Google/Chrome/NativeMessagingHosts``
- **Linux**: Shell wrapper + manifest in
  ``~/.config/google-chrome/NativeMessagingHosts``

Usage
-----
Run from the work-buddy conda environment::

    python install.py

Or with an extension ID argument (after loading the extension in Chrome)::

    python install.py --extension-id abcdefghijklmnopqrstuvwxyzabcdef

If no extension ID is provided, the manifest uses a wildcard origin
that allows any extension to connect (fine for local development).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from work_buddy.compat import IS_WINDOWS, IS_MACOS, chrome_native_messaging_dir

HOST_NAME = "com.work_buddy.tabs"

# The host script lives alongside this install script
HOST_SCRIPT = Path(__file__).resolve().parent / "host.py"

# Python interpreter in the conda env
PYTHON_EXE = Path(sys.executable).resolve()

# Platform-appropriate native messaging directory
NATIVE_MESSAGING_DIR = chrome_native_messaging_dir()


def create_wrapper() -> Path:
    """Create a platform-appropriate wrapper script.

    Native messaging requires the manifest ``path`` field to point to an
    executable file. On Windows, this must be a .bat or .exe. On Unix,
    a shell script with execute permission.

    Returns
    -------
    Path
        Absolute path to the created wrapper.
    """
    if IS_WINDOWS:
        wrapper_path = HOST_SCRIPT.with_suffix(".bat")
        wrapper_content = f'@echo off\r\n"{PYTHON_EXE}" "{HOST_SCRIPT}" %*\r\n'
        wrapper_path.write_text(wrapper_content, encoding="utf-8")
    else:
        wrapper_path = HOST_SCRIPT.with_suffix(".sh")
        wrapper_content = f'#!/bin/bash\nexec "{PYTHON_EXE}" "{HOST_SCRIPT}" "$@"\n'
        wrapper_path.write_text(wrapper_content, encoding="utf-8")
        wrapper_path.chmod(0o755)
    return wrapper_path


def create_manifest(wrapper_path: Path, extension_id: str | None = None) -> Path:
    """Create the native messaging host manifest JSON.

    Parameters
    ----------
    wrapper_path : Path
        Absolute path to the wrapper script.
    extension_id : str or None
        Chrome extension ID. If None, allows any extension.

    Returns
    -------
    Path
        Path to the written manifest file.
    """
    if extension_id:
        allowed_origins = [f"chrome-extension://{extension_id}/"]
    else:
        # During development, allow any extension
        # Chrome still requires at least one origin in the list
        allowed_origins = ["chrome-extension://*/"]

    manifest = {
        "name": HOST_NAME,
        "description": (
            "Native messaging host for Work Buddy Tab Exporter. "
            "Receives tab snapshots and writes them to .chrome_tabs.json."
        ),
        "path": str(wrapper_path),
        "type": "stdio",
        "allowed_origins": allowed_origins,
    }

    # Write to the standard Chrome directory
    NATIVE_MESSAGING_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = NATIVE_MESSAGING_DIR / f"{HOST_NAME}.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    return manifest_path


def register_in_registry(manifest_path: Path) -> None:
    """Create a Windows registry entry pointing to the manifest.

    Chrome on Windows looks up native messaging hosts via the registry
    at ``HKCU\\SOFTWARE\\Google\\Chrome\\NativeMessagingHosts\\<name>``.

    This is a no-op on non-Windows platforms (manifest location is sufficient).

    Parameters
    ----------
    manifest_path : Path
        Absolute path to the manifest JSON file.
    """
    if not IS_WINDOWS:
        return

    import winreg

    registry_key = r"SOFTWARE\Google\Chrome\NativeMessagingHosts"
    key_path = f"{registry_key}\\{HOST_NAME}"
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        )
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path))
        winreg.CloseKey(key)
    except OSError as exc:
        print(f"Warning: could not write registry key: {exc}")
        print("The manifest file was still created. You may need to register it manually.")
        return


def main() -> int:
    """Run the installation.

    Returns
    -------
    int
        Exit code.
    """
    parser = argparse.ArgumentParser(
        description="Register the Work Buddy native messaging host with Chrome."
    )
    parser.add_argument(
        "--extension-id",
        default=None,
        help=(
            "Chrome extension ID (32-character string from chrome://extensions). "
            "If omitted, allows any extension to connect (dev mode)."
        ),
    )
    args = parser.parse_args()

    print(f"Python:      {PYTHON_EXE}")
    print(f"Host script: {HOST_SCRIPT}")
    print(f"Platform:    {sys.platform}")
    print()

    # 1. Create wrapper script
    wrapper_path = create_wrapper()
    ext = ".bat" if IS_WINDOWS else ".sh"
    print(f"Created {ext} wrapper: {wrapper_path}")

    # 2. Create manifest
    manifest_path = create_manifest(wrapper_path, args.extension_id)
    print(f"Created manifest:     {manifest_path}")

    # 3. Register in Windows registry (no-op on other platforms)
    register_in_registry(manifest_path)
    if IS_WINDOWS:
        registry_key = r"SOFTWARE\Google\Chrome\NativeMessagingHosts"
        print(f"Registered in registry: HKCU\\{registry_key}\\{HOST_NAME}")
    print()

    if args.extension_id:
        print(f"Allowed extension: {args.extension_id}")
    else:
        print("WARNING: No extension ID specified. Any extension can connect.")
        print("After loading the extension, re-run with:")
        print(f"  python install.py --extension-id <YOUR_EXTENSION_ID>")

    print()
    print("Installation complete.")
    print()
    print("Next steps:")
    print("  1. Load the extension in chrome://extensions (Developer mode)")
    print("  2. Copy the extension ID from the extensions page")
    print("  3. Re-run: python install.py --extension-id <ID>")
    print("  4. Restart Chrome")
    print(f"  5. Check that {Path(__file__).resolve().parent.parent.parent / '.chrome_tabs.json'} is being written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
