"""MySQL MCP Server — 通过 MCP 协议操作 MySQL 数据库。

工具列表（精简 5 个）：
- query         只读 SELECT/WITH/EXPLAIN/SHOW/DESCRIBE，返回 JSON，默认 LIMIT 1000
- export_query  只读 SELECT + 写 csv/xlsx 文件，无行数上限
- execute       万能：自动判别只读/写；只读直接执行，写操作需 confirm=true
- describe_table  输出表完整结构（字段/类型/可空/默认值/键/字符集/校对/注释）

连接配置优先从 Claude 配置（~/.claude.json）mcpServers.env 字段读取，
也兼容本目录 `.env` 文件。源码内不再含有凭据。

多语句默认禁用（PyMySQL 默认 client_flag）。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pymysql
from pymysql.cursors import DictCursor
from dotenv import load_dotenv
from fastmcp import Context, FastMCP

# ---------------------------------------------------------------------------
# 连接配置（外部化）
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
if (_HERE / ".env").exists():
    # 仅当 .env 存在时加载；Claude 配置里 env 字段注入的同名变量优先级更高
    load_dotenv(_HERE / ".env", override=False)


def _env(*keys: str, default: Optional[str] = None) -> Optional[str]:
    for k in keys:
        v = os.environ.get(k)
        if v is not None and v != "":
            return v
    return default


MYSQL_HOST = _env("MYSQL_HOST", "DB_HOST")
MYSQL_PORT = int(_env("MYSQL_PORT", "DB_PORT", default="3306") or "3306")
MYSQL_USER = _env("MYSQL_USER", "DB_USER")
MYSQL_PASSWORD = _env("MYSQL_PASSWORD", "DB_PASSWORD")
MYSQL_DATABASE = _env("MYSQL_DATABASE", "DB_DATABASE")  # 可选：未指定库时只能跨库查询
MYSQL_CHARSET = _env("MYSQL_CHARSET", default="utf8mb4")
MYSQL_CONNECT_TIMEOUT = int(_env("MYSQL_CONNECT_TIMEOUT", default="10") or "10")
MYSQL_READ_TIMEOUT = int(_env("MYSQL_READ_TIMEOUT", default="60") or "60")


def _check_required_env() -> None:
    """启动前校验必填连接配置。开源分享后每人配置不同，连接凭据不设默认值。

    缺失时抛出明确错误，提示从环境变量（MCP 客户端 env 字段 / .env 文件）注入。
    """
    missing = [name for name, v in (("MYSQL_HOST", MYSQL_HOST), ("MYSQL_USER", MYSQL_USER), ("MYSQL_PASSWORD", MYSQL_PASSWORD)) if not v]
    if missing:
        raise RuntimeError(
            "缺少必填连接配置: " + ", ".join(missing) + "。\n"
            "请在 MCP 客户端配置的 mcpServers.env 中设置，或在本目录 .env 文件中设置（参照 .env.example）。"
        )


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MAX_ROWS = 1000
# 读取 .sql 脚本文件的大小上限（防止误读超大文件）
MAX_SQL_FILE_SIZE = 5 * 1024 * 1024

# 触发"自动视为只读"的前缀（execute/query 自动判别时使用）
_READ_PREFIXES = ("SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC", "USE", "HELP")

# 高危关键词（execute 写模式下额外告警）
_DANGER_KEYWORDS = ("DROP", "TRUNCATE", "DELETE", "UPDATE", "ALTER", "RENAME", "GRANT", "REVOKE")


# ---------------------------------------------------------------------------
# 连接管理（短连接，按需创建）
# ---------------------------------------------------------------------------
def get_connection(database: Optional[str] = None) -> pymysql.connections.Connection:
    """获取一个新连接。database 为 None 时连接到服务器（不指定库）。"""
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=database if database is not None else MYSQL_DATABASE,
        charset=MYSQL_CHARSET,
        connect_timeout=MYSQL_CONNECT_TIMEOUT,
        read_timeout=MYSQL_READ_TIMEOUT,
        cursorclass=DictCursor,
        autocommit=False,
        # 多语句默认禁用；client_flag 不加 CLIENT.MULTI_STATEMENTS
    )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _strip_lead(s: str) -> str:
    """去掉前导空白、括号、注释，返回大写首词所在位置。"""
    # 去掉 -- 单行注释
    s = re.sub(r"^\s*--.*$", "", s, flags=re.MULTILINE)
    # 去掉前导空白 / 括号
    s = re.sub(r"^[\s(]+", "", s)
    return s.strip().upper()


def is_read_query(sql: str) -> bool:
    """判断是否只读查询（SELECT/WITH/EXPLAIN/SHOW/DESCRIBE/USE/HELP 等）。"""
    head = _strip_lead(sql)
    return any(head.startswith(p) for p in _READ_PREFIXES)


def contains_multi_statements(sql: str) -> bool:
    """粗略判断是否含多条语句（按分号分隔；忽略末尾分号和字符串内的分号）。"""
    # 去掉单引号/双引号/反引号字符串内容
    cleaned = re.sub(r"'(?:\\'|[^'])*'", "''", sql)
    cleaned = re.sub(r'"(?:\\"|[^"])*"', '""', cleaned)
    cleaned = re.sub(r"`(?:\\`|[^`])*`", "``", cleaned)
    # 去掉 -- 行注释
    cleaned = re.sub(r"--.*?$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    # 按分号切
    parts = [p.strip() for p in cleaned.split(";") if p.strip()]
    return len(parts) > 1


def has_danger_keyword(sql: str) -> bool:
    """粗略检测是否含高危关键字（用于写操作前的二次告警）。"""
    upper = sql.upper()
    # 用单词边界匹配，避免误判（例如 column 名包含 drop_xxx）
    return any(re.search(rf"\b{kw}\b", upper) for kw in _DANGER_KEYWORDS)


# ---------------------------------------------------------------------------
# 从 .sql 脚本文件读取（避免超长 SQL 塞进提示词）
# ---------------------------------------------------------------------------
def _read_sql_file(path: str) -> str:
    """读取 .sql 脚本文件内容，自动尝试常见编码。

    Args:
        path: 脚本文件路径（支持绝对/相对路径与 ~ 展开）

    Returns:
        str: 脚本内容

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 文件过大或编码无法识别
    """
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

    用『等长空格掩码』替换字符串与注释内容：单引号/双引号/反引号字符串、
    `--` 行注释、`/* */` 块注释统一替换为等长空格，保证这些区域内的分号
    不参与切分，且切分位置能映射回原文。

    注意：不处理存储过程/函数/DELIMITER 等函数体内含分号的语句。

    Args:
        sql: 脚本内容

    Returns:
        list[str]: 拆分后的语句列表（已去首尾空白，忽略空语句）
    """
    masked = sql
    for pattern in (
        r"'(?:\\'|[^'])*'",
        r'"(?:\\"|[^"])*"',
        r"`(?:\\`|[^`])*`",
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


def _jsonify(value: Any) -> Any:
    """把 pymysql 返回值转换为可 JSON 序列化的标量。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        try:
            if value == value.to_integral_value():
                return int(value)
        except Exception:
            pass
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return value.hex()
    return value


def format_rows(rows: list[dict]) -> list[dict]:
    return [{k: _jsonify(v) for k, v in row.items()} for row in rows]


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", s).strip("_") or "export"


def _quote_ident(name: str) -> str:
    """对 MySQL 标识符做反引号转义，并校验只含合法字符。"""
    if not re.fullmatch(r"[A-Za-z_][\w]*", name):
        raise ValueError(f"非法标识符: {name!r}（只允许字母/数字/下划线，且不能以数字开头）")
    return f"`{name}`"


# ---------------------------------------------------------------------------
# MCP 实例
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "MySQL MCP Server",
    instructions=(
        "通过 MCP 协议操作 MySQL 数据库。提供 5 个工具：\n"
        "1) query(sql=None, limit=None, sql_file=None) - 只读 SELECT，返回 JSON，默认 LIMIT 1000；\n"
        "2) export_query(sql, format='csv', export_dir=None, filename=None) - 只读 SELECT 并导出 csv/xlsx；\n"
        "3) execute(sql=None, confirm=False, sql_file=None) - 万能执行：自动识别只读/写，只读直接返回，写操作需 confirm=true；\n"
        "4) describe_table(table, database=None) - 输出完整表结构（字段/类型/可空/默认值/键/字符集/校对/注释）。\n"
        "DROP/TRUNCATE/DELETE 等高危操作会要求 confirm=true 并打印告警。\n"
        "提示：SQL 较长（如 >500 字符）或需执行多语句脚本时，优先用 sql_file 参数指向 .sql 文件，"
        "避免把超长 SQL 塞进提示词导致截断/异常。"
    ),
)


# ---------------------------------------------------------------------------
# 工具 1：query — 只读 SELECT
# ---------------------------------------------------------------------------
def _query_one(sql: str, limit: Optional[int] = None) -> dict:
    """执行单条只读查询，返回 {columns, rows, total, returned}。"""
    n = min(int(limit) if limit else MAX_ROWS, 10_000)
    cleaned = sql.rstrip(";").strip()
    with get_connection() as conn:
        with conn.cursor() as cur:
            total: Optional[int] = None
            if cleaned.upper().startswith(("SELECT", "WITH")):
                try:
                    cur.execute(f"SELECT COUNT(*) AS _c FROM ({cleaned}) AS _count_subquery")
                    total = int((cur.fetchone() or {}).get("_c") or 0)
                except Exception:
                    total = None
            cur.execute(f"{cleaned} LIMIT {n}")
            rows = cur.fetchall()
    columns = list(rows[0].keys()) if rows else []
    data = format_rows(rows)
    return {"columns": columns, "rows": data, "total": total, "returned": len(data)}


@mcp.tool()
def query(
    sql: Optional[str] = None,
    sql_file: Optional[str] = None,
    limit: Optional[int] = None,
    ctx: Context = None,
) -> str:
    """执行 SELECT/WITH/EXPLAIN/SHOW/DESCRIBE 等只读查询，返回 JSON。

    sql 与 sql_file 二选一：
      - sql: SQL 字符串（仅支持单条语句）
      - sql_file: .sql 脚本文件路径（长 SQL 或多条只读语句；脚本逐条执行并汇总）

    自动追加 LIMIT，默认最多 1000 行（可由 limit 覆盖，硬上限 10000）。
    """
    if sql is not None and str(sql).strip():
        script = str(sql)
        stmts = split_sql_script(script)
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

    # 校验全部为只读
    for i, s in enumerate(stmts, 1):
        if not is_read_query(s):
            return f"错误：query 仅接受只读语句，第 {i} 条不是只读语句。写操作请用 execute。"

    try:
        if len(stmts) == 1:
            result = _query_one(stmts[0], limit)
            prefix = (
                f"查询结果（返回 {result['returned']} 行，共 {result['total']} 行）：\n"
                if result["total"] is not None and result["total"] > result["returned"]
                else ""
            )
            return prefix + json.dumps(result, ensure_ascii=False, default=str)
        # 多条脚本：逐条执行，汇总
        results = []
        for i, s in enumerate(stmts, 1):
            r = _query_one(s, limit)
            preview = s[:100] + ("…" if len(s) > 100 else "")
            results.append({"stmt": i, "sql_preview": preview, **r})
        return json.dumps(
            {"statement_count": len(results), "results": results},
            ensure_ascii=False,
            default=str,
        )
    except Exception as e:
        return f"查询错误: {e}"


# ---------------------------------------------------------------------------
# 工具 2：export_query — 只读 + 写文件
# ---------------------------------------------------------------------------
@mcp.tool()
def export_query(
    sql: str,
    format: str = "csv",
    export_dir: Optional[str] = None,
    filename: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """执行 SELECT 并把全部结果导出为 csv / xlsx（无行数上限，写文件有副作用）。

    Args:
        sql: SELECT/WITH/EXPLAIN/SHOW/DESCRIBE 语句
        format: 'csv' 或 'xlsx'
        export_dir: 导出目录，默认当前工作目录
        filename: 自定义文件名（不含后缀），默认 `export_<时间戳>`
    """
    fmt = (format or "").lower()
    if fmt not in ("csv", "xlsx"):
        return f"错误：不支持的导出格式 '{format}'，仅支持 csv / xlsx"
    if not is_read_query(sql):
        return "错误：export_query 仅接受 SELECT/WITH/EXPLAIN/SHOW/DESCRIBE 等只读语句"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql.rstrip(";").strip())
                rows = cur.fetchall()
        data = format_rows(rows)
        columns = list(data[0].keys()) if data else []

        out_dir = Path(export_dir).expanduser() if export_dir else Path.cwd()
        out_dir.mkdir(parents=True, exist_ok=True)
        base = _safe_name(filename) if filename else f"export_{_now_tag()}"
        filepath = out_dir / f"{base}.{fmt}"

        df = pd.DataFrame(data, columns=columns if columns else None)
        if fmt == "csv":
            df.to_csv(filepath, index=False, encoding="utf-8-sig")
        else:
            df.to_excel(filepath, index=False, engine="openpyxl")

        return f"导出成功\n文件路径: {filepath}\n记录数: {len(data)}"
    except Exception as e:
        return f"导出错误: {e}"


# ---------------------------------------------------------------------------
# 工具 3：execute — 万能执行
# ---------------------------------------------------------------------------
def _execute_one(sql: str, confirm: bool) -> str:
    """执行单条 SQL（自动判别只读/写），返回结果字符串。"""
    cleaned = sql.strip()
    if is_read_query(cleaned):
        # 只读分支：直接执行并返回结果集
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(cleaned.rstrip(";").strip())
                rows = cur.fetchall()
        columns = list(rows[0].keys()) if rows else []
        data = format_rows(rows)
        return json.dumps(
            {"columns": columns, "rows": data, "returned": len(data)},
            ensure_ascii=False,
            default=str,
        )
    # 写操作分支
    if not confirm:
        return (
            "安全确认：这是一条写操作语句，请设置 confirm=true 以确认执行。\n"
            f"SQL: {cleaned[:200]}{'...' if len(cleaned) > 200 else ''}"
        )
    warning = ""
    if has_danger_keyword(cleaned):
        warning = "⚠️ 警告：检测到高危关键字（DROP/TRUNCATE/DELETE/UPDATE/ALTER/RENAME/GRANT/REVOKE），请确认操作符合预期。\n"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(cleaned)
            affected = cur.rowcount
        conn.commit()
    return f"{warning}执行成功: 受影响行数 {affected}"


@mcp.tool()
def execute(
    sql: Optional[str] = None,
    sql_file: Optional[str] = None,
    confirm: bool = False,
    ctx: Context = None,
) -> str:
    """万能执行 SQL。

    sql 与 sql_file 二选一：
      - sql: SQL 字符串（仅支持单条语句）
      - sql_file: .sql 脚本文件路径（支持多条语句，逐条执行）

    自动识别只读/写：
      - SELECT/WITH/EXPLAIN/SHOW/DESCRIBE/USE/HELP → 直接执行并返回 JSON
      - 其他（INSERT/UPDATE/DELETE/DDL 等）→ 写操作，必须 confirm=true 才执行

    脚本含写操作时须 confirm=true；任一条失败即中止，已完成的不回滚。
    """
    if sql is not None and str(sql).strip():
        script = str(sql)
        if contains_multi_statements(script):
            return "错误：sql 参数不支持多语句，请拆分为多次调用，或改用 sql_file 参数指向 .sql 文件。"
        stmts = [script.strip()]
    elif sql_file:
        try:
            stmts = split_sql_script(_read_sql_file(sql_file))
        except Exception as e:
            return f"错误: {e}"
    else:
        return "错误：请提供 sql 或 sql_file"
    if not stmts:
        return "错误：SQL 内容为空"

    if len(stmts) == 1:
        try:
            return _execute_one(stmts[0], confirm)
        except Exception as e:
            return f"执行错误: {e}"

    # 多条脚本（仅 sql_file 可到达）：含写操作需 confirm
    writes = [s for s in stmts if not is_read_query(s)]
    if writes and not confirm:
        preview = "\n".join(
            f"[{i}] {'⚠️写 ' if not is_read_query(s) else '只读 '}{s[:120]}{'…' if len(s) > 120 else ''}"
            for i, s in enumerate(stmts, 1)
        )
        return (
            f"安全确认：脚本含 {len(stmts)} 条语句，其中 {len(writes)} 条为写操作。\n"
            f"请设置 confirm=true 以逐条执行。\n{preview}"
        )

    # 逐条执行，汇总结果（含写操作但已 confirm=true，直接逐条执行）
    done = 0
    lines: list[str] = []
    try:
        for s in stmts:
            if is_read_query(s):
                lines.append(f"[{done + 1}] " + _execute_one(s, confirm=True))
            else:
                warning = (
                    "⚠️ 警告：检测到高危关键字（DROP/TRUNCATE/DELETE/UPDATE/ALTER/RENAME/GRANT/REVOKE），请确认操作符合预期。\n"
                    if has_danger_keyword(s)
                    else ""
                )
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(s)
                        affected = cur.rowcount
                    conn.commit()
                lines.append(f"[{done + 1}] {warning}执行成功: 受影响行数 {affected}")
            done += 1
        return f"脚本执行完成：共 {len(stmts)} 条，全部成功。\n" + "\n".join(lines)
    except Exception as e:
        return f"执行错误（第 {done + 1}/{len(stmts)} 条失败，已完成 {done} 条）: {e}"


# ---------------------------------------------------------------------------
# 工具 4：describe_table — 完整表结构
# ---------------------------------------------------------------------------
@mcp.tool()
def describe_table(table: str, database: Optional[str] = None, ctx: Context = None) -> str:
    """输出表的完整结构：字段名、类型、可空、默认值、键、字符集、校对、注释。

    Args:
        table: 表名
        database: 库名；缺省使用默认库 MYSQL_DATABASE
    """
    try:
        target = database or MYSQL_DATABASE
        if not target:
            return "错误：未指定 database，且未配置默认 MYSQL_DATABASE"
        with get_connection(database=target) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COLUMN_NAME AS name, COLUMN_TYPE AS type, DATA_TYPE AS data_type, "
                    "IS_NULLABLE AS nullable, COLUMN_DEFAULT AS default_value, "
                    "COLUMN_KEY AS column_key, EXTRA AS extra, COLUMN_COMMENT AS comment, "
                    "CHARACTER_SET_NAME AS charset, COLLATION_NAME AS collation "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    "ORDER BY ORDINAL_POSITION",
                    (target, table),
                )
                rows = cur.fetchall()
        if not rows:
            return json.dumps(
                {"database": target, "table": table, "columns": [], "warning": "未找到列定义（库/表不存在？）"},
                ensure_ascii=False,
                indent=2,
            )
        out = [
            {
                "name": r["name"],
                "type": r["type"],
                "data_type": r["data_type"],
                "nullable": r["nullable"] == "YES",
                "default": r["default_value"],
                "key": r["column_key"],
                "extra": r["extra"],
                "comment": r["comment"],
                "charset": r["charset"],
                "collation": r["collation"],
            }
            for r in rows
        ]
        return json.dumps(
            {"database": target, "table": table, "columns": out},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        return f"错误: {e}"


# ---------------------------------------------------------------------------
# 工具 5（已移除）：sample_rows —— 取消原因详见 README "工具取舍"
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 资源（连接信息 / 库 / 表）
# ---------------------------------------------------------------------------
@mcp.resource("mysql://connection")
def get_connection_info() -> str:
    """当前连接信息（不含密码）。"""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION() AS v")
                version = cur.fetchone()["v"]
                cur.execute("SELECT DATABASE() AS d")
                db = cur.fetchone()["d"]
                cur.execute("SELECT CURRENT_USER() AS u")
                user = cur.fetchone()["u"]
        info = {
            "host": MYSQL_HOST,
            "port": MYSQL_PORT,
            "user": MYSQL_USER,
            "database": db,
            "charset": MYSQL_CHARSET,
            "version": version,
            "auth_user": user,
        }
        return json.dumps(info, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.resource("mysql://databases")
def get_databases() -> str:
    try:
        with get_connection(database=None) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                    "WHERE SCHEMA_NAME NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys') "
                    "ORDER BY SCHEMA_NAME"
                )
                rows = cur.fetchall()
        return json.dumps([r["SCHEMA_NAME"] for r in rows], ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.resource("mysql://tables")
def get_tables() -> str:
    """当前默认库下的表名列表。"""
    if not MYSQL_DATABASE:
        return json.dumps({"error": "未配置默认 MYSQL_DATABASE"}, ensure_ascii=False)
    try:
        with get_connection(database=MYSQL_DATABASE) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT TABLE_NAME AS name, TABLE_TYPE AS type FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
                    (MYSQL_DATABASE,),
                )
                rows = cur.fetchall()
        return json.dumps(rows, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    _check_required_env()
    mcp.run(transport="stdio")