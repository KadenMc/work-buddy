"""Backend implementations of :class:`work_buddy.email.provider.EmailProvider`.

Each backend lives in its own module so heavy-import paths (HTTP clients,
state stores) are taken only when actually used.
"""
