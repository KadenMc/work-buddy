/**
 * Work Buddy Tab Exporter - Service Worker
 *
 * On-demand export of tab metadata, browsing history, and session data
 * via native messaging. The extension checks every 5 seconds whether a
 * request file exists. When found, it queries tabs, history, and
 * sessions, then sends the snapshot to the native host.
 *
 * Also supports tab mutations (close, group, move) requested via the
 * same file-based signaling protocol with request_action="mutate".
 */

const HOST_NAME = "com.work_buddy.tabs";
const CHECK_ALARM = "check_request";
const CHECK_INTERVAL_MINUTES = 5 / 60; // 5 seconds
const SNAPSHOT_ALARM = "periodic_snapshot";
const SNAPSHOT_INTERVAL_MINUTES = 5; // 5 minutes

// ── Tab creation tracking ───────────────────────────────────────
// The Tab API has no "createdAt" field, so we track it ourselves.

const tabCreationTimes = new Map();

chrome.tabs.onCreated.addListener((tab) => {
  tabCreationTimes.set(tab.id, Date.now());
});

chrome.tabs.onRemoved.addListener((tabId) => {
  tabCreationTimes.delete(tabId);
});

// ── Data collection ─────────────────────────────────────────────

async function getFullSnapshot(options = {}) {
  const { since, until } = options;

  // --- Tabs ---
  const tabs = await chrome.tabs.query({});

  let groupMap = {};
  try {
    if (chrome.tabGroups) {
      const groups = await chrome.tabGroups.query({});
      for (const g of groups) {
        groupMap[g.id] = {
          id: g.id,
          title: g.title || "",
          color: g.color || "",
          collapsed: g.collapsed || false,
          windowId: g.windowId,
        };
      }
    }
  } catch (_) {}

  const tabData = tabs.map((tab) => ({
    windowId: tab.windowId,
    tabId: tab.id,
    index: tab.index,
    title: tab.title || "",
    url: tab.url || "",
    pinned: tab.pinned || false,
    active: tab.active || false,
    status: tab.status || "",
    groupId:
      tab.groupId !== undefined && tab.groupId !== -1 ? tab.groupId : null,
    group:
      tab.groupId !== undefined && tab.groupId !== -1
        ? groupMap[tab.groupId] || null
        : null,
    favIconUrl: tab.favIconUrl || "",
    lastAccessed: tab.lastAccessed || null,
    createdAt: tabCreationTimes.get(tab.id) || null,
    audible: tab.audible || false,
    mutedInfo: tab.mutedInfo || null,
    openerTabId: tab.openerTabId || null,
  }));

  // --- Browsing history ---
  let history = [];
  try {
    const startTime = since ? new Date(since).getTime() : Date.now() - 24 * 60 * 60 * 1000;
    const endTime = until ? new Date(until).getTime() : Date.now();

    const historyItems = await chrome.history.search({
      text: "",
      startTime,
      endTime,
      maxResults: 500,
    });

    history = historyItems.map((item) => ({
      url: item.url || "",
      title: item.title || "",
      lastVisitTime: item.lastVisitTime || null,
      visitCount: item.visitCount || 0,
      typedCount: item.typedCount || 0,
    }));
  } catch (e) {
    console.warn("Work Buddy: history query failed:", e);
  }

  // --- Recently closed tabs ---
  let recentlyClosed = [];
  try {
    const sessions = await chrome.sessions.getRecentlyClosed();
    recentlyClosed = sessions
      .filter((s) => s.tab)
      .map((s) => ({
        title: s.tab.title || "",
        url: s.tab.url || "",
        closedAt: s.lastModified ? s.lastModified * 1000 : null, // seconds → ms
        windowId: s.tab.windowId,
      }));
  } catch (e) {
    console.warn("Work Buddy: sessions query failed:", e);
  }

  return {
    captured_at: new Date().toISOString(),
    tab_count: tabData.length,
    window_ids: [...new Set(tabData.map((t) => t.windowId))],
    tabs: tabData,
    history,
    history_range: {
      since: since || null,
      until: until || null,
    },
    recently_closed: recentlyClosed,
  };
}

// ── Page content extraction ─────────────────────────────────────

/**
 * Fetch text content from specific tabs via content script injection.
 *
 * Handles discarded/frozen tabs by reloading them first. Skips
 * chrome:// and extension:// URLs (can't inject into those).
 *
 * @param {number[]} tabIds - Tab IDs to extract content from
 * @param {number} maxChars - Max characters per tab (default 10000)
 * @returns {Object[]} Array of {tabId, url, title, text, error}
 */
