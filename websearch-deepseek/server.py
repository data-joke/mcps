#!/usr/bin/env python3
"""
MCP WebSearch DeepSeek —— 联网搜索工具（支持 DeepSeek 官方 / OpenCode Go 双后端）

背景（为什么需要这个 MCP）：
    Claude Code 的内置 WebSearch 走 Anthropic 协议的「服务端工具」机制，而很多
    中转（如 OpenCode Go 给 Claude Code 开的 chat completions 门）没有"服务端帮我搜"
    的概念，翻译时会把 web_search 工具丢掉 → 搜不了。

    本 MCP 绕开这一层：它自己直接对后端的 Responses 端点（openai-responses 协议，
    原生支持服务端 web_search）发请求，把搜索结果带回来。搜索由后端服务端执行。

双后端支持：
    只要后端提供 OpenAI Responses 协议 + 服务端 web_search 工具，换 BASE_URL +
    API_KEY 即可切换，代码零改动：
      - DeepSeek 官方：BASE_URL=https://api.deepseek.com/v1
      - OpenCode Go：  BASE_URL=https://opencode.ai/zen/go/v1

环境变量（也可放本文件同目录的 .env 文件）:
    WEBSEARCH_API_KEY     必填，后端 API Key
    WEBSEARCH_BASE_URL    可选，默认 https://opencode.ai/zen/go/v1
    WEBSEARCH_MODEL       可选，默认 deepseek-v4-flash（便宜、适合搜索）
    WEBSEARCH_MAX_TOKENS  可选，默认 4096
    WEBSEARCH_TIMEOUT     可选，默认 60 秒
"""

import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# 加载 server.py 同目录的 .env（MCP 由 Claude Code 从任意目录启动，不能用相对路径）
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

mcp = FastMCP("websearch-deepseek")

# ---------- 配置常量 ----------
API_KEY = os.getenv("WEBSEARCH_API_KEY")
BASE_URL = os.getenv("WEBSEARCH_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
MODEL = os.getenv("WEBSEARCH_MODEL", "deepseek-v4-flash")
MAX_TOKENS = int(os.getenv("WEBSEARCH_MAX_TOKENS", "4096"))
HTTP_TIMEOUT = float(os.getenv("WEBSEARCH_TIMEOUT", "60"))


def _extract_final_text(output: list) -> str:
    """从 Responses API 的 output 数组里取最终回答文本（最后一个 message 的 output_text）。"""
    for block in reversed(output):
        if block.get("type") == "message":
            for content in block.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "").strip()
    return ""


def _extract_search_activity(output: list) -> list[str]:
    """收集搜索动作（搜索关键词 + 打开的页面 URL），用于展示搜索过程。"""
    activity: list[str] = []
    for block in output:
        if block.get("type") != "web_search_call":
            continue
        action = block.get("action", {})
        kind = action.get("type")
        if kind == "search":
            # queries 里混有内部调用 id（ws_call_id=xxx），过滤掉
            for q in action.get("queries", []):
                if isinstance(q, str) and not q.startswith("ws_call_id="):
                    activity.append(f"[搜索] {q}")
        elif kind == "open_page":
            activity.append(f"[打开] {action.get('url', '')}")
    return activity


@mcp.tool()
async def web_search(query: str, model: str = "") -> str:
    """联网搜索，返回带来源的答案（服务端原生搜索）。

    Args:
        query: 要搜索的问题或关键词
        model: 可选，覆盖默认模型（如 deepseek-v4-pro），留空则用 .env 里的默认值
    """
    if not API_KEY:
        return "错误：未配置 WEBSEARCH_API_KEY，请在 server.py 同目录的 .env 里填写"

    payload = {
        "model": model or MODEL,
        "input": query,
        "tools": [{"type": "web_search"}],  # 服务端原生联网搜索工具
        "max_output_tokens": MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    # trust_env=False：强制直连，避免读取系统/Clash 残留代理导致 Connection error
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, trust_env=False) as client:
            resp = await client.post(f"{BASE_URL}/responses", json=payload, headers=headers)
    except httpx.HTTPError as e:
        return f"错误：请求后端失败 — {e}"

    if resp.status_code != 200:
        return f"错误：搜索失败 HTTP {resp.status_code} — {resp.text[:500]}"

    output = resp.json().get("output", [])
    answer = _extract_final_text(output)
    activity = _extract_search_activity(output)

    parts: list[str] = []
    if activity:
        parts.append("【搜索过程】")
        parts.extend(f"  · {a}" for a in activity[:10])
        parts.append("")
    parts.append("【结果】")
    parts.append(answer or "（未返回文本内容）")
    return "\n".join(parts)


if __name__ == "__main__":
    # 注意：MCP 走 stdio 协议，stdout 是协议通道，严禁 print！
    # 所有诊断信息一律输出到 stderr。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if API_KEY:
        print(f"[websearch-deepseek] 配置OK，模型: {MODEL}，后端: {BASE_URL}", file=sys.stderr)
    else:
        print("[websearch-deepseek] 警告：未配置 WEBSEARCH_API_KEY", file=sys.stderr)

    mcp.run()
