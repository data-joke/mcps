# image-recognizer

通过 MCP 协议（stdio）调用多模态模型识别图片的本地工具。当你的 AI Agent 不支持图片输入时，用它把图片内容转为文本，Agent 即可继续处理。

## 功能特性

| 工具 | 用途 |
| --- | --- |
| `recognize_image(image, prompt, detail_level)` | 识别单张图片，返回文本描述 |
| `recognize_images_batch(images, prompt, detail_level, output_format)` | 批量识别多张图片（并发），结果可合并 / 分开 / 总结 |
| `get_supported_formats()` | 查看支持的图片来源、图片格式和配置说明 |

## 使用说明

- 图片来源支持三种：**URL**（http/https）、**本地文件路径**、**Base64 编码**（兼容 `data:` 前缀）。
- `detail_level`：`brief`（简要）/ `detailed`（详细，默认）/ `comprehensive`（全面分析）。
- `prompt` 自定义识别指导；模型偶发"看不到图片"时会自动重试（`VISION_RETRY` 次）。

## 快速开始 A：手动安装

```bash
git clone https://github.com/data-joke/mcps.git && cd mcps/image-recognizer
pip install -r requirements.txt
```

把 MCP 配置写入你的客户端（Claude Code 为 `~/.claude.json`）：

```json
{
  "mcpServers": {
    "image-recognizer": {
      "type": "stdio",
      "command": "<python绝对路径>",
      "args": ["<server.py绝对路径>"],
      "env": {
        "VISION_MODEL_BASE_URL": "<模型base_url>",
        "VISION_MODEL_API_KEY": "<你的API密钥>",
        "VISION_MODEL_NAME": "<模型名称>"
      }
    }
  }
}
```

配置含密钥，写完务必 `chmod 600 <配置 json 路径>`。

验证：完全重启 MCP 客户端，让 AI 调用 `recognize_image(image="...")` 识别一张图片即成功。

---

## 快速开始 B：AI 安装引导提示词（推荐新手）

> 把下面整段复制发给你的 AI，让 AI 引导你完成安装。

````markdown
你是 MCP 安装助手，帮我安装 `image-recognizer`（一个通过 MCP 协议调用多模态模型识别图片的本地工具）。

- 通信：stdio
- 安装步骤（按顺序执行，每步向用户确认）：

1. **获取源码**——先让用户确定下载目录，然后执行：
   ```bash
   git clone https://github.com/data-joke/mcps.git
   cd mcps/image-recognizer
   ```

2. **采集信息**——3 个环境变量**全部必填**：
   - `VISION_MODEL_BASE_URL`：模型的 OpenAI 兼容 base_url（以官方文档为准）
   - `VISION_MODEL_API_KEY`：API 密钥（从你的模型服务商/套餐获取）
   - `VISION_MODEL_NAME`：模型名称（需支持多模态）
   - 可选：`VISION_MODEL_MAX_TOKENS`（默认 4096）、`VISION_BATCH_CONCURRENCY`（默认 4）、`VISION_RETRY`（默认 1）
   - 明确告知：密钥会以明文写入 MCP 客户端配置，文件将 `chmod 600`

3. **装依赖**
   ```bash
   cd <源码所在目录>/image-recognizer
   pip install -r requirements.txt
   ```

4. **写入 MCP 客户端配置**（按用户所用客户端写入对应 JSON，结构如下）
   ```json
   {
     "mcpServers": {
       "image-recognizer": {
         "type": "stdio",
         "command": "<python绝对路径>",
         "args": ["<server.py绝对路径>"],
         "env": {
           "VISION_MODEL_BASE_URL": "<base_url>",
           "VISION_MODEL_API_KEY": "<API密钥>",
           "VISION_MODEL_NAME": "<模型名称>"
         }
       }
     }
   }
   ```
   写完务必 `chmod 600 <配置 json 路径>`。

5. **验证**
   - 完全重启 MCP 客户端（不是 reload window）
   - 让 AI 调用 `recognize_image(image="<一张图片URL或本地路径>")`，能返回图片描述即成功
   - 失败排查：3 个变量是否齐全、base_url 是否匹配所选模型、账户余额/调用限额

完成后告诉用户：可用工具 `recognize_image` / `recognize_images_batch` / `get_supported_formats`。
````

---

## 配置说明（环境变量）

> **模型要求**：通过 **OpenAI 兼容接口**调用多模态模型，所选模型必须支持 OpenAI 兼容格式（`base_url` + `api_key` + `model_name` 三要素）。国内主流厂商如**通义千问、Kimi、MiniMax、智谱**等均有兼容端点，各厂商 `base_url` **以官方文档为准**——`.env.example` 中列出的仅作参考，可能过期。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `VISION_MODEL_BASE_URL` | ✅ | 模型的 OpenAI 兼容 base_url |
| `VISION_MODEL_API_KEY` | ✅ | API 密钥 |
| `VISION_MODEL_NAME` | ✅ | 模型名称（需支持多模态） |
| `VISION_MODEL_MAX_TOKENS` | 否 | 输出最大 token（默认 4096；推理模型建议 ≥2000） |
| `VISION_BATCH_CONCURRENCY` | 否 | 批量识别并发数（默认 4，建议 1-8） |
| `VISION_RETRY` | 否 | 模型偶发拒绝视觉时的重试次数（默认 1） |

### 配置样例（OpenCode Go）

以当前使用的 OpenCode Go 套餐为例（`mimo-v2.5`，推理 + 多模态）：

```bash
VISION_MODEL_BASE_URL=https://opencode.ai/zen/go/v1
VISION_MODEL_API_KEY=sk-<你的 OpenCode Go API Key>
VISION_MODEL_NAME=mimo-v2.5
VISION_MODEL_MAX_TOKENS=4096
```

配置方式（二选一）：
1. **推荐**：写入 MCP 客户端配置的 `env` 字段（见上文快速开始）。
2. 本地调试：复制 `.env.example` 为 `.env` 填入。

源码不含任何密钥，`.env` 已被 `.gitignore` 排除，绝不进入版本控制。

## 安全说明

- API 密钥以明文存在于客户端配置 / 本地 `.env`，配置 json 请 `chmod 600`。
- `.env` 已被 `.gitignore` 排除，绝不进入版本控制。
- 该 MCP 具备读取本地文件的能力（图片路径），权限等同于本机用户，请勿在不可信环境使用。

## License

[MIT](../LICENSE)
