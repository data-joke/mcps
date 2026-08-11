#!/usr/bin/env python3
"""
ODPS MCP Server - 阿里云 MaxCompute 数据查询与导出工具

提供两个核心工具：
- odps_query_data: 执行 SQL 查询，数据直接返回给大模型（适用于数据分析场景）
- odps_export_data: 执行 SQL 查询，导出为 xlsx/csv 文件（适用于数据交付场景）
"""

import sys
import os

import json
import re
import traceback
from typing import Optional, Literal, Any
from pathlib import Path
from datetime import datetime
from odps import ODPS
from odps import errors as odps_errors

from pydantic import BaseModel, Field, ConfigDict, field_validator
from mcp.server.fastmcp import FastMCP

# ============================================================
# 预加载 pyodps，避免首次调用时卡住
# ============================================================

# ============================================================
# ODPS 连接管理
# ============================================================

def get_odps_config():
    """从环境变量获取 ODPS 连接配置。

    4 个环境变量**全部必填**，无任何默认值兜底：
        ODPS_ACCESS_ID:   阿里云 AccessKey ID
        ODPS_ACCESS_KEY:  阿里云 AccessKey Secret
        ODPS_PROJECT:     MaxCompute 项目名
        ODPS_ENDPOINT:    MaxCompute 服务端点

    任一未设置立即抛错，避免隐式回退到错误配置。
    """
    access_id = os.getenv("ODPS_ACCESS_ID")
    secret_key = os.getenv("ODPS_ACCESS_KEY")
    project = os.getenv("ODPS_PROJECT")
    endpoint = os.getenv("ODPS_ENDPOINT")

    missing = []
    if not access_id:
        missing.append("ODPS_ACCESS_ID")
    if not secret_key:
        missing.append("ODPS_ACCESS_KEY")
    if not project:
        missing.append("ODPS_PROJECT")
    if not endpoint:
        missing.append("ODPS_ENDPOINT")
    if missing:
        raise ValueError(
            f"以下环境变量未设置: {', '.join(missing)}。"
            "请在 MCP 客户端配置中补齐后再启动。"
        )

    return access_id, secret_key, project, endpoint


def get_odps_connection():
    """创建 ODPS 连接"""

    access_id, secret_key, project, endpoint = get_odps_config()

    return ODPS(
        access_id=access_id,
        secret_access_key=secret_key,
        project=project,
        endpoint=endpoint,
    )


def execute_sql_to_dataframe(sql: str):
    """执行 SQL 并返回 pandas DataFrame。

    Args:
        sql: 要执行的 SQL 语句

    Returns:
        pandas.DataFrame: 查询结果
    """
    print("[DEBUG] execute_sql_to_dataframe 开始...", file=sys.stderr, flush=True)
    import pandas as pd

    print("[DEBUG] 正在获取 ODPS 连接...", file=sys.stderr, flush=True)
    o = get_odps_connection()
    print("[DEBUG] ODPS 连接获取成功，准备执行 SQL...", file=sys.stderr, flush=True)

    print("[DEBUG] 开始执行 SQL 查询...", file=sys.stderr, flush=True)
    instance = o.execute_sql(sql)
    print("[DEBUG] SQL 执行中，等待结果...", file=sys.stderr, flush=True)

    with instance.open_reader(tunnel=True) as reader:
        print("[DEBUG] 正在读取数据...", file=sys.stderr, flush=True)
        df = reader.to_pandas(n_process=1)

    print(f"[DEBUG] 数据读取完成，共 {len(df)} 行", file=sys.stderr, flush=True)
    return df