async function getTabContents(tabIds, maxChars = 10000) {
  const results = [];

  for (const tabId of tabIds) {
    try {
      // Get current tab state
      const tab = await chrome.tabs.get(tabId);

      // Skip unparseable URLs
      if (
        !tab.url ||
        tab.url.startsWith("chrome://") ||
        tab.url.startsWith("chrome-extension://") ||
        tab.url.startsWith("about:") ||
        tab.url.startsWith("devtools://")
      ) {
        results.push({
          tabId,
          url: tab.url || "",
          title: tab.title || "",
          text: null,
          error: "Cannot inject into this URL type",
        });
        continue;
      }

      // Handle discarded tabs — reload and wait for them to load
      if (tab.discarded || tab.status === "unloaded") {
        console.log(`Work Buddy: tab ${tabId} is discarded, reloading...`);
        await chrome.tabs.reload(tabId);
        // Wait for tab to finish loading (up to 15 seconds)
        await new Promise((resolve) => {
          const timeout = setTimeout(resolve, 15000);
          const listener = (updatedTabId, changeInfo) => {
            if (updatedTabId === tabId && changeInfo.status === "complete") {
              chrome.tabs.onUpdated.removeListener(listener);
              clearTimeout(timeout);
              resolve();
            }
          };
          chrome.tabs.onUpdated.addListener(listener);
        });
      }

      // Inject and extract text — wrapped in a timeout because frozen tabs
      // cause executeScript to hang forever (promise never resolves/rejects).
      // See: https://github.com/w3c/webextensions/issues/527
      const injectionPromise = chrome.scripting.executeScript({
        target: { tabId },
        func: (maxLen) => {
          const article = document.querySelector("article") || document.querySelector("main");
          const source = article || document.body;
          if (!source) return "";

          const meta = {};
          const desc = document.querySelector('meta[name="description"]');
          if (desc) meta.description = desc.content;
          const ogTitle = document.querySelector('meta[property="og:title"]');
          if (ogTitle) meta.og_title = ogTitle.content;
          const ogDesc = document.querySelector('meta[property="og:description"]');
          if (ogDesc) meta.og_description = ogDesc.content;

          const text = source.innerText.substring(0, maxLen);
          return JSON.stringify({ text, meta });
        },
        args: [maxChars],
      });

      const timeoutPromise = new Promise((_, reject) =>
        setTimeout(() => reject(new Error("Timed out — tab may be frozen")), 10000)
      );

      const injectionResults = await Promise.race([injectionPromise, timeoutPromise]);

      const rawResult = injectionResults?.[0]?.result;
      if (rawResult) {
        const parsed = JSON.parse(rawResult);
        results.push({
          tabId,
          url: tab.url,
          title: tab.title || "",
          text: parsed.text,
          meta: parsed.meta || {},
          error: null,
        });
      } else {
        results.push({
          tabId,
          url: tab.url,
          title: tab.title || "",
          text: null,
          error: "Script returned no result",
        });
      }
    } catch (e) {
      results.push({
        tabId,
        url: "",
        title: "",
        text: null,
        error: e.message || String(e),
      });
    }
  }

  return results;
}

// ── Native messaging ────────────────────────────────────────────

function sendToHost(message) {
  const action = message.action || "unknown";
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, message, (response) => {
      if (chrome.runtime.lastError) {
        console.warn(
          `Work Buddy: native messaging error (action=${action}):`,
          chrome.runtime.lastError.message
        );
        resolve(null);
      } else {
        resolve(response);
      }
    });
  });
}

/**
 * Check if Python collector has requested an export.
 * The check response may include:
 * - since/until for scoping the history query
 * - request_action: "snapshot" (default) or "get_content"
 * - tab_ids: array of tab IDs for content extraction
 * - max_chars: max characters per tab content
 */
