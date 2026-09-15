"""Integration tests for the SQL the n8n nodes actually run.

The orchestration layer's two hardest claims live in SQL, not in Python:
idempotency acquisition is race-safe (architecture.md §4.1), and a dead letter
can never exist beside a key still reading 'in_progress' (§4.4). Neither is
exercised by the rest of the suite, because neither runs inside the API.

The statements are **read out of `n8n/lead-qualifier-main.json`** rather than
copied here, so this file cannot drift from what the deployed nodes execute.
If someone edits the query in the n8n canvas and re-exports, these tests run
against the edit.

Skips cleanly when no Postgres is reachable, like the other integration test.
Point LQ_DATABASE_URL at any throwaway database (a free Neon branch is enough)
and run `pytest tests/test_idempotency_sql.py -v`.

Use a DIRECT connection string, not a pooled one: the concurrency test holds two
transactions open at once, which transaction-mode pooling does not preserve.
"""

from __future__ import annotations

import json
import pathlib
import re
import threading
import time
import uuid

import pytest

from lead_qualifier.config import settings

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / "n8n" / "lead-qualifier-main.json"
MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "001_init.sql"


def _node_queries() -> dict[str, str]:
    """Pull every Postgres node's query out of the workflow, keyed by node name."""
    data = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    return {
        n["name"]: n["parameters"]["query"]
        for n in data["nodes"]
        if n["type"] == "n8n-nodes-base.postgres"
    }


SQL = _node_queries()

ACQUIRE = SQL["Acquire Idempotency Key"]
READ_EXISTING = SQL["Read Existing Key"]
MARK_COMPLETED = SQL["Mark Completed"]
DEAD_LETTER = SQL["Dead Letter & Mark Failed"]


def _named(sql: str) -> str:
    """n8n's Postgres node uses native $1/$2 placeholders; psycopg wants its own.

    Named conversion rather than positional, because the dead-letter statement
    deliberately references $2 twice (insert the key, then update by it).
    """
    return re.sub(r"\$(\d+)", r"%(p\1)s", sql)


def _params(*values) -> dict:
    return {f"p{i + 1}": v for i, v in enumerate(values)}


def _reachable(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=5):
            return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def db_url() -> str:
    url = settings.database_url
    if not _reachable(url):
        pytest.skip(
            "no Postgres reachable at LQ_DATABASE_URL "
            "(set it to a throwaway database to exercise the orchestration SQL)"
        )
    # autocommit: the migration carries its own BEGIN/COMMIT.
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(MIGRATION.read_text(encoding="utf-8"))
    return url


@pytest.fixture
def key() -> str:
    """A unique key per test, so runs never collide with each other."""
    return f"test-{uuid.uuid4().hex[:12]}.com:2026-09-15"


@pytest.fixture
def conn(db_url):
    with psycopg.connect(db_url) as c:
        yield c


def _acquire(cur, key: str, domain: str = "acme.com") -> list[tuple]:
    cur.execute(_named(ACQUIRE), _params(key, domain))
    return cur.fetchall()


# --- §4.1 acquisition ---------------------------------------------------------


def test_fresh_key_is_acquired_new(conn, key):
    with conn.cursor() as cur:
        rows = _acquire(cur, key)
    conn.commit()

    assert len(rows) == 1, "a fresh key must return a row — this caller owns the work"
    assert rows[0][1] == "acquired_new", "xmax = 0 must identify the insert, not an update"


def test_completed_key_is_not_reacquired_and_returns_its_result(conn, key):
    stored = {"domain": "acme.com", "tier": "hot", "score": {"score": 88}}
    with conn.cursor() as cur:
        _acquire(cur, key)
        cur.execute(_named(MARK_COMPLETED), _params(key, json.dumps(stored)))
        assert cur.fetchone()[1] == "completed"
        conn.commit()

        # The replay: same key, second submission.
        rows = _acquire(cur, key)
        assert rows == [], "a completed key must NOT be re-acquired — no duplicate LLM spend"

        cur.execute(_named(READ_EXISTING), _params(key))
        _, _, status, result = cur.fetchone()
    conn.commit()

    assert status == "completed"
    assert result == stored, "the cached branch must be able to return the stored result verbatim"


def test_failed_key_is_retryable(conn, key):
    with conn.cursor() as cur:
        _acquire(cur, key)
        cur.execute(
            _named(DEAD_LETTER),
            _params(key.split(":")[0], key, "llm", "provider exploded", 3, json.dumps({"x": 1})),
        )
        cur.fetchone()
        conn.commit()

        rows = _acquire(cur, key)
    conn.commit()

    assert len(rows) == 1, "a failed key must be retryable by a later submission"
    assert rows[0][1] == "acquired_retry", "and must be distinguishable from a fresh insert"


