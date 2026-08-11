# mysql-mcp

通过 MCP 协议（stdio）操作 MySQL 的本地子进程。

## 功能特性

| 工具 | 用途 |
| --- | --- |
| `query(sql=None, sql_file=None, limit=None)` | 只读 `SELECT`/`WITH`/`EXPLAIN`/`SHOW`/`DESCRIBE`，返回 JSON（默认 LIMIT 1000） |
| `export_query(sql, format="csv", export_dir=None, filename=None)` | 只读 SQL 导出为 csv/xlsx |
| `execute(sql=None, sql_file=None, confirm=False)` | 万能执行；自动判别只读/写操作，写操作需 `confirm=true` |
| `describe_table(table, database=None)` | 输出完整表结构（字段/类型/可空/默认值/键/注释） |

> 写操作（DROP/DELETE/UPDATE/ALTER 等）即使 `confirm=true` 也会打印告警。

### 📄 从 .sql 脚本文件读取（长 SQL / 多语句）

`query` 与 `execute` 都支持 `sql_file` 参数，从脚本文件读取 SQL，避免把超长 SQL 字符串塞进提示词导致截断或异常：

```
query(sql_file="/path/to/query.sql", limit=500)
execute(sql_file="/path/to/migration.sql", confirm=True)
```

- 脚本支持多条语句，`query` 逐条只读执行并汇总，`execute` 逐条执行。
- 单语句脚本退化为与 `sql` 参数完全一致的行为。
- 自动识别 utf-8 / gbk 等编码；单文件上限 5MB。
- 限制：不处理存储过程/DELIMITER 等函数体内含分号的语句（拆分会误切）。

---

## 快速开始 A：手动安装

```bash
git clone https://github.com/data-joke/mcps.git && cd mcps/mysql-mcp
pip install -r requirements.txt
```

把 MCP 配置写入你的客户端（Claude Code 为 `~/.claude.json`）：

```json
{
  "mcpServers": {
    "mysql": {
      "type": "stdio",
      "command": "<python绝对路径>",
      "args": ["<server.py绝对路径>"],
      "env": {
        "MYSQL_HOST": "<host>",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "<user>",
        "MYSQL_PASSWORD": "<password>",
        "MYSQL_DATABASE": "<database>"
      }
    }
  }
}
```

配置含密码，写完务必 `chmod 600 <配置 json 路径>`。

验证：完全重启 MCP 客户端，执行 `mysql__execute("SHOW DATABASES")` 能返回库列表即成功。

---

## 快速开始 B：AI 安装引导提示词（推荐新手）

> 把下面整段复制发给你的 AI，让 AI 引导你完成安装。

````markdown
你是 MCP 安装助手，帮我安装 `mysql-mcp`（一个通过 MCP 协议操作 MySQL 的本地工具）。

- 通信：stdio
- 安装步骤（按顺序执行，每步向用户确认）：

1. **获取源码**——先让用户确定下载目录，然后执行：
   ```bash
   git clone https://github.com/data-joke/mcps.git
   cd mcps/mysql-mcp
   ```

2. **采集信息**
   - Python 解释器绝对路径（让用户跑 `which python3`）
   - MySQL 连接信息：HOST / PORT（默认 3306）/ USER / PASSWORD / DATABASE（可省）
   - 明确告知：密码会以明文写入 MCP 客户端配置，文件将 `chmod 600`

3. **装依赖**
   ```bash
   cd <源码所在目录>
   pip install -r requirements.txt
   ```

4. **写入 MCP 客户端配置**（按用户所用客户端写入对应 JSON，结构如下）
   ```json
   {
     "mcpServers": {
       "mysql": {
         "type": "stdio",
         "command": "<python绝对路径>",
         "args": ["<server.py绝对路径>"],
         "env": {
           "MYSQL_HOST": "<host>",
           "MYSQL_PORT": "<port>",
           "MYSQL_USER": "<user>",
           "MYSQL_PASSWORD": "<password>",
           "MYSQL_DATABASE": "<database>"
         }
       }
     }
   }
   ```
   写完务必 `chmod 600 <配置 json 路径>`。

5. **验证**
   - 完全重启 MCP 客户端（不是 reload window）
   - 执行 `mysql__execute("SHOW DATABASES")`，能返回库列表即成功
   - 失败排查：端口可达性（`nc -vz host port`）、账号密码、MySQL 远程授权、server.py 路径、Python 是否能 `import server`

完成后告诉用户：可用工具 `query` / `export_query` / `execute` / `describe_table`，长 SQL 可优先用 `sql_file` 参数。
````

---

## 配置说明（环境变量）

> **连接凭据不设默认值**——每个人数据库配置不同，必须通过环境变量提供，缺失会报错。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `MYSQL_HOST` | ✅ | 数据库地址 |
| `MYSQL_USER` | ✅ | 用户名 |
| `MYSQL_PASSWORD` | ✅ | 密码 |
| `MYSQL_DATABASE` | 否 | 默认库；未指定时只能跨库查询 |
| `MYSQL_PORT` | 否 | 端口（默认 3306） |
| `MYSQL_CHARSET` | 否 | 字符集（默认 utf8mb4） |
| `MYSQL_CONNECT_TIMEOUT` / `MYSQL_READ_TIMEOUT` | 否 | 连接/读取超时秒数（默认 10 / 60） |

配置方式（二选一）：
1. **推荐**：写入 MCP 客户端配置的 `env` 字段（见上文快速开始）
2. 本地调试：复制 `.env.example` 为 `.env` 填入

源码不含任何凭据，`.env` 已被 `.gitignore` 排除，绝不进入版本控制。

## 安全说明

- `execute` 写操作需 `confirm=true`；高危关键字即使确认也会告警。
- 凭据只存在于客户端配置 / 本地 `.env`，`.env` 已被 `.gitignore` 排除，绝不进入版本控制。
- 该 MCP 具备读本地文件 + 执行 SQL 的能力，权限等同于本机用户，请勿在不可信环境使用。