async function checkAndExport(trigger = "unknown") {
  const checkResult = await sendToHost({ action: "check" });
  if (!checkResult || !checkResult.requested) return;

  const requestAction = checkResult.request_action || "snapshot";
  console.log(`Work Buddy: [${trigger}] request found: action=${requestAction}`);

  if (requestAction === "mutate") {
    // Tab mutation mode
    const mutation = checkResult.mutation || "unknown";
    const tabIds = checkResult.tab_ids || [];
    console.log(
      `Work Buddy: [request] mutation=${mutation} tab_ids=[${tabIds.join(", ")}]`
    );
    const mutationResult = await applyMutation(checkResult);
    console.log(
      `Work Buddy: [request] mutation result: status=${mutationResult.status}`,
      mutationResult.details
    );
    const result = await sendToHost({
      action: "export",
      request_action: "mutate",
      captured_at: new Date().toISOString(),
      mutation_result: mutationResult,
    });
    if (result && result.status === "ok") {
      console.log("Work Buddy: [request] mutation exported successfully");
    } else {
      console.warn("Work Buddy: [request] mutation export failed:", result);
    }
  } else if (requestAction === "get_content") {
    // Content extraction mode
    const tabIds = checkResult.tab_ids || [];
    const maxChars = checkResult.max_chars || 10000;
    console.log(
      `Work Buddy: [request] content extraction for ${tabIds.length} tabs (max ${maxChars} chars)`
    );
    const contents = await getTabContents(tabIds, maxChars);
    const result = await sendToHost({
      action: "export",
      request_action: "get_content",
      captured_at: new Date().toISOString(),
      tab_contents: contents,
    });
    if (result && result.status === "ok") {
      console.log("Work Buddy: [request] content exported successfully");
    } else {
      console.warn("Work Buddy: [request] content export failed:", result);
    }
  } else {
    // Default: full snapshot (on-demand, requested by Python collector)
    console.log(`Work Buddy: [request] on-demand snapshot requested`);
    const snapshot = await getFullSnapshot({
      since: checkResult.since || null,
      until: checkResult.until || null,
    });
    snapshot.action = "export";
    const exportResult = await sendToHost(snapshot);
    if (exportResult && exportResult.status === "ok") {
      console.log(
        `Work Buddy: [request] snapshot exported (${snapshot.tab_count} tabs)`,
        exportResult
      );
    } else {
      console.warn("Work Buddy: [request] snapshot export failed:", exportResult);
    }
  }
}

// ── Tab mutations ──────────────────────────────────────────────

/**
 * Apply tab mutations requested by the Python agent.
 *
 * Supported mutations:
 * - close_tabs: Remove specified tabs
 * - group_tabs: Create a new tab group or add tabs to existing group
 * - ungroup_tabs: Remove tabs from their group
 * - move_tabs: Move tabs to a specific position/window
 *
 * @param {Object} params - Mutation parameters from request file
 * @returns {Object} Result with status and details
 */
async function applyMutation(params) {
  const mutation = params.mutation || "";
  const results = { mutation, status: "ok", details: {} };

  try {
    switch (mutation) {
      case "close_tabs": {
        const tabIds = params.tab_ids || [];
        if (tabIds.length === 0) {
          results.status = "error";
          results.details = { error: "No tab_ids provided" };
          break;
        }
        // Verify tabs exist before closing
        const existing = [];
        const missing = [];
        for (const id of tabIds) {
          try {
            await chrome.tabs.get(id);
            existing.push(id);
          } catch (_) {
            missing.push(id);
          }
        }
        if (existing.length > 0) {
          await chrome.tabs.remove(existing);
        }
        results.details = {
          closed: existing.length,
          missing: missing.length,
          closed_ids: existing,
          missing_ids: missing,
        };
        break;
      }

      case "group_tabs": {
        const tabIds = params.tab_ids || [];
        const title = params.title || "";
        const color = params.color || "grey";
        const groupId = params.group_id || undefined;

        if (tabIds.length === 0) {
          results.status = "error";
          results.details = { error: "No tab_ids provided" };
          break;
        }

        let newGroupId;
        if (groupId) {
          // Add to existing group
          newGroupId = await chrome.tabs.group({ tabIds, groupId });
        } else {
          // Create new group
          newGroupId = await chrome.tabs.group({ tabIds });
        }

        // Update group properties
        if (title || color) {
          const updateProps = {};
          if (title) updateProps.title = title;
          if (color) updateProps.color = color;
          await chrome.tabGroups.update(newGroupId, updateProps);
        }

        results.details = {
          group_id: newGroupId,
          tab_count: tabIds.length,
          title,
          color,
        };
        break;
      }

      case "ungroup_tabs": {
        const tabIds = params.tab_ids || [];
        if (tabIds.length > 0) {
          await chrome.tabs.ungroup(tabIds);
        }
        results.details = { ungrouped: tabIds.length };
        break;
      }

      case "move_tabs": {
        const tabIds = params.tab_ids || [];
        const windowId = params.window_id;
        const index = params.index !== undefined ? params.index : -1;

        if (tabIds.length === 0) {
          results.status = "error";
          results.details = { error: "No tab_ids provided" };
          break;
        }

        const moveProps = { index };
        if (windowId) moveProps.windowId = windowId;
        await chrome.tabs.move(tabIds, moveProps);
        results.details = { moved: tabIds.length, windowId, index };
        break;
      }

      case "focus_or_create_tab": {
        const url = params.url || "";
        const targetHash = params.target_hash || "";
        if (!url) {
          results.status = "error";
          results.details = { error: "No url provided" };
          break;
        }
        // Find existing tab matching this origin
        const matchingTabs = await chrome.tabs.query({ url: url + "/*" });
        if (matchingTabs.length > 0) {
          // Focus the most recently accessed matching tab
          const sorted = matchingTabs.sort(
            (a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0)
          );
          const tab = sorted[0];
          const newUrl = targetHash ? url + "/" + targetHash : undefined;
          await chrome.tabs.update(tab.id, { active: true, ...(newUrl ? { url: newUrl } : {}) });
          await chrome.windows.update(tab.windowId, { focused: true });
          results.details = { created: false, tab_id: tab.id, focused: true };
        } else {
          // No existing tab — create one
          const fullUrl = targetHash ? url + "/" + targetHash : url;
          const newTab = await chrome.tabs.create({ url: fullUrl });
          results.details = { created: true, tab_id: newTab.id, url: fullUrl };
        }
        break;
      }

      default:
        results.status = "error";
        results.details = { error: `Unknown mutation: ${mutation}` };
    }
  } catch (e) {
    results.status = "error";
    results.details = { error: e.message || String(e) };
  }

  return results;
}

