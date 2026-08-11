"""mysql-mcp 轻量测试：核心纯函数（不连真实数据库）。

运行：cd mysql-mcp && python -m pytest tests/ -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server as mcp_server


def test_split_sql_script_multiple():
    sql = "SELECT a, '分号;测试' FROM t;  -- 注释;带分号\nUPDATE t SET x=1 WHERE id=2;"
    stmts = mcp_server.split_sql_script(sql)
    assert len(stmts) == 2
    assert "SELECT" in stmts[0]
    # 第 2 条带前导注释，注释内的分号不应被切分
    assert "UPDATE" in stmts[1]
    assert "注释;带分号" in stmts[1]


def test_split_sql_script_comment_preserved():
    # 注释内的分号不应被切分
    sql = "-- 注释;带分号\nSELECT 2"
    stmts = mcp_server.split_sql_script(sql)
    assert len(stmts) == 1


def test_read_sql_file_roundtrip():
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
        f.write("-- 中文注释\nSELECT 'a;b' AS x;\n")
        path = f.name
    try:
        content = mcp_server._read_sql_file(path)
        assert "中文注释" in content
        assert "SELECT" in content
    finally:
        os.unlink(path)


def test_read_sql_file_missing():
    try:
        mcp_server._read_sql_file("/tmp/definitely_not_exist_12345.sql")
        assert False, "应抛出 FileNotFoundError"
    except FileNotFoundError:
        pass


def test_required_env_check():
    # 保留当前值以便恢复
    saved = {k: os.environ.get(k) for k in ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        try:
            mcp_server._check_required_env()
            assert False, "缺失必填变量应抛错"
        except RuntimeError as e:
            assert "MYSQL_HOST" in str(e)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_is_read_query():
    assert mcp_server.is_read_query("SELECT 1")
    assert mcp_server.is_read_query("  -- 注释\nWITH t AS (...) SELECT *")
    assert not mcp_server.is_read_query("INSERT INTO t VALUES (1)")
