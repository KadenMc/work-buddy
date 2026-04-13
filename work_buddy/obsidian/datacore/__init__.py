"""Datacore plugin runtime integration via eval_js bridge.

Provides structured vault querying: typed objects (@page, @section, @task, etc.),
containment queries (childof/parentof), tag/frontmatter filters, and a JSON
query plan compiler for agent-friendly NL-to-structured-query workflows.

See README.md in this directory for the full integration docs.
"""

from work_buddy.obsidian.datacore.env import (  # noqa: F401
    check_ready,
    evaluate,
    fullquery,
    get_page,
    query,
    schema_summary,
    validate_query,
)
from work_buddy.obsidian.datacore.compiler import (  # noqa: F401
    compile_plan,
    validate_plan,
    CompileError,
)
