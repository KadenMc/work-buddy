# Chrome Tab Exporter — Setup Guide

Set up the Work Buddy Chrome Tab Exporter on a new Windows machine. This enables the context collector to see open Chrome tabs, browsing history, recently closed tabs, and on-demand page content.

## Prerequisites

- Google Chrome (any recent version)
- Python 3.11+ with conda environment `work-buddy`
- The `work-buddy` repo cloned to your machine

## Architecture

```
Chrome Extension (background.js)
  │  Polls every 5s: "is there a request?"
  │  If yes: queries tabs, history, sessions; sends to native host
  │  If content requested: injects script into specific tabs
  ▼
Native Messaging Host (host.bat → host.py)
  │  Receives snapshot JSON via Chrome's native messaging protocol
  │  Passes request params (since/until, tab_ids) to extension
  │  Writes .chrome_tabs.json to repo root
  ▼
Python Collector (collect --only chrome)
  │  Creates .chrome_tabs_request file (JSON with params)
  │  Waits for .chrome_tabs.json to update
  │  Produces chrome_summary.md in context pack
```

**Permissions:** `tabs`, `history`, `sessions`, `tabGroups`, `scripting`, `<all_urls>`, `nativeMessaging`, `alarms`

## Setup Steps

### Step 1: Generate extension icons

```powershell
cd <repo-root>\chrome_extension
conda activate work-buddy
python generate_icons.py
```

### Step 2: Load extension in Chrome

1. Open `chrome://extensions/`
2. Enable **Developer mode** (toggle in top-right)
3. Click **Load unpacked**
4. Select the `chrome_extension/` directory
5. **Copy the extension ID** (32-character string shown under the extension name)

### Step 3: Register the native messaging host

**⚠️ CRITICAL: Run this from a REGULAR PowerShell terminal, NOT from Claude Code Desktop.**

Claude Code Desktop is a packaged Windows app with virtualized registry/filesystem. Any registration done from Claude's terminal goes to a private per-app store that normal Chrome cannot see.

```powershell
cd <repo-root>\work_buddy\chrome_native_host
conda activate work-buddy
python install.py --extension-id <PASTE_YOUR_EXTENSION_ID>
```

This creates:
- A `.bat` wrapper at `work_buddy/chrome_native_host/host.bat`
- A manifest at `%APPDATA%\Google\Chrome\NativeMessagingHosts\com.work_buddy.tabs.json`
- A registry key at `HKCU\SOFTWARE\Google\Chrome\NativeMessagingHosts\com.work_buddy.tabs`

### Step 4: Restart Chrome

Close Chrome completely and reopen it. The extension should now be able to communicate with the native host.

### Step 5: Verify

Run the context collector:

```powershell
cd <repo-root>
conda activate work-buddy
collect --only chrome
```

Check the output in `agents/<session>/context/chrome_summary.md` — it should list all open tabs, browsing history, and recently closed tabs.

To test with a time range:

```powershell
collect --since 2026-04-03T05:00:00 --until 2026-04-03T17:00:00 --only chrome
```

## Troubleshooting

### "Specified native messaging host not found"

1. **Did you run `install.py` from a regular terminal?** If you ran it from Claude Code Desktop, the registration went to a virtualized store. Re-run from a regular PowerShell.

2. **Does the extension ID match?** Check `chrome://extensions/` for the current ID, then re-run `install.py --extension-id <correct_id>`.

3. **Is the manifest valid?** Check `%APPDATA%\Google\Chrome\NativeMessagingHosts\com.work_buddy.tabs.json` exists and contains valid JSON with the correct extension ID in `allowed_origins`.

4. **Is the registry key set?** Run `reg query "HKCU\SOFTWARE\Google\Chrome\NativeMessagingHosts\com.work_buddy.tabs" /ve` — it should show the path to the manifest file.

5. **Restart Chrome fully.** Chrome menu → Exit (not just X), or kill via Task Manager, then reopen.

### Extension shows no errors but `.chrome_tabs.json` isn't created

The on-demand system requires the collector to create a `.chrome_tabs_request` file. Run `collect --only chrome` to trigger it.

### Checking the extension console

1. Go to `chrome://extensions/`
2. Find "Work Buddy Tab Exporter"
3. Click **Service worker** link
4. Check the Console tab for success/error messages

## Files Reference

| File | Purpose |
|------|---------|
| `chrome_extension/manifest.json` | Chrome extension manifest (MV3) |
| `chrome_extension/background.js` | Service worker: check/export cycle |
| `chrome_extension/generate_icons.py` | Creates placeholder icon PNGs |
| `work_buddy/chrome_native_host/host.py` | Native messaging host (receives tabs, writes JSON) |
| `work_buddy/chrome_native_host/host.bat` | Windows wrapper for host.py |
| `work_buddy/chrome_native_host/install.py` | Registers the native host with Chrome |
| `work_buddy/collectors/chrome_collector.py` | Python collector (creates request, reads result) |

## Uninstall

1. Remove extension from `chrome://extensions/`
2. Delete registry key: `reg delete "HKCU\SOFTWARE\Google\Chrome\NativeMessagingHosts\com.work_buddy.tabs" /f`
3. Delete manifest: `del "%APPDATA%\Google\Chrome\NativeMessagingHosts\com.work_buddy.tabs.json"`