def dataframe_to_markdown(df, max_rows: int = 200) -> str:
    """将 DataFrame 转换为 Markdown 表格，带汇总信息。

    Args:
        df: pandas DataFrame
        max_rows: 最大展示行数

    Returns:
        str: Markdown 格式的表格文本
    """
    total_rows = len(df)
    total_cols = len(df.columns)
    truncated = total_rows > max_rows

    lines = []

    # 汇总信息
    lines.append(f"📊 **查询结果**: 共 {total_rows:,} 行 × {total_cols} 列")
    if truncated:
        lines.append(f"⚠️ 数据量较大，当前仅展示前 {max_rows} 行。如需完整数据请使用 `odps_export_data` 导出。")
    lines.append("")

    # 截断
    display_df = df.head(max_rows) if truncated else df

    # 构建表格
    columns = list(display_df.columns)
    lines.append("| " + " | ".join(str(c) for c in columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")

    for row in display_df.itertuples(index=False):
        cells = []
        for val in row:
            if val is None or str(val) == "nan" or str(val) == "NaT":
                cells.append("")
            else:
                cells.append(str(val))
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


def export_dataframe(df, file_path: str, file_type: str) -> str:
    """将 DataFrame 导出到文件。

    Args:
        df: pandas DataFrame
        file_path: 文件保存路径
        file_type: 文件类型 (xlsx/csv)

    Returns:
        str: 保存的文件绝对路径
    """
    file_path = os.path.abspath(file_path)

    # 确保目录存在
    dir_name = os.path.dirname(file_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    if file_type == "xlsx":
        df.to_excel(file_path, index=False)
    elif file_type == "csv":
        df.to_csv(file_path, index=False, encoding="utf-8-sig")
    else:
        raise ValueError(f"不支持的文件类型: {file_type}，仅支持 xlsx 和 csv")

    return file_path


def fetch_table_metadata_dict(table_name: str) -> dict:
    """获取指定表的完整元数据，返回结构化字典（同步函数，由 to_thread 包裹）。

    包含：表注释、生命周期、是否分区、字段列表、分区字段列表、
    创建时间、最近修改时间。所有时间字段格式化为 ISO 字符串。
    表不存在时抛出 odps.errors.NoSuchTable。
    """
    o = get_odps_connection()
    table = o.get_table(table_name)
    schema = table.table_schema
    partitions = schema.partitions or []
    # ODPS 用 lifecycle = -1 表示"无生命周期"（不设置）
    lifecycle = table.lifecycle
    lifecycle = lifecycle if (lifecycle is not None and lifecycle >= 0) else None
    return {
        "project": o.project,
        "name": table.name,
        "comment": table.comment or "",
        "lifecycle": lifecycle,
        "is_partitioned": bool(partitions),
        "owner": getattr(table, "owner", "") or "",
        "creation_time": table.creation_time.isoformat() if getattr(table, "creation_time", None) else "",
        "last_modified_time": table.last_modified_time.isoformat() if getattr(table, "last_modified_time", None) else "",
        "columns": [
            {"name": c.name, "type": str(c.type), "comment": c.comment or ""}
            for c in schema.columns
        ],
        "partitions": [
            {"name": p.name, "type": str(p.type), "comment": p.comment or ""}
            for p in partitions
        ],
    }


def format_table_metadata_markdown(meta: dict) -> str:
    """将元数据字典渲染为 Markdown，包含表头信息、字段表、分区表三段。"""
    lines = []
    lines.append(f"📋 **表元数据**: `{meta['project']}.{meta['name']}`")
    lines.append(f"- 注释: {meta['comment'] or '(无注释)'}")
    lines.append(f"- 是否分区表: {'是' if meta['is_partitioned'] else '否'}")
    if meta["lifecycle"] is not None:
        lines.append(f"- 生命周期: {meta['lifecycle']} 天")
    else:
        lines.append("- 生命周期: 未设置")
    if meta["owner"]:
        lines.append(f"- Owner: {meta['owner']}")
    if meta["creation_time"]:
        lines.append(f"- 创建时间: {meta['creation_time']}")
    if meta["last_modified_time"]:
        lines.append(f"- 最近修改: {meta['last_modified_time']}")
    lines.append("")

    # 字段表
    lines.append(f"### 字段（共 {len(meta['columns'])} 个）")
    lines.append("| # | 字段名 | 类型 | 注释 |")
    lines.append("|---|--------|------|------|")
    for i, c in enumerate(meta["columns"], 1):
        lines.append(f"| {i} | `{c['name']}` | {c['type']} | {c['comment'] or '(无注释)'} |")
    lines.append("")

    # 分区表
    if meta["is_partitioned"]:
        lines.append(f"### 分区字段（共 {len(meta['partitions'])} 个）")
        if meta["partitions"]:
            lines.append("| # | 分区名 | 类型 | 注释 |")
            lines.append("|---|--------|------|------|")
            for i, p in enumerate(meta["partitions"], 1):
                lines.append(f"| {i} | `{p['name']}` | {p['type']} | {p['comment'] or '(无注释)'} |")
        else:
            lines.append("（该表标记为分区表，但 schema 中未声明分区字段）")
    else:
        lines.append("### 分区字段")
        lines.append("（该表为非分区表）")
    return "\n".join(lines)


def list_tables_paginated(name_pattern: str, offset: int, limit: int) -> tuple:
    """分页列出项目下的表，支持通配符过滤（同步函数，由 to_thread 包裹）。

    返回 (rows, total):
        rows: [(name, owner, is_partitioned, lifecycle, comment), ...]
        total: 匹配 name_pattern 后的总表数

    通配符语义：使用 fnmatch 客户端过滤，支持 * 和 ?。
    单张表元数据拉取失败时仍展示表名，避免列表被一条错误抹掉。
    """
    import fnmatch
    o = get_odps_connection()
    project = o.project

    # 1) 拉全量表名（项目级 list；pyodps 不支持服务端通配符，先取全量再客户端过滤）
    raw_names = [t.name for t in o.list_tables()]
    total_raw = len(raw_names)

    # 2) 客户端按通配符过滤
    if name_pattern:
        pattern = name_pattern.strip()
        raw_names = [n for n in raw_names if fnmatch.fnmatchcase(n, pattern)]
    total = len(raw_names)

    # 3) 分页切片
    page_names = raw_names[offset: offset + limit]

    # 4) 拉每张表的概要信息（owner/是否分区/lifecycle/注释）
    rows = []
    for n in page_names:
        try:
            t = o.get_table(n)
            schema = t.table_schema
            partitions = schema.partitions or []
            lc = t.lifecycle
            lc_str = "-" if (lc is None or lc < 0) else f"{lc}"
            rows.append((
                t.name,
                getattr(t, "owner", "") or "-",
                "是" if partitions else "否",
                lc_str,
                (t.comment or "").replace("\n", " ").replace("\r", " ")[:80] or "(无注释)",
            ))
        except Exception as e:
            # 单条失败不中断列表，仅记录
            print(f"[WARN] 拉取 {n} 详情失败: {e}", file=sys.stderr, flush=True)
            rows.append((n, "-", "-", "-", "(拉取失败)"))
    return rows, total, total_raw


# ============================================================
# 输入模型定义
# ============================================================

class QueryDataInput(BaseModel):
    """SQL 查询输入模型 - 数据直返大模型"""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    sql: str = Field(
        ...,
        description="要执行的 ODPS SQL 语句",
        min_length=1,
    )
    max_rows: int = Field(
        default=200,
        description="返回给大模型的最大行数，避免上下文溢出。默认 200 行",
        ge=1,
        le=1000,
    )

    @field_validator("sql")
    @classmethod
    def validate_sql(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("SQL 语句不能为空")
        # 移除末尾分号（ODPS 不需要）
        if v.endswith(";"):
            v = v[:-1].strip()
        return v


class ExportDataInput(BaseModel):
    """SQL 导出输入模型 - 数据导出为文件"""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    sql: str = Field(
        ...,
        description="要执行的 ODPS SQL 语句",
        min_length=1,
    )
    file_type: str = Field(
        default="xlsx",
        description="导出文件类型：xlsx 或 csv",
    )
    save_path: str = Field(
        ...,
        description="导出文件的完整路径（含文件名），例如 D:/output/20260328_数据查询.xlsx",
        min_length=1,
    )

    @field_validator("sql")
    @classmethod
    def validate_sql(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("SQL 语句不能为空")
        if v.endswith(";"):
            v = v[:-1].strip()
        return v

    @field_validator("file_type")
    @classmethod
    def validate_file_type(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("xlsx", "csv"):
            raise ValueError("file_type 仅支持 xlsx 或 csv")
        return v


class GetTableMetadataInput(BaseModel):
    """获取单张表元数据 - 字段/分区/注释/生命周期"""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    table_name: str = Field(
        ...,
        description="表名（不含项目前缀），例如 user_behavior_dwd",
        min_length=1,
    )


class ListTablesInput(BaseModel):
    """列出项目下的表 - 元数据发现入口（支持通配符模糊搜索）"""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    name_pattern: str = Field(
        default="",
        description="表名通配符，支持 * 和 ?，例如 user_* 或 *_dwd。留空列出全部",
    )
    limit: int = Field(
        default=100,
        description="返回的最大表数量，默认 100，最大 1000",
        ge=1,
        le=1000,
    )
    offset: int = Field(
        default=0,
        description="分页偏移量，从 0 开始",
        ge=0,
    )


# ============================================================
# MCP Server 定义
# ============================================================

mcp = FastMCP(
    "odps_mcp",
    instructions=(
        "阿里云 MaxCompute (ODPS) MCP Server。\n"
        "提示：SQL 较长（如 >500 字符）或需执行多语句脚本时，优先用 sql_file 参数指向 .sql 文件，"
        "避免把超长 SQL 塞进提示词导致截断/异常。"
    ),
)

@mcp.tool(
    name="odps_query_data",
    annotations={
        "title": "ODPS 数据查询（返回 Markdown 表格）",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def odps_query_data(
    sql: Optional[str] = None,
    sql_file: Optional[str] = None,
    max_rows: int = 200,
) -> str:
    """执行 ODPS SQL 查询，将结果以 Markdown 表格形式直接返回给大模型。

    适用于数据分析、快速验证、指标查看等场景。查询结果不会保存为文件，
    而是直接作为文本返回，供大模型进行后续分析和解读。

    sql 与 sql_file 二选一：
      - sql: SQL 字符串（仅支持单条语句）
      - sql_file: .sql 脚本文件路径（长 SQL 或多条只读语句；脚本逐条执行并汇总）

    注意：
    - 查询结果默认限制 200 行，可通过 max_rows 参数调整（最大 1000 行）
    - 超出限制时会提示用户使用 odps_export_data 导出完整数据
    - 执行耗时取决于 SQL 复杂度和数据量，通常几秒到几分钟不等

    Args:
        sql (str): 要执行的 ODPS SQL 语句
        sql_file (str): .sql 脚本文件路径
        max_rows (int): 最大返回行数，默认 200，最大 1000

    Returns:
        str: Markdown 表格格式的查询结果，附带汇总信息（总行数、总列数）
    """
    import sys
    import asyncio

    print("[DEBUG] odps_query_data 被调用，开始执行...", file=sys.stderr, flush=True)
    try:
        # 参数校验
        if sql is not None and str(sql).strip():
            stmts = split_sql_script(str(sql))
            if len(stmts) > 1:
                return "❌ sql 参数仅支持单条语句（检测到多条）。长 SQL 或多语句脚本请用 sql_file 参数。"
        elif sql_file:
            stmts = split_sql_script(_read_sql_file(sql_file))
        else:
            return "❌ 请提供 sql 或 sql_file"
        stmts = [s.rstrip(";").strip() for s in stmts if s.strip()]
        if not stmts:
            return "❌ SQL 内容为空"
        if max_rows < 1:
            max_rows = 200
        if max_rows > 1000:
            max_rows = 1000

        outputs: list[str] = []
        for i, stmt in enumerate(stmts, 1):
            if _extract_first_keyword(stmt) != "SELECT":
                return f"❌ odps_query_data 仅支持 SELECT 语句（第 {i} 条首词为 {_extract_first_keyword(stmt) or '(无法识别)'}），DDL/DML 请用 odps_execute_sql。"
            print(f"[DEBUG] 准备执行 SQL({i}/{len(stmts)}): {stmt[:100]}...", file=sys.stderr, flush=True)
            # ODPS 操作是同步的，放到线程池中执行避免阻塞事件循环
            df = await asyncio.to_thread(execute_sql_to_dataframe, stmt)
            print(f"[DEBUG] SQL({i}) 执行完成，获取到 {len(df)} 行数据", file=sys.stderr, flush=True)

            if df.empty:
                outputs.append(f"### 语句 {i}/{len(stmts)}\n\n查询结果为空（0 行数据）。请检查 SQL 条件是否正确。\n")
                continue
            md = dataframe_to_markdown(df, max_rows=max_rows)
            outputs.append((f"### 语句 {i}/{len(stmts)}\n\n" + md) if len(stmts) > 1 else md)

        return "\n\n".join(outputs)

    except ValueError as e:
        return f"❌ 配置错误: {str(e)}"
    except Exception as e:
        return f"❌ SQL 执行失败: {type(e).__name__}: {str(e)}\n请检查 SQL 语法或网络连接。"


@mcp.tool(
    name="odps_export_data",
    annotations={
        "title": "ODPS 数据导出（保存为文件）",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def odps_export_data(sql: str, file_type: str = "xlsx", save_path: str = "") -> str:
    """执行 ODPS SQL 查询，并将结果导出为 xlsx 或 csv 文件。

    适用于数据交付、周报导出、批量数据获取等需要保存文件的场景。
    导出无行数限制，会保存完整查询结果。

    注意：
    - 文件会保存到 save_path 指定的路径，目录不存在会自动创建
    - 执行耗时取决于 SQL 复杂度和数据量，通常几分钟到十几分钟不等

    Args:
        sql (str): 要执行的 ODPS SQL 语句
        file_type (str): 导出格式，xlsx 或 csv，默认 xlsx
        save_path (str): 文件保存的完整路径（含文件名）

    Returns:
        str: 导出结果信息，包含文件路径和记录数
    """
    import sys
    try:
        import asyncio

        # 参数校验
        sql = sql.strip()
        if not sql:
            return "❌ SQL 语句不能为空"
        if sql.endswith(";"):
            sql = sql[:-1].strip()
        file_type = file_type.lower().strip()
        if file_type not in ("xlsx", "csv"):
            return "❌ file_type 仅支持 xlsx 或 csv"
        if not save_path:
            return "❌ save_path 不能为空"

        # 执行 SQL
        df = await asyncio.to_thread(execute_sql_to_dataframe, sql)

        if df.empty:
            return "查询结果为空（0 行数据），未生成文件。请检查 SQL 条件是否正确。"

        # 导出文件
        saved_path = await asyncio.to_thread(
            export_dataframe, df, save_path, file_type
        )

        return (
            f"✅ 数据导出成功\n"
            f"- 文件路径: {saved_path}\n"
            f"- 记录数: {len(df):,} 行 × {len(df.columns)} 列\n"
            f"- 文件格式: {file_type}"
        )

    except ValueError as e:
        return f"❌ 参数错误: {str(e)}"
    except Exception as e:
        return f"❌ 导出失败: {type(e).__name__}: {str(e)}\n请检查 SQL 语法、文件路径权限或网络连接。"


@mcp.tool(
    name="odps_get_table_metadata",
    annotations={
        "title": "ODPS 获取表元数据",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def odps_get_table_metadata(table_name: str) -> str:
    """获取 ODPS 单张表的完整元数据，包括字段、分区、注释、生命周期等。

    适用于：写 SQL 前确认表结构、判断字段类型/注释、判断是否分区表、
    了解数据生命周期。在 WHERE 条件、分组键、JOIN 键等场景下，
    调用此工具确认字段名和类型可以避免大量试错 SQL。

    Args:
        table_name (str): 表名（不含项目前缀），例如 user_behavior_dwd

    Returns:
        str: Markdown 格式的表元数据，包含表头信息、字段表、分区表三段
    """
    import asyncio
    from odps.errors import NoSuchObject
    try:
        table_name = table_name.strip()
        if not table_name:
            return "❌ table_name 不能为空"

        meta = await asyncio.to_thread(fetch_table_metadata_dict, table_name)
        return format_table_metadata_markdown(meta)

    except NoSuchObject:
        o = get_odps_connection()
        return f"❌ 表不存在: `{o.project}.{table_name}`。请检查表名拼写"
    except ValueError as e:
        return f"❌ 配置错误: {str(e)}"
    except Exception as e:
        return f"❌ 获取表元数据失败: {type(e).__name__}: {str(e)}"


@mcp.tool(
    name="odps_list_tables",
    annotations={
        "title": "ODPS 列出项目下的表（支持通配符模糊搜索）",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def odps_list_tables(
    name_pattern: str = "",
    limit: int = 100,
    offset: int = 0,
) -> str:
    """列出 ODPS 项目下的表，支持表名通配符过滤和分页。

    适用于：在写 SQL 前先了解项目里有哪些表、查找特定前缀/后缀的表、
    确认表是否存在。该工具不会拉取每个表的完整字段（避免在表多时耗尽 token），
    仅展示表名、owner、是否分区、生命周期、注释摘要。
    获取字段详情请配合 odps_get_table_metadata 使用。

    通配符语法（fnmatch 风格）：
        *  匹配任意数量字符（含 0 个）
        ?  匹配单个字符
        示例：user_*、*_dwd、ads_lyy_*_di、user_???

    Args:
        name_pattern (str): 表名通配符，例如 user_* 或 *_dwd；留空列出全部
        limit (int): 返回的最大表数量，默认 100，最大 1000
        offset (int): 分页偏移量，默认 0

    Returns:
        str: Markdown 格式的表清单，含匹配总数、当前分页、是否有更多
    """
    import asyncio
    try:
        # 参数校验（与 Pydantic schema 保持一致的双重防线）
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        name_pattern = name_pattern.strip()

        rows, total, total_raw = await asyncio.to_thread(
            list_tables_paginated, name_pattern, offset, limit
        )

        o = get_odps_connection()
        project = o.project

        lines = []
        title = f"📋 **表清单**: 项目 `{project}`"
        if name_pattern:
            title += f"，匹配模式 `{name_pattern}`"
        lines.append(title)

        if not name_pattern:
            lines.append(f"📊 项目下共 {total_raw} 张表")
        else:
            lines.append(f"📊 匹配 {total} 张 / 项目下共 {total_raw} 张")

        if rows:
            start = offset + 1
            end = offset + len(rows)
            lines.append(f"本次返回第 {start} - {end} 张")
            if end < total:
                lines.append(
                    f"⚠️ 还有 {total - end} 张未展示，请增大 offset 翻页，或收紧 name_pattern 缩小范围"
                )
        else:
            lines.append("")
            lines.append("（无匹配表）")
            return "\n".join(lines)

        lines.append("")
        lines.append("| # | 表名 | Owner | 是否分区 | 生命周期(天) | 注释 |")
        lines.append("|---|------|-------|----------|--------------|------|")
        for i, (name, owner, is_part, lc, comment) in enumerate(rows, offset + 1):
            lines.append(
                f"| {i} | `{name}` | {owner} | {is_part} | {lc} | {comment} |"
            )
        return "\n".join(lines)

    except ValueError as e:
        return f"❌ 参数错误: {str(e)}"
    except Exception as e:
        return f"❌ 列出表失败: {type(e).__name__}: {str(e)}"


# ============================================================
# 横向辅助函数（新增工具通用）
# ============================================================


def _strip_sql_comments(sql: str) -> str:
    """剥掉 SQL 注释（单行 -- 和多行 /* */），返回纯净 SQL。

    避免注释里的关键字（如注释里出现 'drop'）干扰护栏判断。
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


# ============================================================
# 从 .sql 脚本文件读取（避免超长 SQL 塞进提示词）
# ============================================================
MAX_SQL_FILE_SIZE = 5 * 1024 * 1024


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

    用『等长空格掩码』替换字符串与注释内容（单引号/双引号/反引号字符串、
    `--` 行注释、`/* */` 块注释），保证这些区域内的分号不参与切分，
    且切分位置能映射回原文。不处理存储过程/DELIMITER。
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


def _extract_first_keyword(sql: str) -> str:
    """从 SQL 中提取第一个真正执行的 DDL/DML/管理关键字。

    自动剥注释后查找，覆盖:
    DDL: CREATE/DROP/ALTER/TRUNCATE/RENAME
    DML: SELECT/INSERT/UPDATE/DELETE
    管理: DESC/DESCRIBE/SHOW/USE/SET/EXPLAIN/KILL

    返回大写关键字；找不到返回空串。
    """
    sql_clean = _strip_sql_comments(sql).strip()
    m = re.search(
        r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|TRUNCATE|ALTER|RENAME"
        r"|DESC|DESCRIBE|SHOW|USE|SET|EXPLAIN|KILL)\b",
        sql_clean,
        re.IGNORECASE,
    )
    return m.group(1).upper() if m else ""


def _extract_drop_table_name(sql: str) -> Optional[str]:
    """从 DROP TABLE 语句中提取表名。

    自动:
    - 剥注释
    - 处理 DROP TABLE / DROP TABLE IF EXISTS 两种语法
    - 处理 project.table 形式（去掉 project 前缀，只保留表名）
    - 处理反引号包裹

    返回纯表名（无项目前缀）；非 DROP TABLE 语句返回 None。
    """
    sql_clean = _strip_sql_comments(sql).strip()
    m = re.search(
        r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?"
        r"`?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`?",
        sql_clean,
        re.IGNORECASE,
    )
    if not m:
        return None
    name = m.group(1)
    if "." in name:
        name = name.split(".")[-1]
    return name


def _classify_sql(sql: str) -> tuple:
    """按 SQL 首词分类，与 selectdb_execute 风格一致。

    使用 _extract_first_keyword 自动剥注释，处理带注释的 SQL。
    """
    first_word = _extract_first_keyword(sql)
    if first_word in ("INSERT", "UPDATE", "DELETE"):
        return "dml", f"{first_word} 语句"
    elif first_word in ("CREATE", "DROP", "ALTER", "TRUNCATE", "RENAME"):
        return "ddl", f"{first_word} DDL 语句"
    elif first_word in ("DESC", "DESCRIBE", "SHOW", "USE", "SET", "EXPLAIN", "KILL"):
        return "admin", f"{first_word} 管理语句"
    elif not first_word:
        return "unknown", "未知类型语句（空 SQL 或无法解析）"
    else:
        return "unknown", f"未知类型语句（{first_word}）"


def _fetch_explain_plan(instance) -> str:
    """从 EXPLAIN 实例中读取执行计划文本。

    通过 Instance.get_task_results_without_format() 拿到 task 的 raw Result，
    返回完整执行计划字符串（多行）。

    注意：不能用 open_reader(tunnel=True)，因为 EXPLAIN 实例不是 SELECT，
    Tunnel 端点会拒绝（InstanceTypeNotSupported）。
    """
    results = instance.get_task_results_without_format()
    if not results:
        return ""
    # 多 task 时拼起来，正常只有 1 个 AnonymousSQLTask
    parts = []
    for task_name, raw in results.items():
        if raw is None:
            continue
        parts.append(str(raw))
    return "\n".join(parts).strip()


def _format_col(col_dict: dict) -> str:
    """将 {name, type, comment} 格式化为 `name` TYPE COMMENT 'comment' 字符串。

    注释自动转义单引号。
    """
    name = col_dict.get("name", "").strip()
    typ = col_dict.get("type", "").strip()
    comment = (col_dict.get("comment") or "").replace("'", "''")
    if not name or not typ:
        raise ValueError(f"列定义缺少 name 或 type: {col_dict}")
    parts = [f"`{name}`", typ]
    if comment:
        parts.append(f"COMMENT '{comment}'")
    return " ".join(parts)


def _infer_odps_type_from_series(s: "pd.Series") -> str:
    """根据 pandas Series 推断 ODPS 类型。

    推断规则（按优先级）：
    - 全部为整数（无 NaN）→ BIGINT
    - 全部为浮点（无损可转 int）→ BIGINT
    - 全部为浮点（有小数）→ DOUBLE
    - 全部为日期时间 → DATETIME
    - 全部为布尔 → BOOLEAN
    - 其他（含混合类型）→ STRING
    """
    non_null = s.dropna()
    if non_null.empty:
        return "STRING"

    # 整数
    if pd.api.types.is_integer_dtype(non_null):
        return "BIGINT"
    # 布尔
    if pd.api.types.is_bool_dtype(non_null):
        return "BOOLEAN"
    # 日期时间
    if pd.api.types.is_datetime64_any_dtype(non_null):
        return "DATETIME"
    # 浮点
    if pd.api.types.is_float_dtype(non_null):
        # 进一步看是否其实是整数（"1.0" 这种 pandas 会读成 float）
        try:
            # 用 Int64（pandas 可空整型）做无损转换判断
            if (non_null.astype("Int64") == non_null).all():
                return "BIGINT"
        except Exception:
            pass
        return "DOUBLE"
    # 其他（object / string / 混合）→ STRING
    return "STRING"


def _build_create_table_sql(
    name: str,
    columns: list,
    lifecycle: Optional[int] = None,
    comment: Optional[str] = None,
    if_not_exists: bool = True,
) -> str:
    """拼 CREATE TABLE DDL（不含 PARTITIONED BY——本工具创建的表都是非分区表）。"""
    parts = ["CREATE TABLE"]
    if if_not_exists:
        parts.append("IF NOT EXISTS")
    parts.append(f"`{name}`")
    if comment:
        parts.append(f"COMMENT '{comment.replace(chr(39), chr(39) * 2)}'")
    col_defs = [_format_col(c) for c in columns]
    parts.append("(" + ",\n  ".join(col_defs) + ")")
    if lifecycle:
        parts.append(f"LIFECYCLE {lifecycle}")
    return " ".join(parts)


def _read_file_smart(
    file_path: str,
    sheet_name: Optional[str] = None,
    header: Optional[int] = 0,
    columns: Optional[list] = None,
    usecols: Optional[list] = None,
) -> "pd.DataFrame":
    """智能读取 txt/csv/xlsx 文件。

    txt/csv 走 csv.Sniffer 推断分隔符 + pd.read_csv(engine='c')。
    xlsx 走 pd.read_excel，按 sheet_name 取工作表。

    自动尝试多种编码：utf-8-sig / utf-8 / gbk / gb18030。

    注意：避免使用 pd.read_csv(sep=None, engine='python')，
    它在单列数据上会按字符位置误切（如 D1001 → D | 00 | 1）。

    Args:
        file_path: 文件路径
        sheet_name: xlsx 工作表名
        header: 表头行号（0-based）。0=第 1 行（默认），None=无表头
        columns: 手动指定列名（优先级最高，覆盖文件表头）
        usecols: 只读指定列（按列名或列索引）。None=读全部
    """
    import csv as _csv

    file_path = str(file_path)
    lower = file_path.lower()

    if lower.endswith((".xlsx", ".xls")):
        df = pd.read_excel(file_path, sheet_name=sheet_name or 0, header=header, usecols=usecols)
    else:
        # txt/csv：用 csv.Sniffer 推断分隔符（更稳）+ engine='c'（避免误切）
        last_err: Optional[Exception] = None
        df = None
        for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
            try:
                with open(file_path, "r", encoding=enc, newline="") as f:
                    sample = f.read(8192)

                # Sniffer 推断分隔符；推断失败则用 ','
                try:
                    dialect = _csv.Sniffer().sniff(sample, delimiters=",\t;|")
                    sep = dialect.delimiter
                except _csv.Error:
                    sep = ","

                df = pd.read_csv(
                    file_path,
                    sep=sep,
                    engine="c",
                    encoding=enc,
                    header=header,
                    usecols=usecols,
                )
                break
            except UnicodeDecodeError as e:
                last_err = e
                continue
            except Exception as e:
                last_err = e
                continue
        if df is None:
            raise ValueError(f"无法读取文件（编码/分隔符均失败）: {file_path}; 最后错误: {last_err}")

    # columns 优先级最高：覆盖文件表头或自动生成的列名
    if columns is not None:
        cols_list = list(columns)
        if len(cols_list) != len(df.columns):
            raise ValueError(
                f"columns 数量 ({len(cols_list)}) 与文件数据列数 ({len(df.columns)}) 不一致。"
                f"请检查文件或调整 columns 参数。"
            )
        df.columns = cols_list

    return df


def _ensure_table_for_df(o, table_name: str, df: "pd.DataFrame", lifecycle: Optional[int] = None) -> bool:
    """如果表不存在，根据 df 推断 schema 自动建表。

    Returns:
        bool: True 表示新建了表，False 表示表已存在。
    """
    if o.exist_table(table_name):
        return False
    columns = []
    for col in df.columns:
        inferred = _infer_odps_type_from_series(df[col])
        columns.append({"name": str(col), "type": inferred, "comment": ""})
    sql = _build_create_table_sql(table_name, columns, lifecycle=lifecycle)
    print(f"[DEBUG] 自动建表 SQL: {sql}", file=sys.stderr, flush=True)
    o.execute_sql(sql)
    return True


def _validate_columns_match(t, df: "pd.DataFrame") -> None:
    """严格校验 DataFrame 列与表 schema 一致（缺/多都抛错）。

    注意：分区字段不参与校验——分区值由 partition 参数提供，不由 CSV 携带。
    """
    schema_cols = [c.name for c in t.table_schema.columns]
    partition_cols = {p.name for p in (t.table_schema.partitions or [])}
    data_cols = [c for c in schema_cols if c not in partition_cols]
    df_cols = [str(c) for c in df.columns]
    missing = set(data_cols) - set(df_cols)
    extra = set(df_cols) - set(data_cols)
    errs = []
    if missing:
        errs.append(f"缺少字段: {sorted(missing)}")
    if extra:
        errs.append(f"多余字段: {sorted(extra)}")
    if errs:
        raise ValueError("DataFrame 列与表 schema 不匹配：" + "；".join(errs))


def _df_to_records(df: "pd.DataFrame") -> list:
    """把 DataFrame 转成 pyodps Tunnel writer 兼容的 records。

    关键转换：把 numpy scalar 转成 Python 原生类型（用 .item()），
    否则 ODPS Tunnel writer 会抛 "Unsupported record type"。

    转换规则：
    - numpy.int64 / numpy.int32 → int
    - numpy.float64 / numpy.float32 → float
    - numpy.bool_ → bool
    - numpy.str_ → str
    - pandas NaN / NaT → None
    - Python str / datetime 等保持原样

    参考：
    - pyodps Table.write 文档要求 records 是 Python 原生类型
    - numpy.generic.item() 返回 Python 原生标量
    """
    records = []
    for tup in df.itertuples(index=False, name=None):
        converted = []
        for v in tup:
            if v is None:
                converted.append(None)
            elif isinstance(v, float) and pd.isna(v):
                converted.append(None)
            elif hasattr(v, "item"):  # numpy scalar
                try:
                    converted.append(v.item())
                except (ValueError, OverflowError):
                    converted.append(None)
            else:
                converted.append(v)
        records.append(tuple(converted))
    return records


def _upload_df_to_table(
    o,
    table_name: str,
    df: "pd.DataFrame",
    partition: Optional[str] = None,
    mode: str = "append",
) -> None:
    """把 DataFrame 写入 ODPS 表（覆盖或追加）。

    - NaN → NULL
    - 列按 schema 顺序重排
    - numpy 类型 → Python 原生类型（避免 Unsupported record type）
    - Tunnel 写入（open_writer）
    """
    df = df.where(pd.notnull(df), None)
    t = o.get_table(table_name)
    is_partitioned = bool(t.table_schema.partitions)
    if is_partitioned and not partition:
        raise ValueError(
            f"表 `{table_name}` 是分区表，必须传 partition 参数（如 ds=20260101）"
        )
    if not is_partitioned and partition:
        # 非分区表忽略 partition 参数
        partition = None

    # 列顺序按 schema（分区字段不参与，由 partition 参数提供）
    schema_cols = [c.name for c in t.table_schema.columns]
    partition_cols = {p.name for p in (t.table_schema.partitions or [])}
    data_cols = [c for c in schema_cols if c not in partition_cols]
    df = df[data_cols]

    # 分区表 + append 模式：先确保分区存在（open_writer(overwrite=False) 不会自动建分区）
    if is_partitioned and partition and mode == "append":
        from odps.types import PartitionSpec
        spec = PartitionSpec(partition)
        if not t.exist_partition(spec):
            print(
                f"[DEBUG] 分区 {partition} 不存在，自动创建...",
                file=sys.stderr,
                flush=True,
            )
            t.create_partition(spec, if_not_exists=True)

    # 关键：先转 Python 原生类型，再写入 ODPS
    records = _df_to_records(df)
    print(
        f"[DEBUG] 转换 {len(records)} 条 records，示例: {records[0] if records else 'empty'}",
        file=sys.stderr,
        flush=True,
    )

    overwrite = (mode == "overwrite")
    with t.open_writer(partition=partition, overwrite=overwrite) as writer:
        writer.write(records)


# ============================================================
# 新增输入模型
# ============================================================


class ExplainSqlInput(BaseModel):
    """odps_explain 输入模型"""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    sql: str = Field(
        ...,
        description="要分析的 SQL 语句。不需要带 EXPLAIN 前缀，工具会自动补充",
        min_length=1,
    )

    @field_validator("sql")
    @classmethod
    def validate_sql(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("SQL 语句不能为空")
        if v.endswith(";"):
            v = v[:-1].strip()
        return v


# ============================================================
# 新增 MCP 工具：odps_explain
# ============================================================

@mcp.tool(
    name="odps_explain",
    annotations={
        "title": "ODPS 获取 SQL 执行计划（不实际执行）",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def odps_explain(sql: str) -> str:
    """获取 SQL 的执行计划，不实际扫描数据。

    适用场景：
    - SQL 调优前查看执行计划（join 顺序、聚合策略、reduce 个数等）
    - 检查是否走了正确的分区（防止全表扫描）
    - 估算 SQL 的输入数据量（Statistics 信息）
    - 对比不同写法的执行计划差异

    注意：
    - 本工具**只生成执行计划，不读取数据**，不会产生计费
    - 用户不需要在 SQL 前加 EXPLAIN，工具会自动补充
    - 返回内容包含 ODPS 编译器的完整计划文本（Job / Task / Statistics 等）

    Args:
        sql (str): 要分析的 SQL（SELECT/INSERT 等可计划化语句均可）

    Returns:
        str: 完整的执行计划文本（含 Instance ID、LogView URL、plan 原文）

    Examples:
        - 看执行计划: sql="SELECT COUNT(*) FROM my_table WHERE day='2026-07-09'"
        - 对比两种 JOIN: sql="SELECT ... FROM a JOIN b ON ..."
    """
    import asyncio
    print("[DEBUG] odps_explain 被调用", file=sys.stderr, flush=True)
    try:
        sql_clean = sql.strip()
        if not sql_clean:
            return "❌ SQL 语句不能为空"
        if sql_clean.endswith(";"):
            sql_clean = sql_clean[:-1].strip()

        # 自动补 EXPLAIN 前缀
        first_word = _extract_first_keyword(sql_clean)
        if first_word != "EXPLAIN":
            sql_clean = "EXPLAIN " + sql_clean

        def _do_explain():
            o = get_odps_connection()
            instance = o.execute_sql(sql_clean)
            # 同步等待完成（EXPLAIN 很快，几秒内）
            instance.wait_for_success()
            plan_text = _fetch_explain_plan(instance)
            return {
                "instance_id": getattr(instance, "id", "(unknown)"),
                "logview": instance.get_logview_address() if hasattr(instance, "get_logview_address") else "",
                "plan": plan_text,
            }

        info = await asyncio.to_thread(_do_explain)

        lines = []
        lines.append(f"✅ 执行计划获取成功")
        lines.append(f"- Instance ID: `{info['instance_id']}`")
        if info.get("logview"):
            lines.append(f"- LogView: {info['logview']}")
        lines.append("")
        lines.append("### 执行计划")
        lines.append("```")
        lines.append(info["plan"] or "(空)")
        lines.append("```")
        return "\n".join(lines)

    except odps_errors.ODPSError as e:
        return f"❌ ODPS 错误: {e}"
    except Exception as e:
        return f"❌ 获取执行计划失败: {type(e).__name__}: {e}"


# ============================================================
# 新增输入模型
# ============================================================


class UploadDataInput(BaseModel):
    """odps_upload_data 输入模型"""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    table_name: Optional[str] = Field(
        default=None,
        description="目标表名（不含项目前缀）。留空时自动生成 tmp_yyyymmdd_{file_basename}",
    )
    file_path: str = Field(
        ...,
        description="本地文件路径，支持 .txt/.csv/.xlsx/.xls；智能识别分隔符与表头",
        min_length=1,
    )
    partition: Optional[str] = Field(
        default=None,
        description="分区 spec，如 ds=20260101。目标表是分区表时必填；非分区表传了会被忽略",
    )
    mode: Literal["append", "overwrite"] = Field(
        default="append",
        description="写入模式：append 追加（默认）/ overwrite 覆盖（覆盖会清空该分区已有数据）",
    )
    lifecycle: Optional[int] = Field(
        default=None,
        description="仅当自动建表时生效：新建表设置生命周期（天），范围 1-37230",
    )
    sheet_name: Optional[str] = Field(
        default=None,
        description="xlsx 文件的工作表名（仅 .xlsx/.xls 时有效）",
    )
    header: Optional[int] = Field(
        default=0,
        description="表头行号（0-based）。0=第 1 行（默认，带表头），None=无表头（自动生成 col_0, col_1, ...）",
    )
    columns: Optional[list] = Field(
        default=None,
        description="手动指定列名列表（覆盖文件表头）。无表头时建议传以保证列名可读，例 ['id','name','amount']",
    )
    usecols: Optional[list] = Field(
        default=None,
        description="只读指定列（性能优化 + 大文件必备）。None=读全部。例 ['id','name'] 按列名，[0,2] 按列索引。注意：列名匹配要求 header=0 即带表头；按索引则不依赖表头",
    )

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, v: str) -> str:
        v = v.strip().strip('"').strip("'")
        if not v:
            raise ValueError("file_path 不能为空")
        return v

    @field_validator("lifecycle")
    @classmethod
    def validate_lifecycle(cls, v):
        if v is None:
            return v
        if v < 1 or v > 37230:
            raise ValueError("lifecycle 必须在 1-37230 范围内")
        return v


class ExecuteSqlInput(BaseModel):
    """odps_execute_sql 输入模型"""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    sql: str = Field(
        ...,
        description="要执行的 ODPS SQL（DDL/DML/管理语句，SELECT 拒绝）",
        min_length=1,
    )
    confirm: bool = Field(
        default=False,
        description="高危操作（DROP/TRUNCATE/DELETE）必须传 True 二次确认；DROP TABLE 只允许 tmp_ 前缀的临时表",
    )

    @field_validator("sql")
    @classmethod
    def validate_sql(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("SQL 语句不能为空")
        if v.endswith(";"):
            v = v[:-1].strip()
        return v


# ============================================================
# 新增 MCP 工具：odps_upload_data
# ============================================================

@mcp.tool(
    name="odps_upload_data",
    annotations={
        "title": "ODPS 数据导入（本地文件 → 表/分区）",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def odps_upload_data(
    table_name: Optional[str] = None,
    file_path: str = "",
    partition: Optional[str] = None,
    mode: str = "append",
    lifecycle: Optional[int] = None,
    sheet_name: Optional[str] = None,
    header: Optional[int] = 0,
    columns: Optional[list] = None,
    usecols: Optional[list] = None,
) -> str:
    """把本地 txt/csv/xlsx 文件导入到 ODPS 表（或指定分区）。

    适用场景：
    - 临时打点数据上云
    - 业务方交付的 csv 文件入仓
    - 批量回填 / 补数

    智能行为：
    - 智能识别分隔符：自动检测 , / \\t / ; / | 四种分隔符
    - 智能识别表头：默认第一行为表头
    - 自动建表：若目标表不存在，根据文件内容推断 schema 并自动 CREATE TABLE
    - 默认表名：未传 table_name 时使用 tmp_yyyymmdd_{file_basename}（取文件名前缀去除扩展名）
    - 严格字段匹配：DataFrame 列必须与表 schema 一致，多/缺都报错（不会静默丢数据）
    - NaN → NULL：pandas NaN/NaT 写为 ODPS NULL

    Args:
        table_name (str, optional): 目标表名（不含项目前缀）。留空时自动生成 tmp_yyyymmdd_{file_basename}
        file_path (str): 本地文件路径，支持 .txt/.csv/.xlsx/.xls
        partition (str, optional): 分区 spec，如 ds=20260130。目标表是分区表时必填；非分区表传了会被忽略
        mode (str): 写入模式，append 追加（默认）/ overwrite 覆盖（覆盖会清空该分区已有数据）
        lifecycle (int, optional): 仅当自动建表时生效：新建表设置生命周期（天），1-37230
        sheet_name (str, optional): xlsx 文件的工作表名（仅 .xlsx/.xls 时有效）

    Returns:
        str: Markdown 格式的执行结果

    Examples:
        - 上传 csv 到默认临时表：file_path="D:/data/sales_2026q1.csv"
        - 上传到指定分区：table_name="ods_sales", file_path="D:/data/sales.csv", partition="ds=20260630", mode="overwrite"
    """
    import asyncio
    print("[DEBUG] odps_upload_data 被调用", file=sys.stderr, flush=True)
    try:
        # 参数标准化
        file_path = str(file_path).strip().strip('"').strip("'")
        if not file_path:
            return "❌ file_path 不能为空"
        if mode not in ("append", "overwrite"):
            return "❌ mode 仅支持 append 或 overwrite"

        # 计算默认表名
        if not table_name or not table_name.strip():
            file_basename = os.path.splitext(os.path.basename(file_path))[0]
            safe_basename = re.sub(r"[^a-zA-Z0-9_]", "_", file_basename)
            date_str = datetime.now().strftime("%Y%m%d")
            table_name = f"tmp_{date_str}_{safe_basename}"

        # pandas 延迟导入（避免 server 启动时未安装就崩）
        global pd
        if "pd" not in globals():
            import pandas as pd  # type: ignore

        def _do_upload():
            o = get_odps_connection()
            print(f"[DEBUG] 读取文件: {file_path}", file=sys.stderr, flush=True)
            df = _read_file_smart(file_path, sheet_name=sheet_name, header=header, columns=columns, usecols=usecols)
            if df.empty:
                raise ValueError("文件为空或仅含表头")
            print(f"[DEBUG] 读到 {len(df)} 行 × {len(df.columns)} 列", file=sys.stderr, flush=True)

            # 确保表存在
            created = _ensure_table_for_df(o, table_name, df, lifecycle=lifecycle)
            print(f"[DEBUG] 表 {table_name} {'新建' if created else '已存在'}", file=sys.stderr, flush=True)

            # 严格字段匹配
            t = o.get_table(table_name)
            _validate_columns_match(t, df)

            # 写入
            _upload_df_to_table(o, table_name, df, partition=partition, mode=mode)
            return created, len(df), df.columns.tolist()

        created, row_count, col_list = await asyncio.to_thread(_do_upload)

        lines = []
        lines.append("✅ 数据导入成功")
        lines.append(f"- 项目: `{get_odps_connection().project}`")
        lines.append(f"- 目标表: `{table_name}`")
        if partition:
            lines.append(f"- 分区: `{partition}`")
        lines.append(f"- 模式: {mode}")
        lines.append(f"- 文件: `{file_path}`")
        lines.append(f"- 写入行数: {row_count:,} 行")
        lines.append(f"- 字段数: {len(col_list)} 个 ({', '.join(f'`{c}`' for c in col_list)})")
        if created:
            lines.append("- ⚠️ 表之前不存在，已根据文件内容自动建表（推断类型）")
        return "\n".join(lines)

    except FileNotFoundError:
        return f"❌ 本地文件不存在: `{file_path}`"
    except odps_errors.NoSuchObject:
        o = get_odps_connection()
        return f"❌ 对象不存在: `{o.project}.{table_name}`"
    except ValueError as e:
        return f"❌ 参数/数据错误: {e}"
    except Exception as e:
        return f"❌ 数据导入失败: {type(e).__name__}: {e}\n请检查文件格式、字段匹配、网络连接或 ODPS 权限。"


# ============================================================
# 新增 MCP 工具：odps_execute_sql
# ============================================================

@mcp.tool(
    name="odps_execute_sql",
    annotations={
        "title": "ODPS 通用 SQL 执行（DDL/DML/管理）",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def odps_execute_sql(
    sql: Optional[str] = None,
    sql_file: Optional[str] = None,
    confirm: bool = False,
) -> str:
    """通用 SQL 执行入口：执行 DDL/DML/管理语句，自动按首词分类返回结果。

    适用场景：执行任何非 SELECT 的 SQL——建表、删表、加列、改生命周期、加/删分区、
    重命名表、改表注释、清空数据、INSERT/UPDATE/DELETE 等。

    sql 与 sql_file 二选一：
      - sql: SQL 字符串（仅支持单条语句）
      - sql_file: .sql 脚本文件路径（支持多条语句，逐条执行）

    === 拒绝 SELECT ===
    SELECT 语句请改用 odps_query_data 工具。

    === 自动按首词分类 ===
    - DDL（CREATE/DROP/ALTER/TRUNCATE/RENAME）
    - DML（INSERT/UPDATE/DELETE）
    - 管理（SHOW/DESC/USE/SET/EXPLAIN/KILL）

    === 危险操作必须 confirm=True ===
    涉及 DROP/TRUNCATE/DELETE 的语句必须显式传 confirm=True 才执行，否则拒绝。

    === DROP TABLE 临时表安全护栏 ===
    只允许删除 tmp_ 开头的表，否则返回错误。非临时表的删除请走工单或 DataWorks。

    === 常用 SQL 语法速查（ODPS） ===
    - 建表: CREATE TABLE IF NOT EXISTS `t` (id BIGINT, name STRING) PARTITIONED BY (day STRING) LIFECYCLE 30;
    - 加列: ALTER TABLE `t` ADD COLUMNS (new_col STRING COMMENT '注释');
    - 改生命周期: ALTER TABLE `t` SET LIFECYCLE 90; 或 disable lifecycle;
    - 加分区: ALTER TABLE `t` ADD IF NOT EXISTS PARTITION (day='2026-06-30');
    - 删分区(高危,confirm): ALTER TABLE `t` DROP IF EXISTS PARTITION (day='2026-06-30');
    - 重命名: ALTER TABLE `old` RENAME TO `new`;
    - 改注释: ALTER TABLE `t` SET COMMENT '新注释';
    - 删表(高危+tmp_校验,confirm): DROP TABLE IF EXISTS `tmp_xxx`;（非 tmp_ 开头会被拒绝）
    - 插入: INSERT OVERWRITE/INTO TABLE `t` PARTITION (day='2026-06-30') SELECT ...;

    Args:
        sql (str): 要执行的 SQL（DDL/DML/管理）
        sql_file (str): .sql 脚本文件路径
        confirm (bool): 危险操作二次确认（DROP/TRUNCATE/DELETE 必须为 True）

    Returns:
        str: Markdown 格式的执行结果（包含 SQL 类型、是否成功、影响行数等）
    """
    import asyncio
    import sys
    print("[DEBUG] odps_execute_sql 被调用", file=sys.stderr, flush=True)
    try:
        if sql is not None and str(sql).strip():
            stmts = split_sql_script(str(sql))
            if len(stmts) > 1:
                return "❌ sql 参数仅支持单条语句（检测到多条）。多语句脚本请用 sql_file 参数。"
        elif sql_file:
            stmts = split_sql_script(_read_sql_file(sql_file))
        else:
            return "❌ 请提供 sql 或 sql_file"
        stmts = [s.strip() for s in stmts if s.strip()]
        if not stmts:
            return "❌ SQL 语句不能为空"

        # 预检 + 分类
        classified = []
        for i, stmt in enumerate(stmts, 1):
            first_word = _extract_first_keyword(stmt)

            if not first_word:
                return f"❌ 第 {i} 条：未能识别 SQL 类型，请检查语句是否正确"
            if first_word == "SELECT":
                return f"❌ 第 {i} 条：SELECT 语句请使用 odps_query_data 工具执行"

            # DROP TABLE 临时表安全护栏
            if first_word == "DROP":
                table_candidate = _extract_drop_table_name(stmt)
                if table_candidate and not table_candidate.lower().startswith("tmp_"):
                    return (
                        f"❌ 第 {i} 条：禁止删除非临时表: `{table_candidate}`。"
                        "本工具只允许删除 tmp_ 开头的临时表，请确认表名或改用 DataWorks/工单。"
                    )

            # 高危操作要求 confirm
            if first_word in ("DROP", "TRUNCATE", "DELETE") and not confirm:
                return (
                    f"❌ 第 {i} 条：高危操作（{first_word}）需要二次确认。"
                    "请设置 confirm=True 后重新调用。\n"
                    f"⚠️ 如是 DROP TABLE，请确保表名以 tmp_ 开头。"
                )

            category, desc = _classify_sql(stmt)
            if category == "unknown":
                return f"❌ 第 {i} 条：未能识别的 SQL 类型（首词: {first_word}），请检查语句是否正确"
            classified.append((stmt, first_word, category, desc))

        def _do_execute(stmt: str):
            o = get_odps_connection()
            instance = o.execute_sql(stmt)
            return {
                "instance_id": getattr(instance, "id", "(unknown)"),
                "logview": instance.get_logview_address() if hasattr(instance, "get_logview_address") else "",
            }

        lines = []
        for i, (stmt, first_word, category, desc) in enumerate(classified, 1):
            info = await asyncio.to_thread(_do_execute, stmt)
            if len(classified) > 1:
                lines.append(f"--- 语句 {i}/{len(classified)} ---")
            lines.append(f"✅ 执行成功 | {desc}")
            lines.append(f"- SQL 类型: {category.upper()}")
            lines.append(f"- Instance ID: `{info['instance_id']}`")
            if info.get("logview"):
                lines.append(f"- LogView: {info['logview']}")
            if len(classified) > 1:
                lines.append("")
        lines.append("- 如需回滚或查询日志，请通过 Instance ID 在 DataWorks / LogView 中查看")
        lines.append("- 💡 如需查看执行计划，请改用 odps_explain 工具")
        return "\n".join(lines)

    except odps_errors.NoSuchObject as e:
        return f"❌ 对象不存在: {e}"
    except odps_errors.ODPSError as e:
        return f"❌ ODPS 错误: {e}"
    except ValueError as e:
        return f"❌ 参数错误: {e}"
    except Exception as e:
        return f"❌ SQL 执行失败: {type(e).__name__}: {e}"


# ============================================================
# 入口
# ============================================================

def main():
    print("[DEBUG] MCP server 开始运行...", file=sys.stderr, flush=True)
    mcp.run()


if __name__ == "__main__":
    print("odps_mcp启动。。", file=sys.stderr, flush=True)
    try:
        main()
    except Exception as e:
        print(f"[ERROR] MCP 运行异常: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc(file=sys.stderr)
    print("odps_mcp执行完毕....", file=sys.stderr, flush=True)
