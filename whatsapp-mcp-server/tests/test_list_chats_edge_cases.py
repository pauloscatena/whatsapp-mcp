"""Additional edge-case coverage for list_chats() beyond the
include_last_message regression suite (tests/test_list_chats_include_last_message.py).

These were written during QA review of that fix to probe cases the reviewer
flagged as plausible in production:
  - chats.last_message_time IS NULL
  - two messages in the same chat sharing the exact same timestamp value
    that equals chats.last_message_time (duplicate-row risk from the
    equality LEFT JOIN)
  - LIKE wildcard metacharacters (%, _) coming from user-supplied `query`
  - negative `page` / zero `limit`

Some of these reveal PRE-EXISTING behavior that is unrelated to the
include_last_message fix (the LEFT JOIN clause itself was not touched by
that fix - see whatsapp.py list_chats). They are recorded here as failing
tests (xfail with strict=True) rather than silently skipped, so the gap is
visible in the suite instead of just in a report.
"""
import sqlite3

import pytest

SCHEMA_SQL = """
CREATE TABLE chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TIMESTAMP
);

CREATE TABLE messages (
    id TEXT,
    chat_jid TEXT,
    sender TEXT,
    content TEXT,
    timestamp TIMESTAMP,
    is_from_me BOOLEAN,
    media_type TEXT,
    PRIMARY KEY (id, chat_jid),
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
"""


