"""Built-in ops — executable callables registered under stable ``op.wb.*`` IDs.

Each module in this package registers its ops at import time via
``work_buddy.mcp_server.op_registry.register_op``. ``load_builtin_ops`` imports
every module here, so adding a new ops module is enough to register its ops —
no central list to update.

The package is organized one module per capability category (``tasks_ops``,
``context_ops``, …).
"""
