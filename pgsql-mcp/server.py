"""PostgreSQL MCP Server — 通过 MCP 协议操作 PostgreSQL 数据库。

连接配置从本目录的 `.env` 文件或环境变量中读取，源码内不再含有凭据。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import asyncpg
import pandas as pd
from dotenv import load_dotenv
from fastmcp import Context, FastMCP

# ---------------------------------------------------------------------------
# 连接配置（外部化）
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env", override=False)

PG_CONNECTION_STRING = os.environ.get("PG_CONNECTION_STRING")
# 也支持拆分字段拼装
_PG_PARTS = {
    k: os.environ.get(v)
    for k, v in (
        ("host", "PG_HOST"),
        ("port", "PG_PORT"),
        ("user", "PG_USER"),
        ("password", "PG_PASSWORD"),
        ("database", "PG_DATABASE"),
    )
}


def _resolve_dsn() -> str:
    """从环境变量解析 DSN；缺失则抛错，要求用户在 .env 中配置。"""
    if PG_CONNECTION_STRING:
        return PG_CONNECTION_STRING
    missing = [k.upper() for k, v in _PG_PARTS.items() if not v]
    if missing:
        raise RuntimeError(
            "未找到 PostgreSQL 连接信息。请在 "
            f"{_HERE / '.env'} 中设置 PG_CONNECTION_STRING，或设置以下变量："
            f"PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DATABASE。"
            f"当前缺失：{missing}"
        )
    host = _PG_PARTS["host"]
    port = _PG_PARTS.get("port") or "5432"
    user = _PG_PARTS["user"]
    password = _PG_PARTS["password"]
    database = _PG_PARTS["database"]
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MAX_ROWS = 1000
DEFAULT_SAMPLE_LIMIT = 10
# 读取 .sql 脚本文件的大小上限（防止误读超大文件）
MAX_SQL_FILE_SIZE = 5 * 1024 * 1024

# ---------------------------------------------------------------------------
# 连接池
# ---------------------------------------------------------------------------
pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    """获取或重建连接池。"""
    global pool
    if pool is None or pool._closed:
        dsn = _resolve_dsn()
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    return pool


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
_SELECT_PREFIXES = ("SELECT", "WITH", "EXPLAIN", "SHOW")


def is_select_query(sql: str) -> bool:
    cleaned = re.sub(r"^\s*--.*$", "", sql, flags=re.MULTILINE).strip().upper()
    # 允许 CTE / EXPLAIN / SHOW
    return any(cleaned.startswith(p) for p in _SELECT_PREFIXES)


def format_rows(columns: list[str], rows: list) -> list[dict]:
    return [dict(zip(columns, [a if not isinstance(a, datetime) else a.isoformat() for a in row])) for row in rows]


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", s).strip("_") or "export"


# ---------------------------------------------------------------------------
# 从 .sql 脚本文件读取（避免超长 SQL 塞进提示词）
# ---------------------------------------------------------------------------
def _read_sql_file(path: str) -> str:
    """读取 .sql 脚本文件内容，自动尝试常见编码。"""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"SQL 脚本文件不存在: {p}")
    if p.stat().st_size > MAX_SQL_FILE_SIZE:
        raise ValueError(f"脚本文件过大（>{MAX_SQL_FILE_SIZE // (1024 * 1024)}MB），请拆分后重试: {p}")
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法解析脚本文件编码: {p}（已尝试 utf-8 / gbk / gb18030）")


def split_sql_script(sql: str) -> list[str]:
    """把 SQL 脚本拆分成多条独立语句。

    用『等长空格掩码』替换字符串与注释内容（单引号/双引号字符串、
    `--` 行注释、`/* */` 块注释），保证这些区域内的分号不参与切分，
    且切分位置能映射回原文。不处理存储过程/DELIMITER。
    """
    masked = sql
    for pattern in (
        r"'(?:\\'|[^'])*'",
        r'"(?:\\"|[^"])*"',
        r"--[^\n]*",
    ):
        masked = re.sub(pattern, lambda m: " " * len(m.group()), masked)
    masked = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group()), masked, flags=re.DOTALL)

    stmts: list[str] = []
    start = 0
    for i, ch in enumerate(masked):
        if ch == ";":
            seg = sql[start:i].strip()
            if seg:
                stmts.append(seg)
            start = i + 1
    tail = sql[start:].strip()
    if tail:
        stmts.append(tail)
    return stmts


# ---------------------------------------------------------------------------
# MCP 实例
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "PostgreSQL MCP Server",
    instructions=(
        "通过 MCP 协议操作 PostgreSQL 数据库，支持查询、导出、Schema 浏览、列采样等操作。\n"
        "提示：SQL 较长（如 >500 字符）或需执行多语句脚本时，优先用 sql_file 参数指向 .sql 文件，"
        "避免把超长 SQL 塞进提示词导致截断/异常。"
    ),
)


# ---------------------------------------------------------------------------
# 原 query / execute / 结构浏览工具（保持向后兼容，导出能力被 export_query 替代）
# ---------------------------------------------------------------------------
@mcp.tool()
async def query(
    sql: Optional[str] = None,
    sql_file: Optional[str] = None,
    limit: Optional[int] = None,
    ctx: Context = None,
) -> str:
    """执行 SELECT 查询并以 JSON 返回，默认最多 1000 行（可由 limit 覆盖）。

    sql 与 sql_file 二选一：
      - sql: SQL 字符串（仅支持单条语句）
      - sql_file: .sql 脚本文件路径（长 SQL 或多条只读语句；脚本逐条执行并汇总）
    """
    if sql is not None and str(sql).strip():
        stmts = split_sql_script(str(sql))
        if len(stmts) > 1:
            return "错误：sql 参数仅支持单条语句（检测到多条）。长 SQL 或多语句脚本请用 sql_file 参数。"
    elif sql_file:
        try:
            stmts = split_sql_script(_read_sql_file(sql_file))
        except Exception as e:
            return f"错误: {e}"
    else:
        return "错误：请提供 sql 或 sql_file"
    if not stmts:
        return "错误：SQL 内容为空"

    for i, s in enumerate(stmts, 1):
        if not is_select_query(s):
            return f"错误：query 工具只允许 SELECT/EXPLAIN/SHOW 查询（第 {i} 条不符），请使用 execute 工具执行写操作"

    try:
        conn_pool = await get_pool()
        if len(stmts) == 1:
            s = stmts[0]
            async with conn_pool.acquire() as conn:
                total = await conn.fetchval(f"SELECT COUNT(*) FROM ({s}) AS _count_subquery")
                n = min(int(limit) if limit else MAX_ROWS, 10_000)
                rows = await conn.fetch(f"{s} LIMIT {n}")
                columns = list(rows[0].keys()) if rows else []
                data = format_rows(columns, rows)
            result = {"columns": columns, "rows": data, "total": total, "returned": len(data)}
            prefix = f"查询结果（返回 {len(data)} 行，共 {total} 行）：\n" if total > len(data) else ""
            return prefix + json.dumps(result, ensure_ascii=False, default=str)

        # 多条脚本：逐条执行，汇总
        n = min(int(limit) if limit else MAX_ROWS, 10_000)
        results = []
        for i, s in enumerate(stmts, 1):
            async with conn_pool.acquire() as conn:
                rows = await conn.fetch(f"{s} LIMIT {n}")
                columns = list(rows[0].keys()) if rows else []
                data = format_rows(columns, rows)
            preview = s[:100] + ("…" if len(s) > 100 else "")
            results.append({"stmt": i, "sql_preview": preview, "columns": columns, "rows": data, "returned": len(data)})
        return json.dumps(
            {"statement_count": len(results), "results": results},
            ensure_ascii=False,
            default=str,
        )
    except Exception as e:
        return f"查询错误: {str(e)}"


@mcp.tool()
async def execute(
    sql: Optional[str] = None,
    sql_file: Optional[str] = None,
    confirm: bool = False,
    ctx: Context = None,
) -> str:
    """执行写操作（INSERT/UPDATE/DELETE/DDL），需 confirm=true。

    sql 与 sql_file 二选一：
      - sql: SQL 字符串（仅支持单条语句）
      - sql_file: .sql 脚本文件路径（支持多条写语句，逐条执行；SELECT 请走 query）
    """
    if sql is not None and str(sql).strip():
        stmts = split_sql_script(str(sql))
        if len(stmts) > 1:
            return "错误：sql 参数仅支持单条语句（检测到多条）。多语句脚本请用 sql_file 参数。"
    elif sql_file:
        try:
            stmts = split_sql_script(_read_sql_file(sql_file))
        except Exception as e:
            return f"错误: {e}"
    else:
        return "错误：请提供 sql 或 sql_file"
    if not stmts:
        return "错误：SQL 内容为空"

    for i, s in enumerate(stmts, 1):
        if is_select_query(s):
            return f"提示：第 {i} 条是 SELECT 查询，请使用 query 工具；导出请用 export_query。"

    if not confirm:
        preview = "\n".join(f"[{i}] {s[:120]}{'…' if len(s) > 120 else ''}" for i, s in enumerate(stmts, 1))
        return f"安全确认：请设置 confirm=true 以确认执行以下 {len(stmts)} 条写操作\n{preview}"
    try:
        conn_pool = await get_pool()
        lines = []
        for i, s in enumerate(stmts, 1):
            async with conn_pool.acquire() as conn:
                result = await conn.execute(s)
            lines.append(f"[{i}] 执行成功: {result}")
        return "\n".join(lines) if len(lines) > 1 else lines[0]
    except Exception as e:
        return f"执行错误: {str(e)}"


@mcp.tool()
async def list_databases(ctx: Context = None) -> str:
    """列出所有非模板数据库。"""
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT datname, pg_encoding_to_char(encoding) AS encoding, datcollate AS collation "
                "FROM pg_database WHERE datistemplate = false ORDER BY datname"
            )
        return json.dumps(
            [{"name": r["datname"], "encoding": r["encoding"], "collation": r["collation"]} for r in rows],
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        return f"错误: {str(e)}"


@mcp.tool()
async def list_schemas(ctx: Context = None) -> str:
    """列出所有非系统 schema。"""
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
                "ORDER BY schema_name"
            )
        return json.dumps([r["schema_name"] for r in rows], ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {str(e)}"


@mcp.tool()
async def list_tables(schema: str = "public", ctx: Context = None) -> str:
    """列出指定 schema 下的所有表（含视图）。"""
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = $1 ORDER BY table_name",
                schema,
            )
        return json.dumps(
            [{"name": r["table_name"], "type": r["table_type"]} for r in rows],
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        return f"错误: {str(e)}"


@mcp.tool()
async def describe_table(table: str, schema: str = "public", ctx: Context = None) -> str:
    """查看表结构（列、类型、是否为空、默认值、主键）。"""
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            columns = await conn.fetch(
                "SELECT column_name, data_type, is_nullable, column_default, "
                "character_maximum_length, numeric_precision "
                "FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = $2 ORDER BY ordinal_position",
                schema,
                table,
            )
            pkeys = await conn.fetch(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
                "WHERE tc.table_schema = $1 AND tc.table_name = $2 AND tc.constraint_type = 'PRIMARY KEY'",
                schema,
                table,
            )
            pk_cols = {r["column_name"] for r in pkeys}
        out = []
        for col in columns:
            info: dict[str, Any] = {
                "column": col["column_name"],
                "type": col["data_type"],
                "nullable": col["is_nullable"] == "YES",
                "default": col["column_default"],
                "primary_key": col["column_name"] in pk_cols,
            }
            if col["character_maximum_length"]:
                info["max_length"] = col["character_maximum_length"]
            if col["numeric_precision"]:
                info["precision"] = col["numeric_precision"]
            out.append(info)
        return json.dumps(out, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {str(e)}"


@mcp.tool()
async def list_indexes(table: str, schema: str = "public", ctx: Context = None) -> str:
    """查看表的索引。"""
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = $1 AND tablename = $2 ORDER BY indexname",
                schema,
                table,
            )
        return json.dumps(
            [{"name": r["indexname"], "definition": r["indexdef"]} for r in rows],
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        return f"错误: {str(e)}"


@mcp.tool()
async def list_roles(ctx: Context = None) -> str:
    """列出所有角色（排除 pg_ 开头）。"""
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication "
                "FROM pg_roles WHERE rolname NOT LIKE 'pg_%' ORDER BY rolname"
            )
        out = [
            {
                "name": r["rolname"],
                "superuser": r["rolsuper"],
                "can_login": r["rolcanlogin"],
                "create_db": r["rolcreatedb"],
                "create_role": r["rolcreaterole"],
                "replication": r["rolreplication"],
            }
            for r in rows
        ]
        return json.dumps(out, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {str(e)}"


@mcp.tool()
async def create_table(sql: str, ctx: Context = None) -> str:
    """执行 CREATE TABLE / CREATE VIEW / CREATE INDEX 等 DDL（仅 CREATE 开头）。"""
    if not re.sub(r"^\s*--.*$", "", sql, flags=re.MULTILINE).strip().upper().startswith("CREATE"):
        return "错误：只允许 CREATE 语句"
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            await conn.execute(sql)
            return "创建成功"
    except Exception as e:
        return f"创建错误: {str(e)}"


@mcp.tool()
async def drop_table(
    table: str,
    schema: str = "public",
    cascade: bool = False,
    confirm: bool = False,
    ctx: Context = None,
) -> str:
    """删除表，需 confirm=true。"""
    if not confirm:
        return "安全确认：请设置 confirm=true 以确认删除此表"
    cascade_clause = " CASCADE" if cascade else ""
    sql = f'DROP TABLE IF EXISTS "{schema}"."{table}"{cascade_clause}'
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            await conn.execute(sql)
            return f"表 {schema}.{table} 已删除"
    except Exception as e:
        return f"删除错误: {str(e)}"


# ---------------------------------------------------------------------------
# 新增：通用导出工具 export_query
# ---------------------------------------------------------------------------
@mcp.tool()
async def export_query(
    sql: str,
    format: str = "csv",
    export_dir: Optional[str] = None,
    filename: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """执行 SELECT 并把**全部**结果导出为 csv / xlsx（无行数上限）。

    Args:
        sql: SELECT/WITH/EXPLAIN/SHOW 语句
        format: 'csv' 或 'xlsx'
        export_dir: 导出目录，默认当前工作目录
        filename: 自定义文件名（不含后缀），默认 `export_<时间戳>`
    """
    fmt = (format or "").lower()
    if fmt not in ("csv", "xlsx"):
        return f"错误：不支持的导出格式 '{format}'，仅支持 csv / xlsx"
    if not is_select_query(sql):
        return "错误：export_query 仅接受 SELECT/WITH/EXPLAIN/SHOW 语句"
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT COUNT(*) FROM ({sql}) AS _count_subquery")
            rows = await conn.fetch(sql)
            columns = list(rows[0].keys()) if rows else []
            data = format_rows(columns, rows)

        out_dir = Path(export_dir).expanduser() if export_dir else Path.cwd()
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now_tag()
        base = _safe_name(filename) if filename else f"export_{stamp}"
        filepath = out_dir / f"{base}.{fmt}"

        df = pd.DataFrame(data)
        if fmt == "csv":
            df.to_csv(filepath, index=False, encoding="utf-8-sig")
        else:
            df.to_excel(filepath, index=False, engine="openpyxl")

        return f"导出成功\n文件路径: {filepath}\n记录数: {total}\n实际写入: {len(data)}"
    except Exception as e:
        return f"导出错误: {str(e)}"


# ---------------------------------------------------------------------------
# 新增：Schema 浏览 / 列采样 工具
# ---------------------------------------------------------------------------
@mcp.tool()
async def show_tables(
    schema: str = "public",
    with_stats: bool = True,
    ctx: Context = None,
) -> str:
    """列出 schema 下的表与视图，可选带回行数与"占用估算"。

    行数通过 `pg_stat_user_tables` 取近似值（`n_live_tup`），不执行 COUNT，
    对大表无压力。
    """
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            tables = await conn.fetch(
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = $1 ORDER BY table_name",
                schema,
            )
            if not with_stats:
                return json.dumps(
                    [{"name": r["table_name"], "type": r["table_type"]} for r in tables],
                    ensure_ascii=False,
                    indent=2,
                )
            stats = await conn.fetch(
                "SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) AS total_size, "
                "pg_size_pretty(pg_relation_size(relid)) AS table_size "
                "FROM pg_stat_user_tables WHERE schemaname = $1",
                schema,
            )
            stats_idx = {s["relname"]: s for s in stats}
            out = []
            for r in tables:
                name = r["table_name"]
                s = stats_idx.get(name)
                out.append(
                    {
                        "name": name,
                        "type": r["table_type"],
                        "approx_rows": s["n_live_tup"] if s else None,
                        "total_size": s["total_size"] if s else None,
                        "table_size": s["table_size"] if s else None,
                    }
                )
            return json.dumps(out, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {str(e)}"


@mcp.tool()
async def describe_columns(
    table: str,
    schema: str = "public",
    ctx: Context = None,
) -> str:
    """查看表的列信息（含主键与注释）。"""
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            cols = await conn.fetch(
                """
                SELECT c.column_name, c.data_type, c.is_nullable, c.column_default,
                       c.character_maximum_length, c.numeric_precision, c.numeric_scale,
                       pgd.description AS comment
                FROM information_schema.columns c
                LEFT JOIN pg_catalog.pg_description pgd
                  ON pgd.objoid = (quote_ident($1) || '.' || quote_ident(c.column_name))::regclass
                 AND pgd.objsubid = c.ordinal_position
                WHERE c.table_schema = $2 AND c.table_name = $1
                ORDER BY c.ordinal_position
                """,
                table,
                schema,
            )
            pks = await conn.fetch(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
                "WHERE tc.table_schema = $1 AND tc.table_name = $2 AND tc.constraint_type = 'PRIMARY KEY'",
                schema,
                table,
            )
            pk_set = {r["column_name"] for r in pks}
        out = []
        for c in cols:
            info = {
                "column": c["column_name"],
                "type": c["data_type"],
                "nullable": c["is_nullable"] == "YES",
                "default": c["column_default"],
                "primary_key": c["column_name"] in pk_set,
                "comment": c["comment"],
            }
            if c["character_maximum_length"]:
                info["max_length"] = c["character_maximum_length"]
            if c["numeric_precision"]:
                info["precision"] = c["numeric_precision"]
                if c["numeric_scale"] is not None:
                    info["scale"] = c["numeric_scale"]
            out.append(info)
        return json.dumps({"schema": schema, "table": table, "columns": out}, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {str(e)}"


@mcp.tool()
async def sample_rows(
    table: str,
    schema: str = "public",
    limit: int = DEFAULT_SAMPLE_LIMIT,
    order_by: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """从表中取若干样本行，便于快速了解数据样式。

    Args:
        table: 表名
        schema: 所在 schema
        limit: 返回行数（默认 10，最多 1000）
        order_by: 可选的排序列；缺省时随机抽样（使用 RANDOM()，仅对小表适用）
    """
    try:
        n = max(1, min(int(limit), 1000))
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            if order_by:
                if not re.fullmatch(r"[A-Za-z_][\w]*", order_by):
                    return "错误：order_by 仅允许简单的列名"
                sql = f'SELECT * FROM "{schema}"."{table}" ORDER BY "{order_by}" LIMIT {n}'
            else:
                sql = f'SELECT * FROM "{schema}"."{table}" ORDER BY random() LIMIT {n}'
            rows = await conn.fetch(sql)
            columns = list(rows[0].keys()) if rows else []
            data = format_rows(columns, rows)
        return json.dumps(
            {"schema": schema, "table": table, "returned": len(data), "rows": data},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    except Exception as e:
        return f"错误: {str(e)}"


@mcp.tool()
async def list_foreign_keys(
    table: Optional[str] = None,
    schema: str = "public",
    ctx: Context = None,
) -> str:
    """查看表（或整个 schema）的外键关系。

    不传 table 时，列出整个 schema 的外键。
    """
    try:
        conn_pool = await get_pool()
        async with conn_pool.acquire() as conn:
            sql = """
                SELECT tc.table_schema, tc.table_name, kcu.column_name,
                       ccu.table_schema AS ref_schema, ccu.table_name AS ref_table,
                       ccu.column_name AS ref_column,
                       tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                  ON ccu.constraint_name = tc.constraint_name
                 AND ccu.table_schema = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = $1
            """
            params: list[Any] = [schema]
            if table:
                sql += " AND tc.table_name = $2"
                params.append(table)
            sql += " ORDER BY tc.table_name, kcu.column_name"
            rows = await conn.fetch(sql, *params)
        out = [
            {
                "schema": r["table_schema"],
                "table": r["table_name"],
                "column": r["column_name"],
                "ref_schema": r["ref_schema"],
                "ref_table": r["ref_table"],
                "ref_column": r["ref_column"],
                "constraint": r["constraint_name"],
            }
            for r in rows
        ]
        return json.dumps(out, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {str(e)}"


# ---------------------------------------------------------------------------
# 资源（连接信息 / 库 / 表）
# ---------------------------------------------------------------------------
@mcp.resource("pg://connection")
async def get_connection_info() -> str:
    """当前连接信息（不含密码）。"""
    pool_ = await get_pool()
    async with pool_.acquire() as conn:
        version = await conn.fetchval("SELECT version()")
        db = await conn.fetchval("SELECT current_database()")
        user = await conn.fetchval("SELECT current_user")
    try:
        dsn = _resolve_dsn()
        redacted = re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", dsn)
    except Exception:
        redacted = None
    return json.dumps(
        {"dsn_redacted": redacted, "database": db, "user": user, "version": version},
        ensure_ascii=False,
        indent=2,
    )


@mcp.resource("pg://databases")
async def get_databases() -> str:
    pool_ = await get_pool()
    async with pool_.acquire() as conn:
        rows = await conn.fetch("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname")
    return json.dumps([r["datname"] for r in rows], ensure_ascii=False)


@mcp.resource("pg://tables")
async def get_tables() -> str:
    pool_ = await get_pool()
    async with pool_.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
    return json.dumps([r["table_name"] for r in rows], ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