// ── Periodic tab snapshot (rolling ledger) ─────────────────────

// Track when we last captured, so history queries only fetch new visits.
let lastSnapshotTime = Date.now();

/**
 * Capture a tab snapshot with incremental browsing history and send
 * to the native host for persistent ledger storage.
 *
 * Tab metadata: current state of all open tabs.
 * History: only visits since the *previous* snapshot (incremental diff),
 * so each snapshot adds ~0-50 entries rather than the full history.
 */
async function capturePeriodicSnapshot() {
  try {
    const now = Date.now();
    const capturedAt = new Date(now).toISOString();

    // --- Tabs ---
    const tabs = await chrome.tabs.query({});
    const tabData = tabs.map((tab) => ({
      tabId: tab.id,
      windowId: tab.windowId,
      index: tab.index,
      url: tab.url || "",
      title: tab.title || "",
      active: tab.active || false,
      pinned: tab.pinned || false,
      lastAccessed: tab.lastAccessed || null,
      groupId:
        tab.groupId !== undefined && tab.groupId !== -1 ? tab.groupId : null,
    }));

    // --- Incremental history (only visits since last snapshot) ---
    let history = [];
    try {
      const historyItems = await chrome.history.search({
        text: "",
        startTime: lastSnapshotTime,
        endTime: now,
        maxResults: 200,
      });
      history = historyItems
        .filter(
          (item) =>
            item.url &&
            !item.url.startsWith("chrome://") &&
            !item.url.startsWith("chrome-extension://") &&
            !item.url.startsWith("about:") &&
            !item.url.startsWith("devtools://")
        )
        .map((item) => ({
          url: item.url,
          title: item.title || "",
          lastVisitTime: item.lastVisitTime || null,
        }));
    } catch (e) {
      console.warn("Work Buddy: periodic history query failed:", e);
    }

    lastSnapshotTime = now;

    const snapshot = {
      action: "periodic_snapshot",
      captured_at: capturedAt,
      tab_count: tabData.length,
      tabs: tabData,
      history: history,
      history_count: history.length,
    };

    const result = await sendToHost(snapshot);
    if (result && result.status === "ok") {
      console.log(
        `Work Buddy: [timer] periodic snapshot saved (${tabData.length} tabs, ${history.length} history, ledger: ${result.ledger_count})`
      );
    } else {
      console.warn("Work Buddy: [timer] periodic snapshot send failed:", result);
    }
  } catch (e) {
    console.warn("Work Buddy: [timer] periodic snapshot failed:", e);
  }
}

// ── Alarm-based request checking ────────────────────────────────

chrome.alarms.onAlarm.addListener(async (alarm) => {
  try {
    if (alarm.name === CHECK_ALARM) {
      await checkAndExport("timer");
    } else if (alarm.name === SNAPSHOT_ALARM) {
      console.log("Work Buddy: [timer] periodic snapshot alarm fired");
      await capturePeriodicSnapshot();
    }
  } catch (e) {
    console.error(`Work Buddy: alarm handler error (${alarm.name}):`, e);
  }
});

chrome.alarms.create(CHECK_ALARM, {
  delayInMinutes: 0.1,
  periodInMinutes: CHECK_INTERVAL_MINUTES,
});

chrome.alarms.create(SNAPSHOT_ALARM, {
  delayInMinutes: 0.5, // first snapshot 30s after startup
  periodInMinutes: SNAPSHOT_INTERVAL_MINUTES,
});

chrome.runtime.onStartup.addListener(async () => {
  console.log("Work Buddy: [startup] onStartup fired");
  try {
    await checkAndExport("startup");
  } catch (e) {
    console.error("Work Buddy: [startup] error:", e);
  }
});

chrome.runtime.onInstalled.addListener(() => {
  console.log("Work Buddy Tab Exporter installed/updated.");
  getFullSnapshot().then((snapshot) => {
    snapshot.action = "export";
    sendToHost(snapshot);
  });
});
