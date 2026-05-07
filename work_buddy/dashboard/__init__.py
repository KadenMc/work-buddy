"""Work-buddy dashboard — web UI for system observability.

Sidecar-managed Flask service that serves an extensible single-page
dashboard aggregating state from all work-buddy subsystems.
"""

# Auto-import every form-schema module so register_schema() runs at
# import time. The interact capability and the contract test rely on
# the registry being populated whenever ``work_buddy.dashboard`` is
# imported (which happens before any consumer queries the registry).
# Each new form consumer adds a sibling module + an import line here.
from work_buddy.dashboard import forms_jobs  # noqa: F401
