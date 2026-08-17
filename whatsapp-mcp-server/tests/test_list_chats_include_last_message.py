"""Regression coverage for list_chats() and the include_last_message flag.

Bug: the SELECT clause in list_chats() always referenced messages.content,
messages.sender and messages.is_from_me, but the `LEFT JOIN messages ...`
clause was only appended when include_last_message was True. Calling
list_chats(include_last_message=False) therefore produced invalid SQL
("no such column: messages.content"), which sqlite3.Error swallowed and
turned into a silently-returned empty list.
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
    filename TEXT,
    url TEXT,
    media_key BLOB,
    file_sha256 BLOB,
    file_enc_sha256 BLOB,
    file_length INTEGER,
    PRIMARY KEY (id, chat_jid),
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
"""

# Four chats, deliberately laid out so that:
#  - "last_active" sort order (last_message_time DESC) differs from name order.
#  - Bob (333) has NO message row at all, exercising LEFT JOIN semantics -
#    the chat must still be returned, with last-message fields as None,
#    regardless of include_last_message.
CHATS = [
    ("111@s.whatsapp.net", "Charlie", "2024-01-01T08:00:00"),
    ("222@s.whatsapp.net", "Alice", "2024-01-03T08:00:00"),
    ("333@s.whatsapp.net", "Bob", "2024-01-02T08:00:00"),
    ("444@g.us", "Delta Group", "2024-01-04T08:00:00"),
]

MESSAGES = [
    # id, chat_jid, sender, content, timestamp, is_from_me
    ("m-charlie", "111@s.whatsapp.net", "111@s.whatsapp.net", "Charlie last msg", "2024-01-01T08:00:00", 0),
    ("m-alice", "222@s.whatsapp.net", "222@s.whatsapp.net", "Alice last msg", "2024-01-03T08:00:00", 0),
    # Bob (333) intentionally has no message rows at all.
    ("m-delta", "444@g.us", "555@s.whatsapp.net", "Delta last msg", "2024-01-04T08:00:00", 1),
]


@pytest.fixture
def rich_db(tmp_path):
    db_path = tmp_path / "messages.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
        CHATS,
    )
    conn.executemany(
        """INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me)
           VALUES (?, ?, ?, ?, ?, ?)""",
        MESSAGES,
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def rich_wa(load_whatsapp, rich_db):
    return load_whatsapp(MESSAGES_DB_PATH=str(rich_db))


def _by_jid(chats):
    return {c.jid: c for c in chats}


# --- (a) include_last_message=True populates last-message fields ----------

def test_include_last_message_true_populates_fields(rich_wa):
    chats = _by_jid(rich_wa.list_chats(include_last_message=True, limit=10))

    alice = chats["222@s.whatsapp.net"]
    assert alice.last_message == "Alice last msg"
    assert alice.last_sender == "222@s.whatsapp.net"
    assert bool(alice.last_is_from_me) is False

    delta = chats["444@g.us"]
    assert delta.last_message == "Delta last msg"
    assert delta.last_sender == "555@s.whatsapp.net"
    assert bool(delta.last_is_from_me) is True


# --- (b) include_last_message=False - THE REGRESSION TEST -----------------

def test_include_last_message_false_returns_same_chats(rich_wa):
    """Before the fix this raised sqlite3.OperationalError ("no such column:
    messages.content"), which list_chats() swallows and turns into an empty
    list. This must return the same 4 chats as include_last_message=True,
    just without the last-message fields populated."""
    chats = rich_wa.list_chats(include_last_message=False, limit=10)

    assert {c.jid for c in chats} == {jid for jid, _, _ in CHATS}

    by_jid = _by_jid(chats)
    alice = by_jid["222@s.whatsapp.net"]
    assert alice.name == "Alice"
    assert alice.last_message_time is not None
    assert alice.last_message_time.isoformat() == "2024-01-03T08:00:00"

    for chat in chats:
        assert chat.last_message is None
        assert chat.last_sender is None
        assert chat.last_is_from_me is None


# --- (c) query filter, both on name and jid, both include_last_message values

@pytest.mark.parametrize("include_last_message", [True, False])
def test_query_filters_by_name(rich_wa, include_last_message):
    chats = rich_wa.list_chats(query="Alice", include_last_message=include_last_message)
    assert {c.jid for c in chats} == {"222@s.whatsapp.net"}


@pytest.mark.parametrize("include_last_message", [True, False])
def test_query_filters_by_jid(rich_wa, include_last_message):
    chats = rich_wa.list_chats(query="333@s.whatsapp.net", include_last_message=include_last_message)
    assert {c.jid for c in chats} == {"333@s.whatsapp.net"}


# --- (d) pagination and sort_by ---------------------------------------------

def test_sort_by_last_active_orders_desc(rich_wa):
    chats = rich_wa.list_chats(sort_by="last_active", limit=10)
    assert [c.jid for c in chats] == [
        "444@g.us",
        "222@s.whatsapp.net",
        "333@s.whatsapp.net",
        "111@s.whatsapp.net",
    ]


def test_sort_by_name_orders_alphabetically(rich_wa):
    chats = rich_wa.list_chats(sort_by="name", limit=10)
    assert [c.name for c in chats] == ["Alice", "Bob", "Charlie", "Delta Group"]


def test_pagination_limit_and_page(rich_wa):
    page0 = rich_wa.list_chats(sort_by="last_active", limit=2, page=0)
    page1 = rich_wa.list_chats(sort_by="last_active", limit=2, page=1)

    assert [c.jid for c in page0] == ["444@g.us", "222@s.whatsapp.net"]
    assert [c.jid for c in page1] == ["333@s.whatsapp.net", "111@s.whatsapp.net"]


# --- (e) LEFT JOIN semantics: chat with no matching message still returned -

@pytest.mark.parametrize("include_last_message", [True, False])
def test_chat_with_no_messages_still_returned(rich_wa, include_last_message):
    chats = _by_jid(rich_wa.list_chats(include_last_message=include_last_message, limit=10))
    bob = chats["333@s.whatsapp.net"]
    assert bob.name == "Bob"
    assert bob.last_message is None
    assert bob.last_sender is None
    assert bob.last_is_from_me is None
