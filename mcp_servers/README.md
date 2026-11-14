# Read-only source MCP servers

The project provides `git_server`, `tickets_server` and `mail_server`, backed by
the synthetic local corpus. They do not connect to live GitHub/Jira/mail accounts.
All source tools are read-only and redact text before returning it. The ingestion
client validates and redacts again. Git and tickets cannot open mail or fixture keys.

Mail additionally requires one or more `--mail-folder` arguments at startup;
there is no default folder selection. Personal/private folder names, sensitivity
flags, path escapes and symlinks cannot widen its consent scope. Only selected,
normal, expert-involved, in-range messages are returned. `get_message` looks up
an already-scoped ID rather than opening a caller-provided path. No tool sends,
modifies or deletes a message. Attachments and HTML expansion are not supported.

Use `uv run eki ingest --since D --until D --mail-folder Inbox --mail-folder Sent`
to run all three adapters over the configured corpus. Omitting `--mail-folder`
retains the earlier Git/ticket-only pipeline. `eki audit --run ID` shows metadata
for MCP calls made through the application. Standalone connector probes require
a client-supplied application run context for centralized auditing.
