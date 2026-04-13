"""Inter-agent messaging system for work-buddy.

Provides a SQLite-backed message store with an HTTP API for cross-agent
communication.  Project agents send/receive messages via lightweight
curl hooks; work-buddy manages messages through the Python client.
"""
