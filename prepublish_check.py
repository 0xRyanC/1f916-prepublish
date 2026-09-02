#!/usr/bin/env python3
"""TEMPORARY: prepublish_check.py was corrupted by a file:// MCP probe (3098ce02).

Full SUMMARY-verb implementation lives locally and in .prepublish-staging parts.
See SHIP_NOTE_2026-09-02.md. Restore with RESTORE_PUSH_FILES.json content.

This stub exits 2 so callers do not treat a file:// string as a checker.
"""
import sys
print(
    "prepublish_check.py STUB — restore pending; see SHIP_NOTE_2026-09-02.md "
    "and .prepublish-staging/",
    file=sys.stderr,
)
sys.exit(2)
