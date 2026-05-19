---
name: Artifact Registry
kind: capability
description: Return the cross-backend artifact-registry introspection map. For each registered artifact, lists its name, storage kind (FilesystemStorage, SqliteRowsStorage, JsonRecordsStorage, …), lifecycle kind (trigger+action+optional retention), provenance kind (SessionTagged or none), declared capabilities, and the MCP operations it exposes. Single source of truth for 'what does this persisted resource look like.'
capability_name: artifact_registry
category: artifacts
op: op.wb.artifact_registry
schema_version: wb-capability/v1
tags:
- artifacts
- artifact
- registry
aliases:
- artifact registry
- list artifact types
- show backends
- artifact introspection
- artifact map
- registered artifacts
parents:
- artifacts
---
