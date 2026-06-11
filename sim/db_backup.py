"""
sim/db_backup.py — pure-Python Postgres dump fallback when pg_dump isn't
available. Walks every table in the public schema, SELECTs all rows, and
writes a single JSON file with column names + row data per table.

Not as canonical as pg_dump but it captures every row in a portable format
that can be inspected, grep'd, or restored with a companion script. The
file is human-readable and resilient to schema changes (column names are
recorded alongside the data).

Usage:
    DATABASE_URL='...' python3 sim/db_backup.py
    # → writes backup-YYYYMMDD-HHMMSS.json in the current directory

    # Optional: custom output path
    DATABASE_URL='...' python3 sim/db_backup.py --out /path/to/my-backup.json
"""
import os
import sys
import json
import argparse
import datetime
import decimal
import psycopg2
import psycopg2.extras


def _json_default(o):
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (bytes, bytearray, memoryview)):
        return bytes(o).hex()  # rare but handle it
    raise TypeError(f"unserializable type: {type(o).__name__}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=None,
                   help="output path (default: backup-YYYYMMDD-HHMMSS.json in cwd)")
    args = p.parse_args()

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: DATABASE_URL not set"); sys.exit(1)

    out_path = args.out or datetime.datetime.now().strftime("backup-%Y%m%d-%H%M%S.json")

    conn = psycopg2.connect(url)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Find every table in public schema
    cur.execute("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    """)
    table_names = [r["tablename"] for r in cur.fetchall()]
    print(f"found {len(table_names)} tables in public schema")

    dump = {
        "_meta": {
            "dumped_at":    datetime.datetime.utcnow().isoformat() + "Z",
            "table_count":  len(table_names),
            "format":       "v1",
        },
        "tables": {},
    }

    total_rows = 0
    for t in table_names:
        # Identifier is from pg_tables (trusted) so f-string is safe here.
        cur.execute(f'SELECT * FROM public."{t}"')
        rows = cur.fetchall()
        if not rows:
            cur.execute(f'SELECT column_name FROM information_schema.columns '
                        f"WHERE table_schema='public' AND table_name=%s "
                        f"ORDER BY ordinal_position", (t,))
            cols = [r["column_name"] for r in cur.fetchall()]
        else:
            cols = list(rows[0].keys())
        dump["tables"][t] = {
            "columns":  cols,
            "rows":     [[dict(r).get(c) for c in cols] for r in rows],
        }
        total_rows += len(rows)
        print(f"  {t:<24} {len(rows):>6} rows")

    cur.close()
    conn.close()

    with open(out_path, "w") as f:
        json.dump(dump, f, default=_json_default, indent=2)

    import os as _os
    size = _os.path.getsize(out_path)
    print(f"\n✓ wrote {out_path} ({total_rows} rows, {size:,} bytes)")


if __name__ == "__main__":
    main()
