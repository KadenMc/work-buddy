# Chrome Tab Exporter - Build Log

**Date:** 2026-04-01
**Purpose:** Expose all Chrome tabs to the work-buddy context system via a Chrome extension + native messaging host.

## Why not CDP?

The original approach (handoff doc) used Chrome DevTools Protocol (CDP) via `--remote-debugging-port`. Chrome 146+ blocks this on the default user profile for security. A Chrome extension is the cleanest remaining path to enumerate all tabs from the user's live session.

## Architecture

```
Chrome Extension (background.js)
    |  queries chrome.tabs.query({}) every 30 seconds
    |  sends JSON via chrome.runtime.sendNativeMessage()
    v
Native Messaging Host (host.py)
    |  receives JSON on stdin (length-prefixed protocol)
    |  writes to disk
    v
.chrome_tabs.json
    |  read by any collector
    v
work_buddy context system
```

## What was created

### Chrome extension (`chrome_extension/`)

| File | Purpose |
|------|---------|
| `manifest.json` | Manifest V3. Permissions: `tabs`, `nativeMessaging`, `alarms`, `tabGroups`. |
| `background.js` | Service worker. Queries all tabs every 30s via `chrome.alarms`. Sends snapshot to native host via `sendNativeMessage`. |
| `generate_icons.py` | Creates placeholder icon PNGs. Run once before loading the extension. |

### Native messaging host (`work_buddy/chrome_native_host/`)

| File | Purpose |
|------|---------|
| `host.py` | Receives tab snapshot from the extension via stdin, writes to `.chrome_tabs.json`. |
| `install.py` | Registers the native messaging host with Chrome (creates manifest + Windows registry entry). |
| `com.work_buddy_tabs.json` | Template manifest (reference only; `install.py` generates the real one). |

### Other changes

- `.gitignore` updated to exclude `.chrome_tabs.json` and `.chrome_tabs.tmp`

## Installation steps

### 1. Generate icons

```powershell
cd <repo-root>\chrome_extension
conda activate work-buddy
python generate_icons.py
```

### 2. Load the extension in Chrome

1. Open `chrome://extensions/`
2. Enable **Developer mode** (toggle in top-right)
3. Click **Load unpacked**
4. Select the folder: `<repo-root>\chrome_extension`
5. The extension should appear as "Work Buddy Tab Exporter"
6. **Copy the extension ID** (a 32-character string like `abcdefghijklmnop...`)

### 3. Register the native messaging host

```powershell
cd <repo-root>\work_buddy\chrome_native_host
conda activate work-buddy
python install.py --extension-id <PASTE_YOUR_EXTENSION_ID>
```

This does three things:
- Creates a `.bat` wrapper at `work_buddy/chrome_native_host/host.bat`
- Writes the host manifest to `%APPDATA%\Google\Chrome\NativeMessagingHosts\work_buddy_tabs.json`
- Creates a registry key at `HKCU\SOFTWARE\Google\Chrome\NativeMessagingHosts\work_buddy_tabs`

### 4. Restart Chrome

Close and reopen Chrome completely (all windows). The extension's service worker needs Chrome to recognize the newly registered native host.

### 5. Verify it works

After ~30 seconds, check:
```powershell
type <repo-root>\.chrome_tabs.json
```

You should see JSON with all your open tabs.

If the file is not created, check:
- `chrome://extensions/` -- click "Service worker" link under the extension, check the console for errors
- Ensure the extension ID in the native host manifest matches the actual extension ID
- Ensure Chrome was fully restarted after registration

## JSON output format

```json
{
  "captured_at": "2026-04-01T12:00:00.000Z",
  "tab_count": 42,
  "window_ids": [1, 2],
  "tabs": [
    {
      "windowId": 1,
      "tabId": 123,
      "index": 0,
      "title": "Example Page",
      "url": "https://example.com",
      "pinned": false,
      "active": true,
      "status": "complete",
      "groupId": null,
      "group": null,
      "favIconUrl": "https://example.com/favicon.ico",
      "lastAccessed": 1711929600000,
      "audible": false,
      "mutedInfo": { "muted": false }
    }
  ],
  "host_written_at": "2026-04-01T12:00:01.000000+00:00"
}
```

## Design decisions

1. **Native messaging over CDP**: CDP is blocked on the default profile in Chrome 146+. Native messaging is the official extension-to-local-program communication channel.

2. **sendNativeMessage (one-shot) over connectNative (persistent port)**: Simpler. Each alarm fires, sends one message, gets one response. No connection lifecycle to manage.

3. **30-second polling interval**: Frequent enough for context gathering, infrequent enough to be invisible. Chrome alarms have a minimum of ~30 seconds for periodic alarms anyway.

4. **File-based output**: The collector just reads `.chrome_tabs.json`. No HTTP server, no socket, no IPC complexity. The host writes atomically (write temp, rename).

5. **Read-only**: The extension only has `tabs` permission. It cannot modify, close, or navigate tabs. This is deliberate -- the handoff doc recommends starting with visibility before adding control.

6. **tabGroups permission**: Optional but useful. Chrome tab groups carry a title and color that can help categorize context. If the API is unavailable, the extension degrades gracefully.

## Troubleshooting

### "Native host has exited" error in console
- The `.bat` wrapper path or Python path may be wrong
- Run `host.bat` manually in a terminal to see if Python starts

### Empty or missing `.chrome_tabs.json`
- Check that the native host manifest at `%APPDATA%\Google\Chrome\NativeMessagingHosts\work_buddy_tabs.json` exists and has the correct extension ID in `allowed_origins`
- Check the registry: `HKCU\SOFTWARE\Google\Chrome\NativeMessagingHosts\work_buddy_tabs`

### Extension shows errors
- Open `chrome://extensions/`, click "Service worker" under the extension
- Check the console for error messages
- Common issue: mismatched host name (`work_buddy_tabs` must match in manifest.json, background.js, and the native host manifest)

## Future work

- Integrate with `work_buddy/collectors/` to produce `chrome_summary.md`
- Add optional on-demand trigger (extension popup or keyboard shortcut)
- Consider adding `activeTab` permission later for page content extraction (requires explicit consent per the CLAUDE.md contract)
