---
name: Contract Wip Check
kind: capability
description: Check if active contract count is within the WIP limit (max 3)
capability_name: contract_wip_check
category: contracts
op: op.wb.contract_wip_check
schema_version: wb-capability/v1
tags:
- contracts
- contract
- wip
- check
aliases:
- am I overcommitted
- work in progress limit
- WIP check
- too many contracts
- how many active commitments
- over WIP
parents:
- contracts
requires:
- obsidian
---
