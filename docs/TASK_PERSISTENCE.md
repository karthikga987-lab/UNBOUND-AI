# Task persistence and restart recovery

The normal FastAPI application uses a dedicated SQLite task database. It is
separate from the conversation, reminder, document, and browser replay-state
databases. The default path is
%LOCALAPPDATA%\UNBOUND_AI\tasks.sqlite3 on Windows
(XDG_DATA_HOME/UNBOUND_AI/tasks.sqlite3 elsewhere). Set
UNBOUND_TASK_DB before starting the backend to use another path; tests use
temporary paths.

The repository creates its tables additively with CREATE TABLE IF NOT EXISTS,
enables WAL, foreign keys, a 10-second busy timeout, and secure deletion. Tasks
and their complete step/approval/evidence snapshots are stored as validated
Pydantic JSON records; ordered events and private execution metadata have
separate task-scoped tables. GET /api/tasks?limit=100 returns the latest
public-redacted records, and existing task/event routes load the persisted
records. The React Task history tab reads this endpoint and opens a selected
task in the existing workspace.

Recovery is fail-closed. Tasks interrupted while executing/verifying, or with
a running step, become needs_attention; a step with an uncertain external
outcome is marked failed and is never replayed automatically. Browser replay
approvals are cancelled because the trusted continuation grant exists only in
the running process. Other pending approvals are preserved only until their
existing 10-minute approval lifetime expires. Approved actions remain bound to
the stored task, step, tool, and exact arguments, and the consumed-approval set
is persisted.

SQLite files are NOT encrypted at rest. Task snapshots can include exact tool
arguments and local file paths. Keep the per-user data directory protected
by Windows account permissions, include it only in backups you explicitly
authorize, and do not place it in the repository. The local API still has no
user authentication; loopback, Host, Origin, and body-size checks are not a
replacement for authentication on a multi-user machine.