@pytest.fixture
def db_with_null_time(tmp_path):
    db_path = tmp_path / "messages.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
        ("nulltime@s.whatsapp.net", "NullTimeChat", None),
    )
    conn.execute(
        "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
        ("normal@s.whatsapp.net", "NormalChat", "2024-01-01T08:00:00"),
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def wa_null_time(load_whatsapp, db_with_null_time):
    return load_whatsapp(MESSAGES_DB_PATH=str(db_with_null_time))


def test_chat_with_null_last_message_time_does_not_crash(wa_null_time):
    """A chat that has never received a message has last_message_time NULL
    in the real schema. list_chats must not raise and must surface it with
    last_message_time=None (not crash on datetime.fromisoformat(None))."""
    chats = wa_null_time.list_chats(include_last_message=True, limit=10)
    by_jid = {c.jid: c for c in chats}
    assert "nulltime@s.whatsapp.net" in by_jid
    null_chat = by_jid["nulltime@s.whatsapp.net"]
    assert null_chat.last_message_time is None
    assert null_chat.last_message is None


@pytest.fixture
def db_with_duplicate_timestamps(tmp_path):
    """Two distinct messages in the SAME chat sharing the exact timestamp
    that also equals chats.last_message_time. This is plausible in
    production: WhatsApp timestamps used here are second-granularity, and a
    busy chat (or two participants sending within the same second) can
    produce two rows with an identical `messages.timestamp` string."""
    db_path = tmp_path / "messages.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
        ("dup@s.whatsapp.net", "DupChat", "2024-01-05T10:00:00"),
    )
    conn.executemany(
        """INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            ("m1", "dup@s.whatsapp.net", "a@s.whatsapp.net", "msg1", "2024-01-05T10:00:00", 0),
            ("m2", "dup@s.whatsapp.net", "b@s.whatsapp.net", "msg2", "2024-01-05T10:00:00", 1),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def wa_dup(load_whatsapp, db_with_duplicate_timestamps):
    return load_whatsapp(MESSAGES_DB_PATH=str(db_with_duplicate_timestamps))


@pytest.mark.xfail(
    reason=(
        "PRE-EXISTING bug, unrelated to the include_last_message fix: the "
        "LEFT JOIN in list_chats() matches on `chats.last_message_time = "
        "messages.timestamp` alone (no message id / uniqueness guarantee). "
        "When two messages in the same chat share that exact timestamp, the "
        "join fans out and the SAME chat is returned twice in the result "
        "list, once per matching message row."
    ),
    strict=True,
)
def test_duplicate_message_timestamp_does_not_duplicate_chat(wa_dup):
    chats = wa_dup.list_chats(include_last_message=True, limit=10)
    jids = [c.jid for c in chats]
    assert jids.count("dup@s.whatsapp.net") == 1, (
        f"expected chat to appear exactly once, got {jids.count('dup@s.whatsapp.net')} "
        f"occurrences: {chats}"
    )


def test_duplicate_message_timestamp_include_last_message_false_is_unaffected(wa_dup):
    """Sanity check: since include_last_message=False no longer joins
    `messages` at all (that's the fix under review), the duplicate-timestamp
    fan-out above cannot happen on this path - confirms the bug is scoped to
    the LEFT JOIN branch, not introduced by the fix."""
    chats = wa_dup.list_chats(include_last_message=False, limit=10)
    jids = [c.jid for c in chats]
    assert jids.count("dup@s.whatsapp.net") == 1


@pytest.fixture
def db_for_like_wildcards(tmp_path):
    db_path = tmp_path / "messages.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
        [
            ("target@s.whatsapp.net", "TargetChat", "2024-01-01T08:00:00"),
            ("other@s.whatsapp.net", "OtherChat", "2024-01-02T08:00:00"),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def wa_like(load_whatsapp, db_for_like_wildcards):
    return load_whatsapp(MESSAGES_DB_PATH=str(db_for_like_wildcards))


@pytest.mark.xfail(
    reason=(
        "PRE-EXISTING (not introduced by the fix): `query` is interpolated "
        "into a LIKE pattern as f'%{query}%' without escaping user-supplied "
        "'%'/'_' metacharacters. A query containing '_' acts as a "
        "single-char wildcard and can match chats the user did not intend "
        "to search for, e.g. '_argetChat' incorrectly matches 'TargetChat'."
    ),
    strict=True,
)
def test_query_underscore_is_not_treated_as_like_wildcard(wa_like):
    chats = wa_like.list_chats(query="_argetChat")
    jids = {c.jid for c in chats}
    assert jids == set(), f"expected no match for a literal '_', got {jids}"


def test_negative_page_does_not_crash_and_behaves_like_page_zero(wa_like):
    """SQLite clamps a negative OFFSET to 0, so page=-1 silently behaves
    like page=0 instead of erroring - documenting the actual (permissive)
    behavior rather than assuming it raises."""
    page_negative = wa_like.list_chats(page=-1, limit=10)
    page_zero = wa_like.list_chats(page=0, limit=10)
    assert [c.jid for c in page_negative] == [c.jid for c in page_zero]


def test_limit_zero_returns_no_rows(wa_like):
    chats = wa_like.list_chats(limit=0)
    assert chats == []


@pytest.fixture
def db_missing_messages_table(tmp_path):
    """A store where the `messages` table doesn't exist at all - simulates
    any schema/IO problem, not the one specific bug that was fixed."""
    db_path = tmp_path / "messages.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP)")
    conn.execute(
        "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
        ("a@s.whatsapp.net", "A", "2024-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def wa_missing_table(load_whatsapp, db_missing_messages_table):
    return load_whatsapp(MESSAGES_DB_PATH=str(db_missing_messages_table))


def test_any_sqlite_error_is_silently_swallowed_as_empty_list(wa_missing_table, capsys):
    """RISK, not a regression from this fix: list_chats() catches the broad
    `sqlite3.Error` and returns [] for ANY database error (missing table,
    locked db, corruption, ...), not just the specific 'no such column' bug
    that was just fixed. The only signal is a print() to stdout, which an
    MCP client/agent will never see - a chat that exists (`a@s.whatsapp.net`)
    silently vanishes instead of surfacing an error. This is the exact
    failure mode that hid the original bug; it is not closed off by this fix
    and could mask a *different* real error the same way in the future."""
    result = wa_missing_table.list_chats(include_last_message=True, limit=10)
    assert result == []  # documents current (risky) behavior, not desired behavior
    captured = capsys.readouterr()
    assert "Database error" in captured.out
    assert "no such table: messages" in captured.out
