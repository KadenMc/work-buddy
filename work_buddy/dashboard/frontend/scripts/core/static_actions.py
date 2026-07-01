"""Adapters for the static page-skeleton handlers (html.py).

The static HTML in ``html.py`` historically used inline ``on*=`` handlers.
Those are converted to ``data-on-<event>`` attributes dispatched by
``core/delegation.py``; the matching ``window.wbAction`` adapters live here,
kept out of ``html.py`` (which is pure markup) and out of the per-tab modules
(the skeleton spans many of them).

Populated by the dashboard-frontend delegation sweep.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Static page-skeleton action adapters (see html.py) ----
(function () {
    // Each adapter unpacks any data-* args and calls the existing global
    // handler. Registered at load; adapter bodies run at click/change time,
    // so the target globals only need to exist by then (not now).

    // Header
    window.wbAction('cpOpen', function (el) { cpOpen(); });
    window.wbAction('switchTabSettings', function (el) { switchTab('settings'); });

    // Today
    window.wbAction('onTodayContextPresetChange', function (el) { onTodayContextPresetChange(); });
    window.wbAction('loadToday', function (el) { loadToday(); });

    // Chat sidebar
    window.wbAction('wbChatSidebarClose', function (el) { window.wbChatSidebar.close(); });

    // Jobs
    window.wbAction('showAddJobForm', function (el) { showAddJobForm(); });
    window.wbAction('onJobsHelpClick', function (el) { onJobsHelpClick(); });
    window.wbAction('onCronInput', function (el) { onCronInput(); });
    window.wbAction('onJitterInput', function (el) { onJitterInput(); });
    window.wbAction('onJobTypeChange', function (el) { onJobTypeChange(); });
    window.wbAction('onInvokeKindChange', function (el) { onInvokeKindChange(); });
    window.wbAction('onInvokeNameInput', function (el) { onInvokeNameInput(); });
    window.wbAction('onParamsInput', function (el) { onParamsInput(); });
    window.wbAction('hideAddJobForm', function (el) { hideAddJobForm(); });
    window.wbAction('submitAddJobForm', function (el) { submitAddJobForm(); });

    // Chats
    window.wbAction('chatsGlobalSearch', function (el) { chatsGlobalSearch(); });
    window.wbAction('chatsSearchMethodChanged', function (el) { chatsSearchMethodChanged(el.value); });
    window.wbAction('chatsProjectFilterChanged', function (el) { chatsProjectFilterChanged(el.value); });
    window.wbAction('applyChatsFiltersAndSort', function (el) { applyChatsFiltersAndSort(); });
    window.wbAction('chatsToggleAdvanced', function (el) { chatsToggleAdvanced(); });
    window.wbAction('closeChat', function (el) { closeChat(); });
    window.wbAction('chatsInSessionSearch', function (el) { chatsInSessionSearch(); });
    window.wbAction('chatsCloseInSearch', function (el) { chatsCloseInSearch(); });
    window.wbAction('chatsLoadEarlier', function (el) { chatsLoadEarlier(); });
    window.wbAction('chatsLoadLater', function (el) { chatsLoadLater(); });

    // Costs
    window.wbAction('costsProjectChanged', function (el) { costsProjectChanged(el.value); });
    window.wbAction('costsRangeChanged', function (el) { costsRangeChanged(el.value); });
    window.wbAction('costsToggleRateLimitPopover', function (el, e) { costsToggleRateLimitPopover(e); });
    window.wbAction('costsRefresh', function (el) { costsRefresh(el); });

    // Settings
    window.wbAction('switchSettingsSubtab', function (el) { switchSettingsSubtab(el.dataset.subtab); });
    window.wbAction('reprobeAll', function (el) { reprobeAll(el); });

    // Memory
    window.wbAction('switchMemorySubtab', function (el) { switchMemorySubtab(el.dataset.subtab); });

    // Command palette
    window.wbAction('cpOverlayClick', function (el, e) { if (e.target === el) cpClose(); });
})();
"""
