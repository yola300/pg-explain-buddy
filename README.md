# pg-explain-buddy

`pg-explain-buddy` is a Python CLI tool for parsing PostgreSQL `EXPLAIN ANALYZE` output and generating human-readable optimization hints.

## Features

- Parses PostgreSQL `EXPLAIN ANALYZE` text output
- Builds a structured execution plan tree
- Detects suspicious sequential scans
- Detects poor row estimates
- Detects expensive sorts
- Detects nested loop risks
- Supports colorful terminal output with Rich
- Supports JSON output
- Optional PostgreSQL connection mode
- Optional web interface

## Installation

Install the basic CLI version:

```bash
pip install pg-explain-buddy
```

For database connection support:

```bash
pip install "pg-explain-buddy[db]"
```

For web interface:

```bash
pip install "pg-explain-buddy[web]"
```

For development:

```bash
pip install -e ".[dev,db,web]"
```

## Usage

Analyze a saved PostgreSQL plan:

```bash
pg-explain-buddy --file examples/sample_plan.txt
```

Analyze from stdin:

```bash
psql -c "EXPLAIN ANALYZE SELECT * FROM users;" | pg-explain-buddy
```

JSON output:

```bash
pg-explain-buddy --file examples/sample_plan.txt --json
```

Run a query directly:

```bash
pg-explain-buddy --dsn "postgresql://user:password@localhost:5432/dbname" --query "SELECT * FROM users"
```

## Development

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it on Windows:

```bash
.venv\Scripts\activate
```

Install the project in development mode:

```bash
pip install -e ".[dev,db,web]"
```

Run tests:

```bash
pytest
```

Check code style:

```bash
ruff check .
```

Run type checking:

```bash
mypy src
```

## License

MIT