def test_concurrent_acquisition_lets_exactly_one_caller_win(db_url, key):
    """The race §4.1 exists to close.

    Two callers submit the same key simultaneously. Check-then-insert would let
    both through. The atomic upsert must let exactly one win, and must make the
    loser *block* on the row lock until the winner commits rather than racing
    past it.
    """
    HOLD = 1.5
    start = threading.Barrier(2)
    results: dict[str, list] = {}
    elapsed: dict[str, float] = {}

    def caller(name: str, hold: bool) -> None:
        with psycopg.connect(db_url) as c:
            with c.cursor() as cur:
                start.wait(timeout=10)
                t0 = time.monotonic()
                results[name] = _acquire(cur, key, "race.com")
                elapsed[name] = time.monotonic() - t0
                if hold:
                    time.sleep(HOLD)  # keep the transaction open
            c.commit()

    threads = [
        threading.Thread(target=caller, args=("a", True)),
        threading.Thread(target=caller, args=("b", True)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    winners = [n for n, rows in results.items() if rows]
    losers = [n for n, rows in results.items() if not rows]

    assert len(winners) == 1, f"exactly one caller may own the key, got {results}"
    assert len(losers) == 1
    assert results[winners[0]][0][1] == "acquired_new"

    # The loser must have waited for the winner's transaction, not sailed past it.
    assert elapsed[losers[0]] >= HOLD * 0.8, (
        f"the loser returned in {elapsed[losers[0]]:.2f}s without blocking on the row "
        f"lock — the statement is not actually serialising concurrent callers"
    )

    with psycopg.connect(db_url) as c:
        row = c.execute(_named(READ_EXISTING), _params(key)).fetchone()
    assert row[2] == "in_progress", "the loser's follow-up read must see the winner's state"


# --- §4.4 dead-lettering ------------------------------------------------------


def test_dead_letter_and_demotion_happen_in_one_statement(conn, key):
    domain = key.split(":")[0]
    snapshot = {"request": {"domain": domain}, "attempts": 3}

    with conn.cursor() as cur:
        _acquire(cur, key, domain)
        cur.execute(
            _named(DEAD_LETTER),
            _params(domain, key, "schema", "validation failed 3x", 3, json.dumps(snapshot)),
        )
        returned_key, status, dead_letter_id = cur.fetchone()
        conn.commit()

        assert status == "failed", "the key must be demoted by the same statement"
        assert returned_key == key
        assert dead_letter_id is not None, "the caller needs the id to grep the failure"

        cur.execute(
            "SELECT domain, failure_stage, attempt_count, payload_snapshot "
            "FROM dead_letters WHERE id = %s",
            (dead_letter_id,),
        )
        dl_domain, stage, attempts, payload = cur.fetchone()
    conn.commit()

    assert (dl_domain, stage, attempts) == (domain, "schema", 3)
    assert payload == snapshot


def test_every_failure_stage_the_workflow_emits_is_accepted_by_the_constraint(conn, key):
    """The Classify Failure node maps onto a CHECK constraint in the migration.

    If the two ever disagree, dead-lettering fails at the exact moment it is
    most needed, so the allowed set is pinned from both sides.
    """
    stages = ["enrichment", "llm", "schema", "network", "cost_ceiling", "unknown"]
    domain = key.split(":")[0]

    for stage in stages:
        k = f"{key}-{stage}"
        with conn.cursor() as cur:
            _acquire(cur, k, domain)
            cur.execute(
                _named(DEAD_LETTER),
                _params(domain, k, stage, "x", 1, json.dumps({})),
            )
            assert cur.fetchone()[1] == "failed"
        conn.commit()


def test_classifier_stage_list_matches_the_migration_constraint():
    """Pure static check — runs without a database."""
    workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    classify = next(
        n for n in workflow["nodes"] if n["name"] == "Classify Failure"
    )["parameters"]["jsCode"]
    in_code = set(re.findall(r"'(\w+)'", re.search(r"ALLOWED = \[(.*?)\]", classify, re.S).group(1)))

    constraint = re.search(
        r"failure_stage\s+TEXT NOT NULL\s*CHECK \(failure_stage IN\s*\((.*?)\)\)",
        MIGRATION.read_text(encoding="utf-8"),
        re.S,
    ).group(1)
    in_sql = set(re.findall(r"'(\w+)'", constraint))

    assert in_code == in_sql, (
        f"the n8n classifier and the Postgres CHECK disagree: "
        f"only in code {in_code - in_sql}, only in SQL {in_sql - in_code}"
    )
