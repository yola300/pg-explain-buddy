"""
Rule-based advice engine.
Each rule is a function that receives a PlanNode and the root node,
and returns an Advice dataclass or None.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .parser import PlanNode, walk


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Advice:
    severity: Severity
    node_type: str
    relation: str | None
    title: str
    detail: str
    suggestion: str

    def __str__(self) -> str:
        loc = f" on {self.relation}" if self.relation else ""
        return (
            f"[{self.severity.upper()}] {self.title}{loc}\n"
            f"  {self.detail}\n"
            f"  → {self.suggestion}"
        )


RuleFn = Callable[[PlanNode, PlanNode], Advice | None]
_rules: list[RuleFn] = []


def rule(fn: RuleFn) -> RuleFn:
    _rules.append(fn)
    return fn


# ── Rules ──────────────────────────────────────────────────────────────────

@rule
def seq_scan_large_table(node: PlanNode, root: PlanNode) -> Advice | None:
    """Seq Scan on a table that returns few rows relative to what it scans."""
    if not node.is_seq_scan:
        return None
    # Heuristic: scanned many, returned few, and it's not a tiny table
    total_scanned = node.actual_rows + node.rows_removed_by_filter
    if total_scanned < 1000:
        return None
    if node.filter is None:
        return None
    sel = node.filter_selectivity or 0
    if sel >= 0.9:  # removed at least 90% of rows
        return Advice(
            severity=Severity.WARNING,
            node_type=node.node_type,
            relation=node.relation,
            title="Sequential scan with high row removal",
            detail=(
                f"Scanned ~{total_scanned:,} rows but removed {node.rows_removed_by_filter:,} "
                f"({sel * 100:.1f}% filtered) via: {node.filter}"
            ),
            suggestion=(
                "Consider an index on the filtered column(s). "
                f"Run: CREATE INDEX ON {node.relation} (<filtered_columns>);"
            ),
        )
    return None


@rule
def seq_scan_no_filter(node: PlanNode, root: PlanNode) -> Advice | None:
    """Full table scan with no filter — probably fine, but flag for large tables."""
    if not node.is_seq_scan:
        return None
    if node.filter is not None:
        return None
    if node.actual_rows < 10_000:
        return None
    if node.total_cost < 500:
        return None
    return Advice(
        severity=Severity.INFO,
        node_type=node.node_type,
        relation=node.relation,
        title="Full table scan (no filter)",
        detail=f"Reading all {node.actual_rows:,} rows from {node.relation}.",
        suggestion=(
            "If this is part of a JOIN or aggregation, this may be expected. "
            "Otherwise verify that WHERE conditions are being applied."
        ),
    )


@rule
def bad_row_estimate(node: PlanNode, root: PlanNode) -> Advice | None:
    """Planner row estimate is wildly off (>25x error)."""
    if node.plan_rows == 0 or node.actual_rows == 0:
        return None
    ratio = node.row_estimate_error
    if ratio < 25 and ratio > 0.04:  # within 25x in either direction
        return None
    if node.actual_rows < 100:  # small tables, not critical
        return None

    direction = "underestimated" if ratio > 1 else "overestimated"
    factor = max(ratio, 1 / ratio) if ratio > 0 else 0
    return Advice(
        severity=Severity.WARNING,
        node_type=node.node_type,
        relation=node.relation,
        title=f"Poor row estimate ({direction} by {factor:.0f}×)",
        detail=(
            f"Planner expected {node.plan_rows:,} rows, got {node.actual_rows:,}. "
            f"This can cause suboptimal join strategies."
        ),
        suggestion=(
            "Run ANALYZE on the table to refresh statistics. "
            "Consider increasing default_statistics_target for skewed columns."
        ),
    )


@rule
def hash_join_large_batches(node: PlanNode, root: PlanNode) -> Advice | None:
    """Hash Join where the build side is very large."""
    if node.node_type != "Hash Join":
        return None
    if node.total_cost < 1000:
        return None
    # Check if the right child (build side) is expensive
    if not node.children:
        return None
    build_side = node.children[-1]
    if build_side.actual_rows > 100_000:
        return Advice(
            severity=Severity.INFO,
            node_type=node.node_type,
            relation=node.relation,
            title="Large hash join build side",
            detail=(
                f"Building hash table from {build_side.actual_rows:,} rows "
                f"({build_side.node_type} on {build_side.relation})."
            ),
            suggestion=(
                "If memory pressure is an issue, increase work_mem. "
                "Check pg_stat_statements for hash_batches > 1."
            ),
        )
    return None


@rule
def nested_loop_large(node: PlanNode, root: PlanNode) -> Advice | None:
    """Nested Loop with many iterations — O(n²) risk."""
    if node.node_type != "Nested Loop":
        return None
    if node.actual_loops == 1 and node.actual_rows < 10_000:
        return None
    # Outer rows × inner loops
    if node.children:
        outer = node.children[0]
        inner_loops = node.children[1].actual_loops if len(node.children) > 1 else node.actual_loops
        if outer.actual_rows > 1000 and inner_loops > 500:
            return Advice(
                severity=Severity.WARNING,
                node_type=node.node_type,
                relation=node.relation,
                title="Nested loop with many iterations",
                detail=(
                    f"Inner side executed {inner_loops:,} times "
                    f"(outer rows: {outer.actual_rows:,})."
                ),
                suggestion=(
                    "Consider adding an index on the inner table's join key. "
                    "Or set enable_nestloop=off temporarily to test Hash/Merge Join."
                ),
            )
    return None


@rule
def slow_sort(node: PlanNode, root: PlanNode) -> Advice | None:
    """Sort node that takes significant time."""
    if node.node_type not in ("Sort", "Incremental Sort"):
        return None
    if node.total_time_ms < 100:
        return None
    return Advice(
        severity=Severity.INFO,
        node_type=node.node_type,
        relation=node.relation,
        title=f"Expensive sort ({node.total_time_ms:.0f} ms)",
        detail=f"Sorting by: {node.sort_key or 'unknown'}",
        suggestion=(
            "An index on the sort key can eliminate the sort entirely. "
            "Also check if work_mem is sufficient to avoid disk spills."
        ),
    )


@rule
def high_disk_reads(node: PlanNode, root: PlanNode) -> Advice | None:
    """Node with significant disk block reads (cache miss)."""
    if node.shared_read_blocks < 100:
        return None
    total = node.shared_hit_blocks + node.shared_read_blocks
    if total == 0:
        return None
    hit_ratio = node.shared_hit_blocks / total
    if hit_ratio > 0.95:
        return None
    return Advice(
        severity=Severity.WARNING,
        node_type=node.node_type,
        relation=node.relation,
        title=f"Low buffer cache hit ratio ({hit_ratio * 100:.1f}%)",
        detail=(
            f"Read {node.shared_read_blocks:,} blocks from disk, "
            f"only {node.shared_hit_blocks:,} from cache."
        ),
        suggestion=(
            "Increase shared_buffers or ensure the table fits in OS page cache. "
            "For large tables, consider table partitioning."
        ),
    )


@rule
def slow_total_query(node: PlanNode, root: PlanNode) -> Advice | None:
    """Flag if the root node total time is very high — just informational."""
    if node is not root:
        return None
    if node.actual_total_ms < 1000:
        return None
    return Advice(
        severity=Severity.INFO,
        node_type="Query",
        relation=None,
        title=f"Slow query ({node.actual_total_ms:.0f} ms total)",
        detail="Total execution time exceeds 1 second.",
        suggestion=(
            "Review the most expensive nodes above. "
            "Enable pg_stat_statements to track this query over time."
        ),
    )


# ── Public API ─────────────────────────────────────────────────────────────

def analyze(root: PlanNode) -> list[Advice]:
    """Run all rules against every node and return sorted advice."""
    results: list[Advice] = []
    for node in walk(root):
        for rule_fn in _rules:
            advice = rule_fn(node, root)
            if advice:
                results.append(advice)

    # Sort: CRITICAL first, then WARNING, then INFO
    order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    results.sort(key=lambda a: order[a.severity])
    return results