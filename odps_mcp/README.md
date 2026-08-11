# ODPS MCP Server

阿里云 MaxCompute (ODPS) 的 MCP (Model Context Protocol) Server，为 AI Agent 提供完整的 SQL 查询、数据导入导出、表元数据管理和安全 SQL 执行能力。

通过 stdio 协议与 AI Agent 通信，可适配所有支持 MCP 协议的客户端（Claude Code、Codex CLI、Hermes Agent 等）。

---

## 功能

提供 **6 个核心工具**，覆盖 ODPS 日常使用全场景：

### 数据查询 / 导出

| 工具 | 说明 | 适用场景 |
|------|------|----------|
| `odps_query_data` | 执行 SQL，结果以 Markdown 表格返回 | 数据分析、快速验证、查看指标 |
| `odps_export_data` | 执行 SQL，导出为 xlsx/csv 文件 | 数据交付、周报、批量下载 |

### 元数据 / 发现

| 工具 | 说明 | 适用场景 |
|------|------|----------|
| `odps_list_tables` | 列出项目下的表，支持通配符模糊搜索 | 写 SQL 前发现表名 |
| `odps_get_table_metadata` | 获取单张表的完整元数据（字段/类型/注释/分区/生命周期）| 写 SQL 前必备 |

### 通用 SQL 执行（DDL/DML）

| 工具 | 说明 | 适用场景 |
|------|------|----------|
| `odps_execute_sql` | 执行 DDL/DML/管理语句，**拒绝 SELECT** | 建表、改表、加列、INSERT、删表、改生命周期、加/删分区 |

**安全特性**：
- DROP/TRUNCATE/DELETE 高危操作需 `confirm=true` 二次确认
- DROP TABLE 仅允许 `tmp_` 前缀的临时表，**业务表保护护栏**（防误删生产数据）

### 📄 从 .sql 脚本文件读取（长 SQL / 多语句）

`odps_query_data` 与 `odps_execute_sql` 都支持 `sql_file` 参数，从脚本文件读取 SQL，避免把超长 SQL 字符串塞进提示词导致截断或异常：

```
odps_query_data(sql_file="/path/to/query.sql", max_rows=500)
odps_execute_sql(sql_file="/path/to/migration.sql", confirm=True)
```

- 脚本支持多条语句：`odps_query_data` 逐条只读执行并汇总；`odps_execute_sql` 逐条执行，**每条都套用 SELECT 拒绝 / DROP 临时表护栏 / confirm 校验**。
- 单语句脚本退化为与 `sql` 参数完全一致的行为。
- 自动识别 utf-8 / gbk 等编码；单文件上限 5MB。
- 限制：不处理存储过程/DELIMITER 等函数体内含分号的语句（拆分会误切）。

### 数据导入

| 工具 | 说明 | 适用场景 |
|------|------|----------|| `odps_upload_data` | 把本地 txt/csv/xlsx 导入到 ODPS 表（或指定分区）| 临时打点、业务方数据交付、批量回填 |

**智能特性**：
- 自动识别分隔符（`,` / `\t` / `;` / `|`）和表头
- 表不存在时**自动建表**（按内容推断 schema）
- append / overwrite 两种模式
- 支持非分区表 + 分区表（分区表 append 模式自动建分区）
- 严格字段匹配（缺/多字段直接报错，不静默丢数据）
- NaN → NULL、numpy 类型 → Python 原生类型（避免 Tunnel 写入异常）

---

## 环境要求

- Python >= 3.10
- 阿里云 AccessKey（需有目标 MaxCompute 项目的访问权限）
- 目标 MaxCompute 项目的 endpoint（深圳/北京/上海等 region 不同）

## 依赖

```
mcp>=1.0.0
pyodps>=0.12.0
pandas>=1.5.0
openpyxl>=3.0.0
```

## 必填环境变量

**4 个环境变量全部必填，无任何默认值兜底**——任一缺失立即报错，避免隐式回退到错误配置。

