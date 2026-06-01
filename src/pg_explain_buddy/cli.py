"""
CLI entry point for pg-explain-buddy.
Usage:
  pg-explain-buddy < query.explain
  psql -c "EXPLAIN ANALYZE SELECT ..." | pg-explain-buddy
  pg-explain-buddy --file plan.txt
  pg-explain-buddy --dsn postgresql://user:pass@host/db --query "SELECT ..."
"""
from __future__ import annotations

import argparse
import sys

from .advisor import Severity, analyze
from .parser import parse, walk

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


console = Console() if HAS_RICH else None

SEVERITY_STYLE = {
    Severity.CRITICAL: ("bold red", "🔴"),
    Severity.WARNING:  ("yellow",   "🟡"),
    Severity.INFO:     ("cyan",     "🔵"),
}


def run_from_text(text: str, json_out: bool = False) -> int:
    root = parse(text)
    if root is None:
        print("ERROR: Could not parse EXPLAIN ANALYZE output.", file=sys.stderr)
        print("Make sure you're using EXPLAIN (ANALYZE, BUFFERS) format.", file=sys.stderr)
        return 1

    advice_list = analyze(root)

    if json_out:
        import json
        out = [
            {
                "severity": a.severity.value,
                "title": a.title,
                "node_type": a.node_type,
                "relation": a.relation,
                "detail": a.detail,
                "suggestion": a.suggestion,
            }
            for a in advice_list
        ]
        print(json.dumps(out, indent=2))
        return 0

    if HAS_RICH:
        _rich_output(root, advice_list)
    else:
        _plain_output(root, advice_list)

    return 1 if any(a.severity == Severity.CRITICAL for a in advice_list) else 0


# ── Rich output ───────────────────────────────────────────────────────────

def _rich_output(root, advice_list):
    console.print()

    # Plan tree
    console.print("[bold]Execution plan[/bold]", style="dim")
    tree = _build_rich_tree(root, None)
    console.print(tree)
    console.print()

    # Stats table
    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold dim")
    table.add_column("Node", style="bold", min_width=28)
    table.add_column("Relation", style="dim")
    table.add_column("Est. rows", justify="right")
    table.add_column("Act. rows", justify="right")
    table.add_column("Time (ms)", justify="right")
    table.add_column("Loops", justify="right")

    for node in walk(root):
        est = f"{node.plan_rows:,}"
        act = f"{node.actual_rows:,}"
        ratio = node.row_estimate_error
        if ratio > 10 or ratio < 0.1:
            act = f"[yellow]{act}[/yellow]"
        time_str = f"{node.total_time_ms:.1f}"
        if node.total_time_ms > 500:
            time_str = f"[red]{time_str}[/red]"
        table.add_row(
            node.node_type,
            node.relation or "",
            est,
            act,
            time_str,
            str(node.actual_loops),
        )

    console.print("[bold]Node statistics[/bold]", style="dim")
    console.print(table)

    # Advice
    if not advice_list:
        console.print(Panel("✅ No issues found — the plan looks good!", style="green"))
        return

    console.print(f"[bold]Advice ({len(advice_list)} suggestion{'s' if len(advice_list) != 1 else ''})[/bold]", style="dim")
    for advice in advice_list:
        style, icon = SEVERITY_STYLE[advice.severity]
        loc = f" [dim]on {advice.relation}[/dim]" if advice.relation else ""
        console.print(f"\n  {icon}  [bold {style}]{advice.title}[/bold {style}]{loc}")
        console.print(f"     {advice.detail}", style="dim")
        console.print(f"     [green]→ {advice.suggestion}[/green]")

    console.print()


def _build_rich_tree(node, parent_tree):
    label = Text()
    label.append(node.node_type, style="bold")
    if node.relation:
        label.append(f" on {node.relation}", style="dim")
    if node.actual_total_ms > 0:
        ms = node.actual_total_ms
        style = "red" if ms > 500 else "yellow" if ms > 100 else "green"
        label.append(f"  [{ms:.1f} ms]", style=style)

    t = Tree(label) if parent_tree is None else parent_tree.add(label)
    for child in node.children:
        _build_rich_tree(child, t)
    return t


# ── Plain output ──────────────────────────────────────────────────────────

def _plain_output(root, advice_list):
    print("\n=== PLAN TREE ===")
    for node in walk(root):
        indent = "  " * (node.indent // 2)
        print(f"{indent}{node.node_type}"
              + (f" on {node.relation}" if node.relation else "")
              + f"  [{node.actual_total_ms:.1f} ms, {node.actual_rows} rows]")

    print("\n=== ADVICE ===")
    if not advice_list:
        print("No issues found.")
        return
    for advice in advice_list:
        print(f"\n{advice}")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="pg-explain-buddy",
        description="Parse EXPLAIN ANALYZE output and get optimization hints.",
    )
    parser.add_argument("--file", "-f", help="Path to file with EXPLAIN ANALYZE output")
    parser.add_argument("--dsn", help="PostgreSQL DSN (runs the query directly)")
    parser.add_argument("--query", "-q", help="SQL query to EXPLAIN ANALYZE (requires --dsn)")
    parser.add_argument("--json", action="store_true", help="Output advice as JSON")
    args = parser.parse_args()

    if args.dsn and args.query:
        text = _fetch_from_db(args.dsn, args.query)
    elif args.file:
        with open(args.file) as f:
            text = f.read()
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.print_help()
        sys.exit(1)

    sys.exit(run_from_text(text, json_out=args.json))


def _fetch_from_db(dsn: str, query: str) -> str:
    try:
        import psycopg2
    except ImportError:
        print("psycopg2 is required for --dsn mode: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {query}"
    cur.execute(explain_sql)
    rows = cur.fetchall()
    conn.close()
    return "\n".join(r[0] for r in rows)


if __name__ == "__main__":
    main()
