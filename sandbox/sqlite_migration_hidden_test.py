import pytest

from sqlite_utils import Database


def test_compound_index_progress_copies_every_row_across_batches():
    db = Database(memory=True)
    db.executescript("""
        CREATE TABLE events (group_id TEXT NOT NULL, item_id INTEGER NOT NULL,
                             day INTEGER NOT NULL, value TEXT);
        CREATE UNIQUE INDEX events_key ON events(group_id, item_id, day);
        INSERT INTO events VALUES
          ('a', 1, 1, 'one'), ('a', 1, 2, 'two'), ('a', 2, 1, 'three'),
          ('a', 2, 2, 'four'), ('a', 3, 1, 'five'), ('b', 1, 1, 'six');
    """)
    table = db["events"]
    table._transform_progress_batch_size = 2
    updates = []
    table.transform(rename={"group_id": "group_name"}, progress=lambda n, t: updates.append((n, t)))
    assert db.execute("SELECT group_name, item_id, day, value FROM events ORDER BY group_name, item_id, day").fetchall() == [
        ("a", 1, 1, "one"), ("a", 1, 2, "two"), ("a", 2, 1, "three"),
        ("a", 2, 2, "four"), ("a", 3, 1, "five"), ("b", 1, 1, "six"),
    ]
    assert updates == [(0, 6), (2, 6), (4, 6), (6, 6)]


def test_single_column_key_progress_copies_every_row_across_batches():
    db = Database(memory=True)
    db.executescript("""
        CREATE TABLE items (key TEXT NOT NULL UNIQUE, value TEXT);
        INSERT INTO items VALUES ('a', 'one'), ('b', 'two'), ('c', 'three');
    """)
    table = db["items"]
    table._transform_progress_batch_size = 2
    table.transform(rename={"value": "label"}, progress=lambda _n, _t: None)
    assert db.execute("SELECT key, label FROM items ORDER BY key").fetchall() == [
        ("a", "one"), ("b", "two"), ("c", "three")
    ]


def test_nullable_single_column_primary_key_uses_safe_pagination():
    db = Database(memory=True)
    db.executescript("""
        CREATE TABLE items (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO items VALUES
          (NULL, 'one'), (NULL, 'two'), ('a', 'three'), ('b', 'four');
    """)
    table = db["items"]
    table._transform_progress_batch_size = 2
    table.transform(rename={"value": "label"}, progress=lambda _n, _t: None)
    assert db.execute(
        "SELECT _rowid_, * FROM items ORDER BY _rowid_"
    ).fetchall() == [
        (1, None, "one"), (2, None, "two"), (3, "a", "three"), (4, "b", "four")
    ]


def test_nullable_compound_primary_key_uses_safe_pagination():
    db = Database(memory=True)
    db.executescript("""
        CREATE TABLE items (
            left_key TEXT, right_key TEXT, value TEXT,
            PRIMARY KEY (left_key, right_key)
        );
        INSERT INTO items VALUES
          (NULL, 'x', 'one'), (NULL, 'y', 'two'),
          ('a', 'x', 'three'), ('b', 'y', 'four');
    """)
    table = db["items"]
    table._transform_progress_batch_size = 2
    table.transform(rename={"value": "label"}, progress=lambda _n, _t: None)
    assert db.execute(
        "SELECT _rowid_, * FROM items ORDER BY _rowid_"
    ).fetchall() == [
        (1, None, "x", "one"), (2, None, "y", "two"),
        (3, "a", "x", "three"), (4, "b", "y", "four"),
    ]


def test_integer_primary_key_progress_preserves_rowid_alias_values():
    db = Database(memory=True)
    db.executescript("""
        CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT);
        INSERT INTO items VALUES (7, 'one'), (31, 'two'), (90, 'three');
    """)
    table = db["items"]
    table._transform_progress_batch_size = 1
    table.transform(rename={"value": "label"}, progress=lambda _n, _t: None)
    assert db.execute("SELECT id, label FROM items ORDER BY id").fetchall() == [
        (7, "one"), (31, "two"), (90, "three")
    ]


def test_without_rowid_compound_primary_key_progress_preserves_rows():
    db = Database(memory=True)
    db.executescript("""
        CREATE TABLE items (
            left_key TEXT NOT NULL, right_key INTEGER NOT NULL, value TEXT,
            PRIMARY KEY (left_key, right_key)
        ) WITHOUT ROWID;
        INSERT INTO items VALUES
          ('a', 1, 'one'), ('a', 2, 'two'), ('a', 3, 'three'),
          ('b', 1, 'four'), ('b', 2, 'five');
    """)
    table = db["items"]
    table._transform_progress_batch_size = 2
    table.transform(rename={"value": "label"}, progress=lambda _n, _t: None)
    assert db.execute(
        "SELECT left_key, right_key, label FROM items ORDER BY left_key, right_key"
    ).fetchall() == [
        ("a", 1, "one"), ("a", 2, "two"), ("a", 3, "three"),
        ("b", 1, "four"), ("b", 2, "five"),
    ]


def test_empty_table_reports_zero_and_transforms_schema():
    db = Database(memory=True)
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
    updates = []
    db["items"].transform(rename={"value": "label"}, progress=lambda n, t: updates.append((n, t)))
    assert updates == [(0, 0)]
    assert [column.name for column in db["items"].columns] == ["id", "label"]


def test_callback_failure_rolls_back_replacement():
    db = Database(memory=True)
    db.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT); INSERT INTO items VALUES (1, 'kept');")
    db["items"]._transform_progress_batch_size = 1
    def fail(copied, _total):
        if copied:
            raise RuntimeError("stop")
    with pytest.raises(RuntimeError, match="stop"):
        db["items"].transform(rename={"value": "label"}, progress=fail)
    assert db.execute("SELECT * FROM items").fetchall() == [(1, "kept")]
    assert "value" in [column.name for column in db["items"].columns]


def test_single_column_unique_index_and_sparse_rowids_survive_batches():
    db = Database(memory=True)
    db.executescript("""
        CREATE TABLE items (key TEXT NOT NULL UNIQUE, value TEXT);
        INSERT INTO items (_rowid_, key, value) VALUES (4, 'a', 'one');
        INSERT INTO items (_rowid_, key, value) VALUES (90, 'b', 'two');
        INSERT INTO items (_rowid_, key, value) VALUES (300, 'c', 'three');
    """)
    table = db["items"]
    table._transform_progress_batch_size = 2
    table.transform(rename={"value": "label"}, progress=lambda _n, _t: None)
    assert db.execute(
        "SELECT _rowid_, key, label FROM items ORDER BY _rowid_"
    ).fetchall() == [(4, "a", "one"), (90, "b", "two"), (300, "c", "three")]


def test_declared_rowid_column_does_not_replace_the_hidden_rowid():
    db = Database(memory=True)
    db.executescript("""
        CREATE TABLE items (rowid TEXT, value TEXT);
        INSERT INTO items (_rowid_, rowid, value) VALUES (8, 'user-a', 'first');
        INSERT INTO items (_rowid_, rowid, value) VALUES (31, 'user-b', 'second');
    """)
    table = db["items"]
    table._transform_progress_batch_size = 1
    table.transform(rename={"value": "label"}, progress=lambda _n, _t: None)
    assert db.execute(
        "SELECT _rowid_, rowid, label FROM items ORDER BY _rowid_"
    ).fetchall() == [(8, "user-a", "first"), (31, "user-b", "second")]
