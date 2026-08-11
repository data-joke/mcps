"""odps-mcp 轻量测试：核心纯函数（不连真实 ODPS）。

运行：cd odps_mcp && python -m pytest tests/ -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from odps_mcp import server as mcp_server


def test_split_sql_script_multiple():
    sql = "CREATE TABLE IF NOT EXISTS `t` (id BIGINT);ALTER TABLE `t` ADD COLUMNS (a STRING);"
    stmts = mcp_server.split_sql_script(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE")
    assert stmts[1].startswith("ALTER")


def test_split_sql_script_backtick_semicolon():
    # 反引号/字符串内的分号不切分
    sql = "SELECT 'a;b'; SELECT 2;"
    stmts = mcp_server.split_sql_script(sql)
    assert len(stmts) == 2


def test_read_sql_file_roundtrip():
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
        f.write("-- 中文注释\nSELECT GETDATE();\n")
        path = f.name
    try:
        content = mcp_server._read_sql_file(path)
        assert "中文注释" in content
    finally:
        os.unlink(path)


def test_extract_first_keyword():
    assert mcp_server._extract_first_keyword("  -- 注释\nCREATE TABLE t (id int)") == "CREATE"
    assert mcp_server._extract_first_keyword("insert into t values (1)") == "INSERT"
    assert mcp_server._extract_first_keyword("  ") == ""


def test_odps_config_required():
    saved = {k: os.environ.get(k) for k in ("ODPS_ACCESS_ID", "ODPS_ACCESS_KEY", "ODPS_PROJECT", "ODPS_ENDPOINT")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        try:
            mcp_server.get_odps_config()
            assert False, "缺失 ODPS 变量应抛错"
        except ValueError as e:
            assert "ODPS_ACCESS_ID" in str(e)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
