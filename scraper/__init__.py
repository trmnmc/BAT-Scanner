"""BaT Value Map scraper (v0.2, Phase 0/1).

A small, dependency-free pipeline that:
  fetch -> parse -> categorize -> enrich -> validate -> write snapshot

Public surface is intentionally small; see the individual modules.
Run the pipeline with:  python -m scraper
"""

__version__ = "0.2.0"
SCHEMA_VERSION = 1
