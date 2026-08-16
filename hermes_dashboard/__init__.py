"""hermes-dashboard — static system dashboard generator for Hermes Agent.

Reads the agent's live telemetry (state.db, memory_store.db, cron/jobs.json,
config.yaml, systemd, git) and renders self-contained HTML pages. Everything
agent-specific lives in dashboard.json; the engine itself is stdlib-only.
"""

__version__ = "0.1.0"
