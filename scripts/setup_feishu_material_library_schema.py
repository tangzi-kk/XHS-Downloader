#!/usr/bin/env python3
"""Setup Feishu Bitable material library schema.

Safe schema migration script: only adds NEW fields and tables.
Never deletes, renames, changes types, or modifies existing records.

Usage:
    python scripts/setup_feishu_material_library_schema.py --dry-run
    python scripts/setup_feishu_material_library_schema.py --apply
    python scripts/setup_feishu_material_library_schema.py  # defaults to --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime


LARK_CLI = "/Users/changbeifenggongzuoshi/.local/bin/lark-cli"


def run_lark_cli(*args: str) -> dict:
    cmd = [LARK_CLI] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli failed ({result.returncode}): {result.stderr}")
    return json.loads(result.stdout)


def get_existing_fields(base_token: str, table_id: str) -> dict:
    data = run_lark_cli("base", "+field-list", "--base-token", base_token, "--table-id", table_id, "--limit", "200")
    fields = data.get("data", {}).get("fields", [])
    return {f["name"]: f["id"] for f in fields}


def get_existing_tables(base_token: str) -> dict:
    data = run_lark_cli("base", "+table-list", "--base-token", base_token)
    tables = data.get("data", {}).get("tables", [])
    return {t["name"]: t["id"] for t in tables}


# ── Field definitions ──

MATERIAL_LIBRARY_FIELDS = [
    {"name": "平台内容ID", "type": "text"},
    {"name": "作者账号ID", "type": "text"},
    {"name": "外部发布时间", "type": "datetime"},
    {"name": "内容形态", "type": "select", "multiple": False, "options": [
        {"name": "图文"}, {"name": "单视频"}, {"name": "多视频/动图"},
        {"name": "图文+视频"}, {"name": "直播/长视频切片"}, {"name": "纯文本/无媒体"}, {"name": "其他"},
    ]},
    {"name": "表达方式", "type": "select", "multiple": True, "options": [
        {"name": "口播"}, {"name": "字幕"}, {"name": "实拍"}, {"name": "混剪"},
        {"name": "动图"}, {"name": "图文"}, {"name": "情绪流"}, {"name": "知识讲解"},
        {"name": "剧情"}, {"name": "直播切片"},
    ]},
    {"name": "目标人群", "type": "select", "multiple": True, "options": [
        {"name": "情感困惑"}, {"name": "事业焦虑"}, {"name": "命理爱好者"},
        {"name": "成长自救"}, {"name": "年轻女性"}, {"name": "中老年"},
        {"name": "泛人群"}, {"name": "待判断"},
    ]},
    {"name": "素材价值", "type": "select", "multiple": False, "options": [
        {"name": "S·优先创作"}, {"name": "A·值得留"}, {"name": "B·普通参考"}, {"name": "C·仅备份"},
    ]},
    {"name": "创作状态", "type": "select", "multiple": False, "options": [
        {"name": "待筛选"}, {"name": "待提炼"}, {"name": "可创作"}, {"name": "创作中"},
        {"name": "待发布"}, {"name": "已发布"}, {"name": "归档"}, {"name": "不采用"},
    ]},
    {"name": "使用风险", "type": "select", "multiple": False, "options": [
        {"name": "未评估"}, {"name": "可参考"}, {"name": "需改写"},
        {"name": "仅作灵感"}, {"name": "不可直接使用"},
    ]},
    {"name": "AI·内容摘要", "type": "text"},
    {"name": "AI·视觉摘要", "type": "text"},
    {"name": "人工备注", "type": "text"},
    {"name": "使用次数", "type": "number"},
    {"name": "最后使用时间", "type": "datetime"},
]

CREATION_CARD_TABLE = {
    "name": "10·创作卡库",
    "fields": [
        {"name": "创作卡标题", "type": "text"},
        {"name": "创作方向", "type": "select", "multiple": False, "options": [
            {"name": "小红书图文"}, {"name": "抖音口播"}, {"name": "动图"}, {"name": "混剪"},
            {"name": "情绪短句"}, {"name": "命理解读"}, {"name": "直播切片"},
            {"name": "选题储备"}, {"name": "其他"},
        ]},
        {"name": "目标发布平台", "type": "select", "multiple": True, "options": [
            {"name": "小红书"}, {"name": "抖音"}, {"name": "视频号"}, {"name": "B站"},
            {"name": "微博"}, {"name": "快手"}, {"name": "公众号"}, {"name": "其他"},
        ]},
        {"name": "目标受众", "type": "select", "multiple": True, "options": [
            {"name": "情感困惑"}, {"name": "事业焦虑"}, {"name": "命理爱好者"},
            {"name": "成长自救"}, {"name": "年轻女性"}, {"name": "中老年"}, {"name": "泛人群"},
        ]},
        {"name": "核心观点", "type": "text"},
        {"name": "选题方向", "type": "text"},
        {"name": "黄金开头", "type": "text"},
        {"name": "文案结构", "type": "text"},
        {"name": "AI改写文案", "type": "text"},
        {"name": "人工定稿", "type": "text"},
        {"name": "封面标题", "type": "text"},
        {"name": "画面/分镜建议", "type": "text"},
        {"name": "制作状态", "type": "select", "multiple": False, "options": [
            {"name": "待生成"}, {"name": "待修改"}, {"name": "待制作"},
            {"name": "待发布"}, {"name": "已发布"}, {"name": "废弃"},
        ]},
        {"name": "发布链接", "type": "url"},
        {"name": "发布时间", "type": "datetime"},
        {"name": "复盘结论", "type": "text"},
    ],
}


def main():
    parser = argparse.ArgumentParser(description="Setup Feishu Bitable schema (safe migration)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True, help="Print plan without executing (default)")
    group.add_argument("--apply", action="store_true", help="Execute schema changes")
    parser.add_argument("--base-token", help="Bitable app token")
    parser.add_argument("--material-table-id", help="Material library table ID")
    args = parser.parse_args()

    apply_mode = args.apply
    base_token = args.base_token or os.environ.get("FEISHU_BITABLE_APP_TOKEN")
    material_table_id = args.material_table_id or os.environ.get("FEISHU_BITABLE_TABLE_ID")

    # Fallback: read from render.yaml
    if not base_token or not material_table_id:
        try:
            with open("render.yaml") as f:
                content = f.read()
            import re
            m = re.search(r'key:\s*FEISHU_BITABLE_APP_TOKEN\s*\n\s*value:\s*(\S+)', content)
            if m:
                base_token = base_token or m.group(1)
            m = re.search(r'key:\s*FEISHU_BITABLE_TABLE_ID\s*\n\s*value:\s*(\S+)', content)
            if m:
                material_table_id = material_table_id or m.group(1)
        except FileNotFoundError:
            pass

    if not base_token or not material_table_id:
        print("ERROR: Missing base token or table ID", file=sys.stderr)
        sys.exit(1)

    if apply_mode:
        print("=" * 60)
        print("  ⚠️  仅新增字段和新表，不改旧字段、不改记录")
        print("=" * 60)

    # ── Check existing schema ──
    existing_fields = get_existing_fields(base_token, material_table_id)
    existing_tables = get_existing_tables(base_token)

    print(f"\n素材库现有字段: {len(existing_fields)}")
    print(f"现有数据表: {len(existing_tables)}")
    print(f"10·创作卡库是否存在: {'是' if '10·创作卡库' in existing_tables else '否'}\n")

    # ── Phase: Add missing fields to 素材库 ──
    field_adds = []
    for field_def in MATERIAL_LIBRARY_FIELDS:
        name = field_def["name"]
        if name in existing_fields:
            print(f"  ✓ {name} — 已存在，跳过")
        else:
            field_adds.append(field_def)
            print(f"  + {name} — 待新增")

    # ── Phase: Create 10·创作卡库 if missing ──
    table_creates = []
    if "10·创作卡库" in existing_tables:
        print(f"\n  ✓ 10·创作卡库 — 已存在，跳过创建")
        # Check for missing fields in existing table
        card_table_id = existing_tables["10·创作卡库"]
        card_fields = get_existing_fields(base_token, card_table_id)
        print(f"  创作卡库现有字段: {len(card_fields)}")
        for fd in CREATION_CARD_TABLE["fields"]:
            if fd["name"] in card_fields:
                print(f"    ✓ {fd['name']} — 已存在，跳过")
            else:
                field_adds.append(fd)
                print(f"    + {fd['name']} — 待新增")
    else:
        table_creates.append(CREATION_CARD_TABLE)
        print(f"\n  + 10·创作卡库 — 待创建")

    # ── Summary ──
    print(f"\n{'=' * 60}")
    print(f"  新增字段: {len(field_adds)}")
    print(f"  新增数据表: {len(table_creates)}")
    print(f"  删除/修改: 0")
    print(f"{'=' * 60}\n")

    if not apply_mode:
        print("DRY RUN 完成。使用 --apply 执行变更。")
        return

    # ── Apply ──
    # Create table first if needed
    for table_def in table_creates:
        fields_json = json.dumps(table_def["fields"], ensure_ascii=False)
        print(f"Creating table: {table_def['name']}...")
        result = run_lark_cli(
            "base", "+table-create",
            "--base-token", base_token,
            "--name", table_def["name"],
            "--fields", fields_json,
        )
        new_table_id = result.get("data", {}).get("table_id", "unknown")
        print(f"  Created: {new_table_id}")

    # Add fields to material library
    target_table = material_table_id
    for field_def in field_adds:
        field_json = json.dumps(field_def, ensure_ascii=False)
        print(f"Creating field: {field_def['name']}...")
        result = run_lark_cli(
            "base", "+field-create",
            "--base-token", base_token,
            "--table-id", target_table,
            "--json", field_json,
        )
        print(f"  Done: {field_def['name']}")

    print(f"\n{'=' * 60}")
    print("  Schema apply 完成！")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
