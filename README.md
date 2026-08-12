# MCP 工具集（mcps）

通过 MCP 协议（stdio）提供**数据库操作**与**图片识别**能力的本地工具集，全部为**单文件/轻量 Python**，可接入所有支持 MCP 的客户端（Claude Code、Codex CLI 等）。

| MCP | 定位 | 技术栈 | 工具集 | 目录 |
| --- | --- | --- | --- | --- |
| **mysql-mcp** | MySQL 数据库 | FastMCP + PyMySQL | 查询 / 导出 / 万能执行 / 表结构 | [`mysql-mcp/`](mysql-mcp/) |
| **pgsql-mcp** | PostgreSQL 数据库 | FastMCP + asyncpg | 查询 / 导出 / 结构浏览 / 列采样 | [`pgsql-mcp/`](pgsql-mcp/) |
| **odps-mcp** | 阿里云 MaxCompute (ODPS) | 官方 MCP SDK + pyodps | 查询 / 导出 / 元数据 / DDL·DML / 上传 | [`odps_mcp/`](odps_mcp/) |
| **image-recognizer** | 图片识别（多模态） | FastMCP + openai | 单张/批量识别 / 格式查询 / 视觉拒绝重试 | [`image-recognizer/`](image-recognizer/) |

## 选哪个

- 日常本地 MySQL 操作 → `mysql-mcp`
- PostgreSQL 库结构探索 + 查询导出 → `pgsql-mcp`
- 阿里云数仓（MaxCompute/ODPS）取数、建表、导入 → `odps-mcp`
- 识别图片（URL / 本地文件 / Base64），把多模态转为文本 → `image-recognizer`

## 通用特性

- **SQL 脚本文件读取（`sql_file`）**：所有查询/执行工具都支持从 `.sql` 文件读取长 SQL 或多语句脚本，避免把超长 SQL 塞进提示词导致截断。
- **安全护栏**：写操作需 `confirm=true`；高危关键字（DROP/TRUNCATE/DELETE 等）二次告警；ODPS 还有 `tmp_` 临时表保护。
- **凭据零硬编码**：连接信息一律从环境变量注入，源码不含任何默认连接凭据，`.env` 已被 `.gitignore` 排除。

## 快速开始

每个子目录的 README 都有详细步骤，并附一段**可直接发给 AI 的安装引导提示词**（推荐新手）：

- [mysql-mcp 安装说明](mysql-mcp/README.md)
- [pgsql-mcp 安装说明](pgsql-mcp/README.md)
- [odps-mcp 安装说明](odps_mcp/README.md)

通用步骤：

1. `git clone https://github.com/data-joke/mcps.git`
2. 进入目标子目录，`pip install -r requirements.txt`（odps 为 `pip install -e .`）
3. 把连接配置写入 MCP 客户端配置的 `env` 字段（或本目录 `.env`）
4. 重启 MCP 客户端，跑一个轻量查询验证

## 开发

- 语言/版本：Python ≥ 3.10（odps 依赖需要 ≥3.10；mysql/pgsql 无硬性要求）
- 本地布局：三个子目录独立成包，可分别 clone 或整个仓库使用

## License

[MIT](LICENSE)

## 免责声明

本工具集具备「读本地文件 + 执行 SQL」能力，权限等同于本机用户。请勿在不可信环境使用；数据库写操作请谨慎并配合 `confirm` 确认机制。
