# websearch-deepseek

基于 DeepSeek / OpenCode Go **服务端原生搜索**的联网搜索 MCP，让 Claude Code 等客户端具备联网搜索能力。支持 **DeepSeek 官方** 和 **OpenCode Go** 两个后端。

## AI 安装提示词

> 复制下面整段发给 AI，让它引导你完成安装。

````markdown
使用如下指令从 github 下载 deepseek 网络搜索的 mcp：

```bash
git clone https://github.com/data-joke/mcps.git
cd mcps/websearch-deepseek
pip install -r requirements.txt
```

下载完后配置环境变量（写入 .env 或 MCP 客户端的 env）：

```
WEBSEARCH_BASE_URL=<后端url，参考下方>
WEBSEARCH_API_KEY=<你的 api key>
WEBSEARCH_MODEL=<模型名称，如 deepseek-v4-flash>
```

后端 url 参考：

- DeepSeek 官方：https://api.deepseek.com/v1
- OpenCode Go：https://opencode.ai/zen/go/v1

> 注：Claude Code 如果使用 DeepSeek 官方 API，内置的 WebSearch 已支持联网搜索，可不用本 MCP。
````
