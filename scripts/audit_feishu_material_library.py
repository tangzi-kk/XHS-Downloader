#!/usr/bin/env python3
"""Audit Feishu Bitable material library schema.

Read-only script that outputs table names, field names/types/IDs, and view names.
Supports --output to save JSON snapshot, --save for auto-timestamped file.

Usage:
    python scripts/audit_feishu_material_library.py
    python scripts/audit_feishu_material_library.py --output docs/backups/schema.json
    python scripts/audit_feishu_material_library.py --save
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime


def run_lark_cli(*args: str) -> dict:
    """Run lark-cli and return parsed JSON output."""
    cmd = ["/Users/changbeifenggongzuoshi/.local/bin/lark-cli"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli failed: {result.stderr}")
    return json.loads(result.stdout)


def redact_secrets(data: str) -> str:
    """Redact potential secrets from output."""
    for pattern in ["app_secret", "APP_SECRET", "secret", "token", "cookie"]:
        data = data.replace(f'"{pattern}": ', f'"{pattern}": "***REDACTED***"')
    return data


def audit_table(base_token: str, table_id: str, table_name: str) -> dict:
    """Audit a single table: fields and views."""
    # List fields
    field_data = run_lark_cli("base", "+field-list", "--base-token", base_token, "--table-id", table_id, "--limit", "200")
    fields = field_data.get("data", {}).get("fields", [])

    # List views
    view_data = run_lark_cli("base", "+view-list", "--base-token", base_token, "--table-id", table_id)
    views = view_data.get("data", {}).get("items", view_data.get("data", {}).get("views", []))

    # Count records (best-effort, some tables may fail)
    try:
        record_data = run_lark_cli("base", "+record-list", "--base-token", base_token, "--table-id", table_id, "--limit", "1")
        total = record_data.get("data", {}).get("total", 0)
    except Exception:
        total = -1  # Unknown

    return {
        "table_id": table_id,
        "table_name": table_name,
        "field_count": len(fields),
        "fields": [
            {
                "field_id": f["id"],
                "field_name": f["name"],
                "type": f["type"],
                "options": [opt["name"] for opt in f.get("options", [])],
                "multiple": f.get("multiple"),
                "link_table": f.get("link_table"),
            }
            for f in fields
        ],
        "view_count": len(views),
        "views": [
            {
                "view_id": v.get("view_id", v.get("id")),
                "view_name": v["name"],
                "view_type": v.get("view_type", v.get("type")),
            }
            for v in views
        ],
        "record_count": total,
    }


def main():
    parser = argparse.ArgumentParser(description="Audit Feishu Bitable schema")
    parser.add_argument("--base-token", help="Bitable app token (reads from env FEISHU_BITABLE_APP_TOKEN)")
    parser.add_argument("--output", help="Save JSON snapshot to this path")
    parser.add_argument("--save", action="store_true", help="Auto-save with timestamp")
    args = parser.parse_args()

    base_token = args.base_token or os.environ.get("FEISHU_BITABLE_APP_TOKEN")
    if not base_token:
        # Fallback: read from render.yaml
        try:
            with open("render.yaml") as f:
                content = f.read()
            import re
            m = re.search(r'key:\s*FEISHU_BITABLE_APP_TOKEN\s*\n\s*value:\s*(\S+)', content)
            if m:
                base_token = m.group(1)
        except FileNotFoundError:
            pass
    if not base_token:
        print("ERROR: No base token found. Set FEISHU_BITABLE_APP_TOKEN or pass --base-token", file=sys.stderr)
        sys.exit(1)

    print(f"Base token: {base_token[:4]}...{base_token[-4:]}")

    # List all tables
    table_data = run_lark_cli("base", "+table-list", "--base-token", base_token)
    tables = table_data.get("data", {}).get("tables", [])
    print(f"Found {len(tables)} tables\n")

    snapshot = {
        "base_token_prefix": f"{base_token[:4]}...{base_token[-4:]}",
        "audit_time": datetime.now().isoformat(),
        "tables": [],
    }

    for t in tables:
        tid = t["id"]
        tname = t["name"]
        print(f"--- {tname} ({tid}) ---")
        table_info = audit_table(base_token, tid, tname)
        snapshot["tables"].append(table_info)
        print(f"  Fields: {table_info['field_count']}")
        print(f"  Views:  {table_info['view_count']}")
        print(f"  Records: {table_info['record_count']}")

    # Output
    output_json = json.dumps(snapshot, ensure_ascii=False, indent=2)

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w") as f:
            f.write(output_json)
        print(f"\nSaved to {args.output}")
    elif args.save:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = f"docs/backups/feishu-material-library-schema-{ts}.json"
        os.makedirs("docs/backups", exist_ok=True)
        with open(path, "w") as f:
            f.write(output_json)
        print(f"\nSaved to {path}")
    else:
        print("\n" + output_json)


if __name__ == "__main__":
    main()
