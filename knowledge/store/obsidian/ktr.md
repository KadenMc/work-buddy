---
name: Keep the Rhythm Integration
kind: integration
description: Writing activity tracking via KTR plugin (v0.2.8) -- per-file word/char deltas in 5-minute buckets
tags:
- obsidian
- ktr
- keep-the-rhythm
- writing
- activity
- eval_js
aliases:
- keep the rhythm
- ktr plugin
- writing activity
- word count tracking
parents:
- obsidian
- obsidian
---

Writing activity tracking via Keep the Rhythm plugin (v0.2.8) + eval_js bridge.

## What it provides

KTR tracks per-file, per-day writing activity with 5-minute bucketed word/character deltas. Used by hot_files for writing intensity scoring.

Important: KTR only tracks files opened in Obsidian's editor. External changes, sync-created files, and script-modified files are invisible. This is a writing intensity signal, not a complete file change log.

## Data Store

Plugin data at plugin.data: schema ("0.2"), settings, stats ({currentStreak, highestStreak, daysWithCompletedGoal, dailyActivity}).

DailyActivity record: date (YYYY-MM-DD), filePath, wordCountStart, charCountStart, changes ([{timeKey: "HH:MM", w: int, c: int}]), id (sequential).

## Python API

check_ready -> {ready, version, activity_count, unique_files, current_streak}. get_hot_files(since_date, until_date?, limit=20) -> ranked files by composite score. get_file_activity(file_path, since_date, until_date?) -> detailed writing timeline.

## Stale Warnings

- KTR main.js is bundled/minified -- runtime probing only
- Data schema ("0.2") may change between plugin versions
- changes[].timeKey uses 5-minute bucketing rounded to nearest 5 min
- Files only enter KTR when opened in editor; unopened files have no records