| 变量 | 必填 | 说明 | 取值来源 |
|------|------|------|----------|
| `ODPS_ACCESS_ID` | ✅ | 阿里云 AccessKey ID | [RAM 控制台](https://ram.console.aliyun.com/manage/ak) |
| `ODPS_ACCESS_KEY` | ✅ | 阿里云 AccessKey Secret | 创建 AccessKey 时**只显示一次**，务必保存 |
| `ODPS_PROJECT` | ✅ | MaxCompute 项目名 | [MaxCompute 控制台](https://maxcompute.console.aliyun.com/) 查项目列表 |
| `ODPS_ENDPOINT` | ✅ | MaxCompute 服务端点 | 取决于项目所在 region（见下表）|

> `ODPS_ENDPOINT` 因 region 而异。常见格式：`http://service.cn-shenzhen.maxcompute.aliyun.com/api`（深圳）、`http://service.cn-beijing.maxcompute.aliyun.com/api`（北京）、`http://service.cn-shanghai.maxcompute.aliyun.com/api`（上海）等。

---

## 🆔 如何获取这 4 个值

### 1. AccessKey（`ODPS_ACCESS_ID` + `ODPS_ACCESS_KEY`）

**推荐用 RAM 子账号 AccessKey**（不要用主账号，权限过大且风险高）。

**步骤**：
1. 登录 [RAM 访问控制](https://ram.console.aliyun.com/) 控制台
2. 左侧菜单 → **人员** → **用户** → 选中要用的子账号 → 进入详情
3. **认证管理** 页签 → **AccessKey** 区域 → **创建 AccessKey**
4. 系统会弹出 **AccessKey ID 和 Secret**，**Secret 仅显示一次**，必须当场复制保存（或下载 CSV）
5. 重要：创建后到 **权限管理** 页签给该子账号授权 MaxCompute 项目访问权限（`AliyunMaxComputeFullAccess` 或更细粒度）

**安全提示**：
- ❌ 不要把 AccessKey 提交到 git
- ❌ 不要用主账号 AccessKey
- ✅ 建议给 AccessKey 配 IP 白名单
- ✅ 定期轮换（90 天一次）

### 2. 项目名（`ODPS_PROJECT`）

**步骤**：
1. 登录 [MaxCompute 控制台](https://maxcompute.console.aliyun.com/)（或 [DataWorks](https://dataworks.console.aliyun.com/) 控制台）
2. 左侧 → **Project 管理** / **工作空间列表**
3. 复制你要访问的项目名（不含后缀 `.cn-shanghai` 之类，那是 region 标识）

**项目名格式**：`{业务线}_{环境}_{region}`，例 `my_company_dev`、`bi_dw_prd`

### 3. Endpoint（`ODPS_ENDPOINT`）

**根据项目所在 region 选**：

| Region（项目所在）| Endpoint |
|-------------------|----------|
| 深圳 | `http://service.cn-shenzhen.maxcompute.aliyun.com/api` |
| 北京 | `http://service.cn-beijing.maxcompute.aliyun.com/api` |
| 上海 | `http://service.cn-shanghai.maxcompute.aliyun.com/api` |
| 杭州 | `http://service.cn-hangzhou.maxcompute.aliyun.com/api` |
| 香港 | `http://service.cn-hongkong.maxcompute.aliyun.com/api` |
| 新加坡 | `http://service.ap-southeast-1.maxcompute.aliyun.com/api` |

**怎么查项目所在 region**：
- DataWorks 工作空间详情里能直接看到
- MaxCompute 项目列表里每行都标了 region

> ⚠️ 项目和 endpoint 的 region 必须一致，否则会报"项目不存在"或鉴权失败。

### 4. 快速验证脚本

拿到 4 个值后，先单独验证连接（不用 MCP）：

```bash
python -c "
import os
os.environ['ODPS_ACCESS_ID'] = '你的 ID'
os.environ['ODPS_ACCESS_KEY'] = '你的 Secret'
os.environ['ODPS_PROJECT'] = '你的项目名'
os.environ['ODPS_ENDPOINT'] = '你的 endpoint'
from odps_mcp.server import get_odps_connection
o = get_odps_connection()
print(f'✅ 连接成功, project={o.project}')
print(f'表数: {len(list(o.list_tables()))}')
"
```

成功打印出 `✅ 连接成功` 即可继续配置 MCP。

---

## 安装

### Step 0: 获取源码

```bash
git clone https://github.com/data-joke/mcps.git
cd mcps/odps_mcp
```

### Step 1: 安装 Python 包

```bash
pip install -e .
```

或手动装依赖：

```bash
pip install mcp pyodps pandas openpyxl
```

### Step 2: 在 MCP 客户端中注册

**通用做法**：所有支持 MCP stdio 的客户端，都需要告知它"如何启动这个 server"。配置本质是相同的——

```json
{
  "command": "python",
  "args": ["-m", "odps_mcp"],
  "env": {
    "ODPS_ACCESS_ID": "<你的 AccessKey ID>",
    "ODPS_ACCESS_KEY": "<你的 AccessKey Secret>",
    "ODPS_PROJECT": "<你的项目名>",
    "ODPS_ENDPOINT": "<你的 region endpoint>"
  }
}
```

每个客户端把这个 JSON 落到它自己的配置文件中即可。

---

## 🤖 AI 安装引导提示词（推荐新手）

> 把下面整段复制发给你的 AI，让 AI 引导你完成安装。

````markdown
你是 MCP 安装助手，帮我安装 `odps-mcp`（一个通过 MCP 协议操作阿里云 MaxCompute/ODPS 的工具）。

- 通信：stdio
- 安装步骤（按顺序执行，每步向用户确认）：

1. **获取源码**——先让用户确定下载目录，然后执行：
   ```bash
   git clone https://github.com/data-joke/mcps.git
   cd mcps/odps_mcp
   ```

2. **采集信息**——4 个环境变量**全部必填**，不要用任何默认值，请先问用户以下 4 个值：
   - `ODPS_ACCESS_ID`：阿里云 AccessKey ID（RAM 子账号，权限最小化）
   - `ODPS_ACCESS_KEY`：AccessKey Secret（创建时只显示一次）
   - `ODPS_PROJECT`：MaxCompute 项目名（DataWorks 工作空间详情可查）
   - `ODPS_ENDPOINT`：项目所在 region 的 endpoint，如 `http://service.cn-shenzhen.maxcompute.aliyun.com/api`
   - 明确告知：密钥会以明文写入 MCP 客户端配置，文件将 `chmod 600`

3. **装依赖**
   ```bash
   cd <源码所在目录>/odps_mcp
   pip install -e .
   ```

4. **写入 MCP 客户端配置**（按用户所用客户端写入对应 JSON，结构如下）
   ```json
   {
     "mcpServers": {
       "odps": {
         "type": "stdio",
         "command": "<python绝对路径>",
         "args": ["-m", "odps_mcp"],
         "env": {
           "ODPS_ACCESS_ID": "<AccessKey ID>",
           "ODPS_ACCESS_KEY": "<AccessKey Secret>",
           "ODPS_PROJECT": "<项目名>",
           "ODPS_ENDPOINT": "<region endpoint>"
         }
       }
     }
   }
   ```
   写完务必 `chmod 600 <配置 json 路径>`。

5. **验证**
   - 完全重启 MCP 客户端（不是 reload window）
   - 执行 `odps__odps_query_data("SELECT GETDATE()")`，能返回时间即成功
   - 失败排查：4 个变量是否齐全、AccessKey 是否有项目权限、endpoint region 是否与项目一致

完成后告诉用户：可用工具 `odps_query_data` / `odps_export_data` / `odps_execute_sql` / `odps_list_tables` / `odps_get_table_metadata` / `odps_upload_data`，长 SQL 可优先用 `sql_file` 参数。
````

---

## 示例 1：Claude Code

### 方式 A：CLI 一键添加

```bash
claude mcp add -s user \
  -e ODPS_ACCESS_ID=<你的AccessKey_ID> \
  -e ODPS_ACCESS_KEY=<你的AccessKey_Secret> \
  -e ODPS_PROJECT=<你的项目名> \
  -e ODPS_ENDPOINT=<你的region_endpoint> \
  odps -- python -m odps_mcp
```

`-s user` 表示用户级（所有项目生效）。运行后立即可用，按提示授权工具即可。

### 方式 B：手动改 `~/.claude.json`

在 `mcpServers` 段下加入：

```json
{
  "mcpServers": {
    "odps": {
      "command": "python",
      "args": ["-m", "odps_mcp"],
      "env": {
        "ODPS_ACCESS_ID": "<你的AccessKey_ID>",
        "ODPS_ACCESS_KEY": "<你的AccessKey_Secret>",
        "ODPS_PROJECT": "<你的项目名>",
        "ODPS_ENDPOINT": "<你的region_endpoint>"
      }
    }
  }
}
```

### 验证

```bash
/mcp                      # 查看 MCP 状态
```

或在对话中直接让 Claude 调用：

```
查下我的项目下 test_* 开头的表
```

---

## 示例 2：Codex CLI

Codex CLI 配置文件位于 `~/.codex/config.toml`，在文件末尾追加：

```toml
[mcp_servers.odps]
command = "python"
args = ["-m", "odps_mcp"]

[mcp_servers.odps.env]
ODPS_ACCESS_ID = "<你的AccessKey_ID>"
ODPS_ACCESS_KEY = "<你的AccessKey_Secret>"
ODPS_PROJECT = "<你的项目名>"
ODPS_ENDPOINT = "<你的region_endpoint>"
```

保存后重启 Codex CLI 生效。

---

## 工具详细参数

### odps_query_data

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `sql` | string | 与 sql_file 二选一 | ODPS SQL 语句 |
| `sql_file` | string | 与 sql 二选一 | .sql 脚本文件路径（长 SQL / 多条只读语句，逐条执行并汇总）|
| `max_rows` | int | 200 | 返回给大模型的最大行数（1-1000） |

> 提示：ODPS 限制 `ORDER BY` 必须配合 `LIMIT`（安全机制）。

### odps_export_data

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `sql` | string | 必填 | ODPS SQL 语句 |
| `file_type` | string | xlsx | xlsx 或 csv |
| `save_path` | string | 必填 | 完整保存路径（含文件名）|

### odps_list_tables

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `name_pattern` | string | 空 | fnmatch 通配符：`user_*` / `*_dwd` / `user_???` |
| `limit` | int | 100 | 返回上限（1-1000）|
| `offset` | int | 0 | 分页偏移 |

### odps_get_table_metadata

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `table_name` | string | 是 | 不含项目前缀 |

### odps_execute_sql

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sql` | string | 与 sql_file 二选一 | DDL/DML/管理语句（**不接受 SELECT**）|
| `sql_file` | string | 与 sql 二选一 | .sql 脚本文件路径（支持多条语句，逐条执行；每条都套用 SELECT 拒绝 / DROP 护栏 / confirm 校验）|
| `confirm` | bool | 否 | DROP/TRUNCATE/DELETE 必须 `true` |

**支持的 SQL 类型**：
- DDL：`CREATE` / `DROP` / `ALTER` / `TRUNCATE` / `RENAME`
- DML：`INSERT` / `UPDATE` / `DELETE`
- 管理：`DESC` / `SHOW` / `USE` / `SET` / `EXPLAIN` / `KILL`

**护栏规则**：
- `DROP TABLE` 只允许 `tmp_` 前缀的临时表
- 业务表删除请走 DataWorks / 工单

### odps_upload_data

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `file_path` | string | 必填 | 本地 txt/csv/xlsx/xls 路径 |
| `table_name` | string | 自动 | 留空则用 `tmp_yyyymmdd_{file_basename}` |
| `partition` | string | 空 | 分区表必填，例 `ds=20260630` |
| `mode` | enum | append | append / overwrite |
| `lifecycle` | int | 空 | 自动建表时的生命周期（1-37230 天）|
| `sheet_name` | string | 空 | xlsx 工作表名（仅 xlsx）|
| `header` | int | `0` | 表头行号（0-based）。`0`=第 1 行（默认，带表头），`None`=无表头（自动生成 `col_0, col_1, ...`）|
| `columns` | list | 空 | 手动指定列名（覆盖文件表头），无表头时建议传，例 `['id','name','amount']` |
| `usecols` | list | 空 | 只读指定列（性能优化，大文件必备）。按列名 `['id','amount']` 或按列索引 `[0,2]`。None=读全部 |

**表头处理优先级**：`columns` > `header` > 自动

**示例**：
```python
# 默认（带表头，读全部列）
odps_upload_data(file_path="data.csv", table_name="t")

# 无表头文件 + 指定列名
odps_upload_data(
    file_path="raw.csv",
    table_name="t",
    header=None,
    columns=["id", "name", "amount"]
)

# 文件第 3 行是表头（0-based = 2）
odps_upload_data(file_path="data.csv", table_name="t", header=2)

# 大文件只读 2 列（节省内存）
odps_upload_data(
    file_path="huge.csv",          # 100 列、10GB
    table_name="t",
    usecols=["id", "amount"]      # 只读这 2 列
)

# 无表头 + 按列索引选列 + 改名
odps_upload_data(
    file_path="raw.csv",
    table_name="t",
    header=None,
    usecols=[0, 2],                # 只读第 1 列和第 3 列
    columns=["id", "amount"]      # 改名
)
```

---

## 目录结构

```
odps_mcp/
├── pyproject.toml          # 项目配置和依赖
├── README.md               # 本文件
└── odps_mcp/
    ├── __init__.py
    └── server.py           # MCP Server 实现（6 个工具）
```

> 注：本目录位于 monorepo 仓库 `data-joke/mcps` 内，LICENSE 在仓库根目录。

---

## 常见问题

**Q: 报 `Connection closed` 或进程异常退出？**
A: 4 个环境变量必须全部设置（无默认值）；用 `python -c "from odps_mcp.server import get_odps_connection; print(get_odps_connection().project)"` 单独验证连接。

**Q: `odps_query_data` 行数限制怎么办？**
A: 默认 200 行返回大模型；如需完整数据用 `odps_export_data` 导出。

**Q: DROP 业务表被拒绝？**
A: 这是护栏。业务表删除请走 DataWorks/工单；工具只允许删 `tmp_` 前缀临时表。

---

