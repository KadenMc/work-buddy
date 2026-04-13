"""Integration with the Smart Connections ecosystem (Brian Petro).

This package provides programmatic access to Smart Environment runtime
objects and data structures via the obsidian-work-buddy bridge (eval_js).

Located under work_buddy.obsidian because the entire Smart ecosystem
is Obsidian-native — all access flows through the Obsidian runtime.

See README.md in this directory for the full integration map.
"""

from work_buddy.obsidian.smart.env import (  # noqa: F401
    check_ready,
    create_context_pack,
    drain_console,
    embed_batch,
    embed_text,
    find_related,
    get_item_content,
    get_workspace_context,
    hybrid_search,
    install_console_capture,
    lookup_context,
    monitor_model_config,
    search_with_filter,
    semantic_search,
    wait_until_ready,
)
from work_buddy.obsidian.smart.diagnostics import (  # noqa: F401
    connect_pro_errors,
    embed_queue_status,
    heap_pressure,
    read_event_logs,
    smart_health_report,
)
