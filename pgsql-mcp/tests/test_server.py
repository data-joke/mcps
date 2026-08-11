"""pgsql-mcp 轻量测试：核心纯函数（不连真实数据库）。

运行：cd pgsql-mcp && python -m pytest tests/ -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server as mcp_server


def test_split_sql_script_multiple():
    sql = "SELECT 1; SELECT 'a;b' AS x;"
    stmts = mcp_server.split_sql_script(sql)
    assert len(stmts) == 2
    assert stmts[0] == "SELECT 1"


def test_split_sql_script_comment():
    sql = "-- 注释;带分号\nSELECT 2"
    stmts = mcp_server.split_sql_script(sql)
    assert len(stmts) == 1


def test_read_sql_file_roundtrip():
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
        f.write("-- 中文\nSELECT version();\n")
        path = f.name
    try:
        content = mcp_server._read_sql_file(path)
        assert "中文" in content
    finally:
        os.unlink(path)


def test_is_select_query():
    assert mcp_server.is_select_query("SELECT 1")
    assert mcp_server.is_select_query("  -- c\nWITH t AS (SELECT 1) SELECT * FROM t")
    assert not mcp_server.is_select_query("UPDATE t SET x=1")
    assert not mcp_server.is_select_query("CREATE TABLE t (id int)")
