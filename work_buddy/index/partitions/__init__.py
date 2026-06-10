"""Fallback home for partition adapters with no natural domain package.

Partitions WITH a domain home live there (``knowledge/partition.py``,
``vault_index/partition.py``). The IR sources (conversation, projects, chrome, summary,
task_note) are adapted here via the generic ``IRSourcePartition`` wrapper, since they
have no single domain package. See ``bootstrap.py`` for registration.
"""
