"""Tests for pg-explain-buddy parser and advisor."""
from pg_explain_buddy import Severity, analyze, parse

# ── Sample EXPLAIN ANALYZE outputs ────────────────────────────────────────

SEQ_SCAN_WITH_FILTER = """\
Seq Scan on orders  (cost=0.00..18450.00 rows=1 width=48) (actual time=0.054..185.432 rows=3 loops=1)
  Filter: ((status = 'pending') AND (amount > 1000))
  Rows Removed by Filter: 249997
Planning Time: 0.1 ms
Execution Time: 185.5 ms
"""

NESTED_LOOP_PLAN = """\
Nested Loop  (cost=0.00..12.50 rows=10 width=32) (actual time=0.020..45.123 rows=1500 loops=1)
  ->  Seq Scan on users  (cost=0.00..5.00 rows=100 width=16) (actual time=0.010..0.200 rows=100 loops=1)
  ->  Index Scan using orders_user_id_idx on orders  (cost=0.00..0.07 rows=1 width=16) (actual time=0.004..0.040 rows=15 loops=100)
        Index Cond: (user_id = users.id)
Planning Time: 0.3 ms
Execution Time: 45.5 ms
"""

HASH_JOIN_PLAN = """\
Hash Join  (cost=5000.00..25000.00 rows=100000 width=64) (actual time=200.0..1500.0 rows=95000 loops=1)
  Hash Cond: (orders.customer_id = customers.id)
  ->  Seq Scan on orders  (cost=0.00..15000.00 rows=500000 width=32) (actual time=0.010..400.0 rows=500000 loops=1)
  ->  Hash  (cost=2500.00..2500.00 rows=200000 width=32) (actual time=100.0..100.0 rows=200000 loops=1)
        ->  Seq Scan on customers  (cost=0.00..2500.00 rows=200000 width=32) (actual time=0.010..80.0 rows=200000 loops=1)
Planning Time: 1.2 ms
Execution Time: 1520.0 ms
"""

GOOD_PLAN = """\
Index Scan using users_email_idx on users  (cost=0.42..8.44 rows=1 width=128) (actual time=0.030..0.032 rows=1 loops=1)
  Index Cond: ((email)::text = 'alice@example.com')
Planning Time: 0.2 ms
Execution Time: 0.1 ms
"""


# ── Parser tests ──────────────────────────────────────────────────────────

def test_parse_seq_scan():
    root = parse(SEQ_SCAN_WITH_FILTER)
    assert root is not None
    assert root.node_type == "Seq Scan"
    assert root.relation == "orders"
    assert root.rows_removed_by_filter == 249997
    assert root.actual_rows == 3


def test_parse_nested_loop_tree():
    root = parse(NESTED_LOOP_PLAN)
    assert root is not None
    assert root.node_type == "Nested Loop"
    assert len(root.children) == 2
    assert root.children[0].node_type == "Seq Scan"
    assert root.children[1].node_type == "Index Scan"


def test_parse_hash_join_tree():
    root = parse(HASH_JOIN_PLAN)
    assert root is not None
    assert root.node_type == "Hash Join"
    assert root.hash_cond is not None
    assert len(root.children) == 2


def test_parse_good_plan():
    root = parse(GOOD_PLAN)
    assert root is not None
    assert root.is_index_scan


def test_parse_returns_none_on_garbage():
    assert parse("this is not an explain output") is None


# ── Advisor tests ─────────────────────────────────────────────────────────

def test_seq_scan_advice_triggered():
    root = parse(SEQ_SCAN_WITH_FILTER)
    advice = analyze(root)
    titles = [a.title for a in advice]
    assert any("Sequential scan" in t for t in titles)


def test_good_plan_no_warnings():
    root = parse(GOOD_PLAN)
    advice = analyze(root)
    warnings = [a for a in advice if a.severity in (Severity.WARNING, Severity.CRITICAL)]
    assert len(warnings) == 0


def test_slow_query_info():
    root = parse(HASH_JOIN_PLAN)
    advice = analyze(root)
    assert any("Slow query" in a.title for a in advice)


def test_advice_sorted_by_severity():
    root = parse(SEQ_SCAN_WITH_FILTER)
    advice = analyze(root)
    order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    severities = [order[a.severity] for a in advice]
    assert severities == sorted(severities)


def test_row_estimate_error():
    root = parse(NESTED_LOOP_PLAN)
    assert root is not None
    # Nested loop: planned 10 rows, got 1500 — 150x error
    assert root.row_estimate_error > 100