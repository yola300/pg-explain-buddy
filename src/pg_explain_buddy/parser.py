"""
EXPLAIN ANALYZE output parser for PostgreSQL.
Converts raw text output into a structured tree of nodes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class PlanNode:
    node_type: str
    relation: str | None = None
    alias: str | None = None
    index_name: str | None = None
    join_type: str | None = None

    # Cost / row estimates
    startup_cost: float = 0.0
    total_cost: float = 0.0
    plan_rows: int = 0
    plan_width: int = 0

    # Actual execution
    actual_startup_ms: float = 0.0
    actual_total_ms: float = 0.0
    actual_rows: int = 0
    actual_loops: int = 1

    # Extra attributes
    filter: str | None = None
    index_cond: str | None = None
    hash_cond: str | None = None
    merge_cond: str | None = None
    sort_key: str | None = None
    rows_removed_by_filter: int = 0
    shared_hit_blocks: int = 0
    shared_read_blocks: int = 0

    children: list[PlanNode] = field(default_factory=list)
    indent: int = 0

    @property
    def row_estimate_error(self) -> float:
        """Ratio of actual vs planned rows (>10x is suspicious)."""
        if self.plan_rows == 0:
            return 0.0
        return self.actual_rows / self.plan_rows

    @property
    def total_time_ms(self) -> float:
        return self.actual_total_ms * self.actual_loops

    @property
    def is_seq_scan(self) -> bool:
        return self.node_type == "Seq Scan"

    @property
    def is_index_scan(self) -> bool:
        return "Index" in self.node_type

    @property
    def filter_selectivity(self) -> float | None:
        total = self.actual_rows + self.rows_removed_by_filter
        if total == 0:
            return None
        return self.rows_removed_by_filter / total


# ── Regex patterns ────────────────────────────────────────────────────────────

_NODE_LINE = re.compile(
    r"^(\s*)"
    r"(?:->)?\s*"
    r"(.+?)"
    r"\s+\(cost=([\d.]+)\.\.([\d.]+)"
    r"\s+rows=(\d+)\s+width=(\d+)\)"
    r"(?:\s+\(actual\s+time=([\d.]+)\.\.([\d.]+)"
    r"\s+rows=(\d+)\s+loops=(\d+)\))?"
)

_FILTER = re.compile(r"Filter:\s+(.+)")
_INDEX_COND = re.compile(r"Index Cond:\s+(.+)")
_HASH_COND = re.compile(r"Hash Cond:\s+(.+)")
_MERGE_COND = re.compile(r"Merge Cond:\s+(.+)")
_SORT_KEY = re.compile(r"Sort Key:\s+(.+)")
_ROWS_REMOVED = re.compile(r"Rows Removed by Filter:\s+(\d+)")
_BUFFERS = re.compile(r"Buffers:\s+shared hit=(\d+)(?:\s+read=(\d+))?")


def parse(explain_output: str) -> PlanNode | None:
    """
    Parse EXPLAIN ANALYZE text output and return the root PlanNode.
    Supports both EXPLAIN ANALYZE and EXPLAIN (ANALYZE, BUFFERS) formats.
    """
    lines = explain_output.strip().splitlines()
    nodes: list[tuple[int, PlanNode]] = []  # (indent, node)

    current_node: PlanNode | None = None

    for line in lines:
        # Skip footer lines (Planning/Execution time, JIT, etc.)
        stripped = line.strip()
        if not stripped or stripped.startswith("Planning") or stripped.startswith("Execution") or stripped.startswith("JIT"):
            continue

        m = _NODE_LINE.match(line)
        if m:
            indent = len(m.group(1))
            node_type, relation, alias, index_name = _parse_node_header(m.group(2))

            node = PlanNode(
        node_type=node_type,
        relation=relation,
        alias=alias,
        index_name=index_name,
        startup_cost=float(m.group(3)),
        total_cost=float(m.group(4)),
        plan_rows=int(m.group(5)),
        plan_width=int(m.group(6)),
        actual_startup_ms=float(m.group(7)) if m.group(7) else 0.0,
        actual_total_ms=float(m.group(8)) if m.group(8) else 0.0,
        actual_rows=int(m.group(9)) if m.group(9) else 0,
        actual_loops=int(m.group(10)) if m.group(10) else 1,
        indent=indent,
    )
            nodes.append((indent, node))
            current_node = node
            continue

        if current_node is None:
            continue

        if m2 := _ROWS_REMOVED.search(stripped):
            current_node.rows_removed_by_filter = int(m2.group(1))
        elif m2 := _FILTER.search(stripped):
            current_node.filter = m2.group(1)
        elif m2 := _INDEX_COND.search(stripped):
            current_node.index_cond = m2.group(1)
        elif m2 := _HASH_COND.search(stripped):
            current_node.hash_cond = m2.group(1)
        elif m2 := _MERGE_COND.search(stripped):
            current_node.merge_cond = m2.group(1)
        elif m2 := _SORT_KEY.search(stripped):
            current_node.sort_key = m2.group(1)
        elif m2 := _BUFFERS.search(stripped):
            current_node.shared_hit_blocks = int(m2.group(1))
            current_node.shared_read_blocks = int(m2.group(2)) if m2.group(2) else 0

    if not nodes:
        return None

    _build_tree(nodes)
    return nodes[0][1]


def _build_tree(nodes: list[tuple[int, PlanNode]]) -> None:
    """Reconstruct parent-child relationships from indentation."""
    for i, (indent, node) in enumerate(nodes[1:], 1):
        # Find the nearest ancestor with smaller indent
        for j in range(i - 1, -1, -1):
            parent_indent, parent = nodes[j]
            if parent_indent < indent:
                parent.children.append(node)
                break


def walk(node: PlanNode):
    """Depth-first walk over all nodes."""
    yield node
    for child in node.children:
        yield from walk(child)

def _parse_node_header(header: str) -> tuple[str, str | None, str | None, str | None]:
    """
    Split PostgreSQL plan header into:
    node_type, relation, alias, index_name.

    Examples:
    - Seq Scan on orders
    - Index Scan using orders_user_id_idx on orders
    - Hash Join
    """
    header = header.strip()

    index_scan = re.match(
        r"^(Index Scan|Index Only Scan|Bitmap Index Scan)\s+using\s+([\w.\"]+)\s+on\s+([\w.\"]+)(?:\s+([\w\"]+))?$",
        header,
    )
    if index_scan:
        return (
            index_scan.group(1),
            index_scan.group(3),
            index_scan.group(4),
            index_scan.group(2),
        )

    relation_scan = re.match(
        r"^(.+?)\s+on\s+([\w.\"]+)(?:\s+([\w\"]+))?$",
        header,
    )
    if relation_scan:
        return (
            relation_scan.group(1).strip(),
            relation_scan.group(2),
            relation_scan.group(3),
            None,
        )

    return header, None, None, None
