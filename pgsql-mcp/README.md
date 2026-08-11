# pgsql-mcp

通过 MCP 协议（stdio）操作 PostgreSQL 数据库。基于 FastMCP / asyncpg 实现。

## 功能特性

- `query(sql=None, sql_file=None, limit=None)` — 执行 SELECT/WITH/EXPLAIN/SHOW，返回 JSON
- `export_query(sql, format="csv", export_dir=None, filename=None)` — 导出查询结果为 CSV/XLSX
- `execute(sql=None, sql_file=None, confirm=False)` — 写操作，需 `confirm=true`
- `create_table / drop_table` — DDL
- `list_databases / list_schemas / list_tables / list_roles`
- `describe_table / describe_columns` — 列结构（含注释）
- `list_indexes / list_foreign_keys`
- `show_tables(schema, with_stats=True)` — 列出表 + 行数估算 + 占用
- `sample_rows(table, schema, limit, order_by)` — 取样本行

### 📄 从 .sql 脚本文件读取（长 SQL / 多语句）

`query` 与 `execute` 都支持 `sql_file` 参数，从脚本文件读取 SQL，避免把超长 SQL 字符串塞进提示词导致截断或异常：

```
query(sql_file="/path/to/query.sql", limit=500)
execute(sql_file="/path/to/migration.sql", confirm=True)
```

- 脚本支持多条语句，`query` 逐条只读执行并汇总，`execute` 逐条执行写操作。
- 单语句脚本退化为与 `sql` 参数完全一致的行为。
- 自动识别 utf-8 / gbk 等编码；单文件上限 5MB。
- 限制：不处理存储过程/DELIMITER 等函数体内含分号的语句（拆分会误切）。

---

## 快速开始 A：手动安装

```bash
git clone <本仓库地址> && cd pgsql-mcp
pip install -r requirements.txt
cp .env.example .env   # 编辑填入 PostgreSQL 连接信息
```

两种配置方式二选一（在 `.env` 或 MCP 客户端配置的 env 字段中）：

- `PG_CONNECTION_STRING`：完整 DSN，例如 `postgresql://user:pwd@host:5432/db`
- `PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DATABASE`：拆分字段

注册到 MCP 客户端（Claude Code 为 `~/.claude.json`）：

```json
"pgsql": {
  "type": "stdio",
  "command": "<python绝对路径>",
  "args": ["<server.py绝对路径>"],
  "env": {}
}
```

`.env` 文件位于本目录，源码不含任何凭据；`.env` 已被 `.gitignore` 排除。

---

## 快速开始 B：AI 安装引导提示词（推荐新手）

> 把下面整段复制发给你的 AI，让 AI 引导你完成安装。

````markdown
你是 MCP 安装助手，帮我安装 `pgsql-mcp`（一个通过 MCP 协议操作 PostgreSQL 的本地工具）。

- 源码：本仓库根目录（README 所在目录），server.py 在仓库根目录。请让用户确定源码在本机的绝对路径，后续所有路径都用绝对路径。
- 通信：stdio
- 安装步骤（按顺序执行，每步向用户确认）：

1. **采集信息**
   - Python 解释器绝对路径（让用户跑 `which python3`）
   - PostgreSQL 连接信息：HOST / PORT（默认 5432）/ USER / PASSWORD / DATABASE
   - 明确告知：密码会以明文写入 MCP 客户端配置或本目录 `.env`，文件将 `chmod 600`

2. **装依赖 + 写配置**
   ```bash
   cd <源码所在目录>
   pip install -r requirements.txt
   cp .env.example .env   # 编辑填入连接信息，或直接在客户端配置的 env 字段里填
   ```

3. **写入 MCP 客户端配置**（按用户所用客户端写入对应 JSON，结构如下）
   ```json
   {
     "mcpServers": {
       "pgsql": {
         "type": "stdio",
         "command": "<python绝对路径>",
         "args": ["<server.py绝对路径>"],
         "env": {
           "PG_CONNECTION_STRING": "postgresql://<user>:<password>@<host>:5432/<database>"
         }
       }
     }
   }
   ```
   写完务必 `chmod 600 <配置 json 路径>`。

4. **验证**
   - 完全重启 MCP 客户端（不是 reload window）
   - 执行 `pgsql__query("SELECT version()")`，能返回版本信息即成功
   - 失败排查：端口可达性（`nc -vz host port`）、账号密码、pg_hba 授权、server.py 路径、Python 是否能 `import server`

完成后告诉用户：可用工具 `query` / `export_query` / `execute` / 各类结构浏览工具，长 SQL 可优先用 `sql_file` 参数。
````

---

## Resources

- `pg://connection` — 当前连接信息（密码脱敏）
- `pg://databases` — 所有数据库
- `pg://tables` — 当前 public schema 表名

## 安全说明

- `execute` 默认拒绝执行，需要 `confirm=true`；`drop_table` 同样需要 `confirm=true`。
- 大表 `sample_rows` 默认 10 行、上限 1000；使用 `ORDER BY random()` 在大表上会变慢，可指定 `order_by`。
- 凭据只存在于客户端配置 / 本地 `.env`，`.env` 已被 `.gitignore` 排除，绝不进入版本控制。
- 该 MCP 具备读本地文件 + 执行 SQL 的能力，权限等同于本机用户，请勿在不可信环境使用。
