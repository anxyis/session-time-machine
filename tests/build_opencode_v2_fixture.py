#!/usr/bin/env python3
"""Build the opencode v2 fixture inside tests/fixtures/opencode.db.

opencode 1.19 moved NEW sessions into `session_v2` (registry) + `session_message`
(messages, with their parts stored INLINE in `data.content`) and left the legacy
`session` / `message` / `part` tables holding the old ones. The fixture corpus used
to carry only the legacy shape, so CI never exercised the v2 reader -- and the v2
reader did not exist until 2026-11-14.

This adds a synthetic, SANITIZED v2 session (ses_fixture02) to the same fixture db, so
one fixture file covers both schemas and the CI suite tests the reader that actually
runs on a modern opencode install.

Everything here is invented. There are no real transcripts, no paths, no secrets.

Run:  python3 tests/build_opencode_v2_fixture.py
"""

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "fixtures", "opencode.db")

# The real v2 table shapes (column names only -- the reader never depends on order).
SESSION_V2_DDL = """
CREATE TABLE IF NOT EXISTS session_v2 (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    workspace_id TEXT,
    parent_id TEXT,
    fork_session_id TEXT,
    fork_boundary INTEGER,
    slug TEXT,
    directory TEXT,
    path TEXT,
    title TEXT,
    version TEXT,
    share_url TEXT,
    summary_additions INTEGER,
    summary_deletions INTEGER,
    summary_files INTEGER,
    summary_diffs INTEGER,
    metadata TEXT,
    cost REAL,
    tokens_input INTEGER,
    tokens_output INTEGER,
    tokens_reasoning INTEGER,
    tokens_cache_read INTEGER,
    tokens_cache_write INTEGER,
    revert INTEGER,
    permission TEXT,
    agent TEXT,
    model TEXT,
    time_created INTEGER,
    time_updated INTEGER,
    time_compacting INTEGER,
    time_archived INTEGER,
    time_suspended INTEGER,
    resume_attempts INTEGER,
    time_idle INTEGER,
    time_viewed INTEGER,
    idle_outcome TEXT,
    time_busy INTEGER
)
"""

SESSION_MESSAGE_DDL = """
CREATE TABLE IF NOT EXISTS session_message (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    type TEXT,
    seq INTEGER,
    time_created INTEGER,
    time_updated INTEGER,
    data TEXT
)
"""

SESSION_PART_DDL = """
CREATE TABLE IF NOT EXISTS session_part (
    message_id TEXT,
    session_id TEXT,
    ordinal INTEGER,
    type TEXT,
    call_id TEXT,
    time_created INTEGER,
    time_updated INTEGER,
    data TEXT
)
"""

SID = "ses_fixture02"
MODEL = '{"id":"fixture-model","providerID":"opencode"}'


def msg(mid, mtype, seq, ts, data):
    return (mid, SID, mtype, seq, ts, ts, json.dumps(data, separators=(",", ":")))


def build():
    if not os.path.exists(DB):
        sys.exit(f"fixture db not found: {DB}")
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute(SESSION_V2_DDL)
    cur.execute(SESSION_MESSAGE_DDL)
    cur.execute(SESSION_PART_DDL)

    cur.execute("DELETE FROM session_v2 WHERE id=?", (SID,))
    cur.execute("DELETE FROM session_message WHERE session_id=?", (SID,))
    cur.execute("DELETE FROM session_part WHERE session_id=?", (SID,))

    cur.execute(
        "INSERT INTO session_v2 (id, directory, title, version, agent, model,"
        " time_created, time_updated) VALUES (?,?,?,?,?,?,?,?)",
        (SID, "/tmp/fixture", "fixture v2 session", "1.19.0", "build", MODEL, 2000, 2600),
    )

    # 1. the human asks something
    cur.execute(
        "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
        " VALUES (?,?,?,?,?,?,?)",
        msg("msg_v2_1", "user", 1, 2000, {
            "time": {"created": 2000},
            "text": "what is up",
            "files": [],
            "agents": [],
        }))

    # 2. the model thinks, answers, and calls a tool -- the v2 part shapes:
    #    reasoning{text} / text{text} / tool{name,state{status,input,content[]}}
    cur.execute(
        "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
        " VALUES (?,?,?,?,?,?,?)",
        msg("msg_v2_2", "assistant", 2, 2001, {
            "time": {"created": 2001},
            "agent": "build",
            "model": {"id": "fixture-model", "providerID": "opencode"},
            "content": [
                {"type": "reasoning", "text": "thinking about it"},
                {"type": "text", "text": "one moment"},
                {"type": "tool", "id": "call_v2_1", "name": "read",
                 "state": {"status": "completed", "input": {"path": "a.txt"},
                           "content": [{"type": "text", "text": "file contents here"}]}},
            ],
        }))

    # 3. the tool result comes back inside the same assistant message (v2 keeps
    #    parts inline, so the output block lives on the tool part's state)
    cur.execute(
        "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
        " VALUES (?,?,?,?,?,?,?)",
        msg("msg_v2_3", "assistant", 3, 2002, {
            "time": {"created": 2002},
            "agent": "build",
            "model": {"id": "fixture-model", "providerID": "opencode"},
            "content": [
                {"type": "text", "text": "here you go"},
            ],
        }))

    # 4. a system row -- must not be read as the human's message
    cur.execute(
        "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
        " VALUES (?,?,?,?,?,?,?)",
        msg("msg_v2_4", "system", 4, 2003, {
            "time": {"created": 2003},
            "text": "system notice",
        }))

    con.commit()
    n_sess = cur.execute("SELECT count(*) FROM session_v2").fetchone()[0]
    n_msg = cur.execute("SELECT count(*) FROM session_message WHERE session_id=?", (SID,)).fetchone()[0]
    con.close()
    print(f"opencode v2 fixture ready: {n_sess} v2 session(s), {n_msg} v2 message(s) in {DB}")


if __name__ == "__main__":
    build()
