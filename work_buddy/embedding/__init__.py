"""Shared embedding service for work-buddy.

Loads sentence-transformers model once, serves embedding and search
requests to all agent sessions via HTTP API on localhost.
"""
