"""Host-native directory selection for the local Co-work dashboard.

The browser cannot safely turn a client-side directory handle into a path on the
machine running Work Buddy. Local desktop installs therefore ask the host OS to
choose a directory; hosts without a graphical picker report it as unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path


HostFolderChooser = Callable[[], str | Path | None]

_DIALOG_LOCK = threading.Lock()
_WINDOWS_CANCELLED = 2
_DIALOG_TIMEOUT_SECONDS = 120

_WINDOWS_DIALOG_SCRIPT = r"""
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$source = @'
using System;
using System.Runtime.InteropServices;

namespace WorkBuddy.Cowork
{
    internal static class NativeMethods
    {
        [DllImport("user32.dll")]
        internal static extern IntPtr GetForegroundWindow();
    }

    [Flags]
    internal enum FILEOPENDIALOGOPTIONS : uint
    {
        FOS_NOCHANGEDIR = 0x00000008,
        FOS_PICKFOLDERS = 0x00000020,
        FOS_FORCEFILESYSTEM = 0x00000040,
        FOS_PATHMUSTEXIST = 0x00000800
    }

    internal enum SIGDN : uint
    {
        FILESYSPATH = 0x80058000
    }

    [ComImport]
    [Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellItem
    {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName(SIGDN sigdnName, out IntPtr ppszName);
        void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
        void Compare(IShellItem psi, uint hint, out int piOrder);
    }

    [ComImport]
    [Guid("D57C7288-D4AD-4768-BE02-9D969532D960")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IFileOpenDialog
    {
        [PreserveSig]
        int Show(IntPtr parent);
        void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
        void SetFileTypeIndex(uint iFileType);
        void GetFileTypeIndex(out uint piFileType);
        void Advise(IntPtr pfde, out uint pdwCookie);
        void Unadvise(uint dwCookie);
        void SetOptions(FILEOPENDIALOGOPTIONS fos);
        void GetOptions(out FILEOPENDIALOGOPTIONS pfos);
        void SetDefaultFolder(IShellItem psi);
        void SetFolder(IShellItem psi);
        void GetFolder(out IShellItem ppsi);
        void GetCurrentSelection(out IShellItem ppsi);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string pszName);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
        void GetResult(out IShellItem ppsi);
    }

    [ComImport]
    [Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")]
    [ClassInterface(ClassInterfaceType.None)]
    internal class FileOpenDialog
    {
    }

    public static class FolderPicker
    {
        private const int CancelledHResult = unchecked((int)0x800704C7);

        public static int Pick(out string selectedPath)
        {
            selectedPath = null;
            IFileOpenDialog dialog = null;
            IShellItem item = null;

            try
            {
                dialog = (IFileOpenDialog)new FileOpenDialog();
                dialog.SetOptions(
                    FILEOPENDIALOGOPTIONS.FOS_NOCHANGEDIR |
                    FILEOPENDIALOGOPTIONS.FOS_PICKFOLDERS |
                    FILEOPENDIALOGOPTIONS.FOS_FORCEFILESYSTEM |
                    FILEOPENDIALOGOPTIONS.FOS_PATHMUSTEXIST);
                dialog.SetTitle("Open Folder");

                IntPtr owner = NativeMethods.GetForegroundWindow();
                int result = dialog.Show(owner);
                if (result == CancelledHResult)
                {
                    return 2;
                }
                if (result < 0)
                {
                    Marshal.ThrowExceptionForHR(result);
                }

                dialog.GetResult(out item);
                IntPtr displayName = IntPtr.Zero;
                try
                {
                    item.GetDisplayName(SIGDN.FILESYSPATH, out displayName);
                    selectedPath = Marshal.PtrToStringUni(displayName);
                }
                finally
                {
                    if (displayName != IntPtr.Zero)
                    {
                        Marshal.FreeCoTaskMem(displayName);
                    }
                }

                return String.IsNullOrWhiteSpace(selectedPath) ? 1 : 0;
            }
            finally
            {
                if (item != null && Marshal.IsComObject(item))
                {
                    Marshal.FinalReleaseComObject(item);
                }
                if (dialog != null && Marshal.IsComObject(dialog))
                {
                    Marshal.FinalReleaseComObject(dialog);
                }
            }
        }
    }
}
'@

try {
    Add-Type -TypeDefinition $source -Language CSharp
    $selectedPath = $null
    $result = [WorkBuddy.Cowork.FolderPicker]::Pick([ref]$selectedPath)
    if ($result -eq 0) {
        [Console]::Write($selectedPath)
        exit 0
    }
    exit $result
}
catch {
    [Console]::Error.Write($_.Exception.Message)
    exit 1
}
""".strip()


class NativeFolderChooserError(RuntimeError):
    """The host advertised a native picker but could not open it."""


def _run_dialog(command: list[str], *, cancelled_code: int = 1) -> str | None:
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": _DIALOG_TIMEOUT_SECONDS,
        "check": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(command, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeFolderChooserError("The Folder picker could not be opened.") from exc
    if completed.returncode == cancelled_code:
        return None
    if completed.returncode != 0:
        raise NativeFolderChooserError("The Folder picker closed unexpectedly.")
    selected = completed.stdout.strip()
    return selected or None


def _choose_windows(powershell: str) -> str | None:
    return _run_dialog(
        [powershell, "-NoProfile", "-NonInteractive", "-STA", "-Command", _WINDOWS_DIALOG_SCRIPT],
        cancelled_code=_WINDOWS_CANCELLED,
    )


def _choose_macos() -> str | None:
    script = (
        'try\nPOSIX path of (choose folder with prompt "Open Folder")\n'
        'on error number -128\nerror number 2\nend try'
    )
    return _run_dialog(["/usr/bin/osascript", "-e", script], cancelled_code=2)


def _choose_zenity(zenity: str) -> str | None:
    return _run_dialog(
        [
            zenity,
            "--file-selection",
            "--directory",
            "--title=Open Folder",
        ]
    )


def default_host_folder_chooser() -> HostFolderChooser | None:
    """Return the supported chooser for this host, or ``None`` when headless.

    Detection is intentionally conservative so unavailable hosts do not advertise
    a button that can never surface a dialog.
    """

    implementation: Callable[[], str | None] | None = None
    if os.name == "nt":
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell is not None:
            implementation = lambda: _choose_windows(powershell)
    elif sys.platform == "darwin" and Path("/usr/bin/osascript").is_file():
        implementation = _choose_macos
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        zenity = shutil.which("zenity")
        if zenity is not None:
            implementation = lambda: _choose_zenity(zenity)

    if implementation is None:
        return None

    def choose() -> str | None:
        if not _DIALOG_LOCK.acquire(blocking=False):
            raise NativeFolderChooserError("A Folder picker is already open.")
        try:
            return implementation()
        finally:
            _DIALOG_LOCK.release()

    return choose


__all__ = [
    "HostFolderChooser",
    "NativeFolderChooserError",
    "default_host_folder_chooser",
]
