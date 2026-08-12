#!/usr/bin/env python3
"""
MCP Image Recognizer - 图片识别MCP工具
支持国内各大厂商模型（Kimi、通义千问、MiniMax、智谱等）

配置环境变量（也可放 .env 文件）:
    VISION_MODEL_BASE_URL      必填，模型API地址
    VISION_MODEL_API_KEY       必填，API密钥
    VISION_MODEL_NAME          必填，模型名称
    VISION_MODEL_MAX_TOKENS    可选，输出最大token，默认4096
    VISION_BATCH_CONCURRENCY   可选，批量识别并发数，默认4
"""

import asyncio
import base64
import binascii
import os
from pathlib import Path
from typing import Optional, Tuple

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from openai import AsyncOpenAI

# 加载 server.py 同目录的 .env 文件
# （MCP server 由 Claude Code 从任意工作目录启动，不能用默认的相对路径）
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

mcp = FastMCP("image-recognizer")

# ---------- 配置常量 ----------
ENV_BASE_URL = "VISION_MODEL_BASE_URL"
ENV_API_KEY = "VISION_MODEL_API_KEY"
ENV_MODEL = "VISION_MODEL_NAME"
ENV_MAX_TOKENS = "VISION_MODEL_MAX_TOKENS"

BATCH_CONCURRENCY = int(os.getenv("VISION_BATCH_CONCURRENCY", "4"))
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB，防止下载超大文件
HTTP_TIMEOUT = 30.0
VISION_RETRY = int(os.getenv("VISION_RETRY", "1"))  # 视觉拒绝时重试次数

# 详细程度 -> 提示词模板
DETAIL_PREFIXES = {
    "brief": "简要描述这张图片: {prompt}",
    "detailed": "详细描述这张图片的所有内容: {prompt}",
    "comprehensive": (
        "全面分析这张图片，包括：1)整体场景 2)主要对象 3)文字内容 "
        "4)颜色和构图 5)其他细节。额外说明: {prompt}"
    ),
}

# 本地文件扩展名 -> MIME 类型
MEDIA_TYPE_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


# ---------- 配置与客户端（懒加载单例，复用连接池） ----------

_client: Optional[AsyncOpenAI] = None
_http_client: Optional[httpx.AsyncClient] = None


def get_model_config() -> dict:
    """从环境变量读取模型配置"""
    return {
        "base_url": os.getenv(ENV_BASE_URL),
        "api_key": os.getenv(ENV_API_KEY),
        "model": os.getenv(ENV_MODEL),
    }


def validate_config() -> None:
    """校验模型配置是否完整，缺失则抛出明确错误"""
    missing = [k for k, v in get_model_config().items() if not v]
    if missing:
        raise ValueError(
            f"缺少必要环境变量: {', '.join(missing)}。"
            "请设置 VISION_MODEL_BASE_URL / VISION_MODEL_API_KEY / VISION_MODEL_NAME。"
        )


def get_client() -> AsyncOpenAI:
    """获取 OpenAI 兼容异步客户端（懒加载单例）"""
    global _client
    if _client is None:
        validate_config()
        config = get_model_config()
        _client = AsyncOpenAI(
            base_url=config["base_url"],
            api_key=config["api_key"],
            timeout=HTTP_TIMEOUT,
        )
    return _client


async def get_http_client() -> httpx.AsyncClient:
    """获取共享 HTTP 客户端（复用连接池）"""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, follow_redirects=True
        )
    return _http_client


def _max_tokens() -> int:
    return int(os.getenv(ENV_MAX_TOKENS, "4096"))


# ---------- 图片加载 ----------


def _check_size(size: int, source: str) -> None:
    """校验图片大小，超限则拒绝"""
    if size > MAX_IMAGE_SIZE:
        raise ValueError(
            f"图片过大（{size / 1024 / 1024:.1f}MB > {MAX_IMAGE_SIZE / 1024 / 1024:.0f}MB）"
            f"来源: {source[:80]}"
        )


async def load_image_as_base64(source: str) -> Tuple[str, str]:
    """
    加载图片并转换为 base64

    支持三种来源:
    - URL（http:// 或 https://）
    - 本地文件路径
    - base64 编码数据（含 data URI 前缀）

    Returns:
        (base64数据, MIME类型)
    """
    if source.startswith(("http://", "https://")):
        # URL：复用连接池，带超时，校验大小
        client = await get_http_client()
        response = await client.get(source)
        response.raise_for_status()

        # 若服务端声明了长度，先拦截过大文件
        declared = response.headers.get("content-length")
        if declared and int(declared) > MAX_IMAGE_SIZE:
            raise ValueError(f"图片过大: {source[:80]}")

        content = response.content
        _check_size(len(content), source)
        media_type = (
            response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            or "image/jpeg"
        )

    elif os.path.isfile(source):
        # 本地文件
        _check_size(os.path.getsize(source), source)
        with open(source, "rb") as f:
            content = f.read()
        media_type = MEDIA_TYPE_MAP.get(Path(source).suffix.lower(), "image/jpeg")

    else:
        # base64（兼容 data:image/png;base64,xxxx 前缀）
        b64_source = source
        media_type = "image/jpeg"
        if source.startswith("data:"):
            header, _, b64_source = source.partition(",")
            media_type = header[len("data:"):].split(";")[0] or "image/jpeg"

        try:
            content = base64.b64decode(b64_source, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(
                f"无法识别的图片来源: {source[:80]}...\n"
                "支持的格式: URL、本地文件路径、base64编码"
            )

    return base64.b64encode(content).decode("utf-8"), media_type


# ---------- 识别逻辑 ----------


def _extract_content(msg) -> str:
    """提取模型回答。

    兼容推理模型（如 mimo-v2.5）：content 可能为空，推理过程在 reasoning_content。
    优先返回最终答案 content，为空时降级为推理内容。
    """
    content = getattr(msg, "content", None) or ""
    if content.strip():
        return content.strip()

    reasoning = getattr(msg, "reasoning_content", None) or ""
    if reasoning.strip():
        return f"[推理过程]\n{reasoning.strip()}"

    return "（模型未返回有效内容）"


async def chat_with_image(b64_data: str, media_type: str, prompt: str) -> str:
    """调用多模态模型识别单张图片"""
    response = await get_client().chat.completions.create(
        model=get_model_config()["model"],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{b64_data}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        max_tokens=_max_tokens(),
    )
    return _extract_content(response.choices[0].message)


async def chat_text(prompt: str) -> str:
    """调用模型进行纯文本对话（不带图片，用于总结等场景）"""
    response = await get_client().chat.completions.create(
        model=get_model_config()["model"],
        messages=[{"role": "user", "content": prompt}],
        max_tokens=_max_tokens(),
    )
    return _extract_content(response.choices[0].message)


# 视觉拒绝关键词：模型偶发会声称"看不到图片"，检测到则触发重试
VISION_REFUSAL_KEYWORDS = [
    "无法查看", "无法识别", "不能识别", "看不到", "没有视觉",
    "无法处理图像", "无法处理图片", "不能查看", "无法直接查看",
    "cannot see", "no visual", "can't see",
]


def _is_vision_refusal(text: str) -> bool:
    """检测模型是否拒绝了视觉输入"""
    return any(k in text for k in VISION_REFUSAL_KEYWORDS)


async def recognize_single_image(image_source: str, prompt: str, detail_level: str) -> str:
    """识别单张图片（视觉拒绝时自动重试）"""
    validate_config()

    if detail_level not in DETAIL_PREFIXES:
        raise ValueError(
            f"detail_level 取值无效: {detail_level}，可选: {', '.join(DETAIL_PREFIXES)}"
        )

    b64_data, media_type = await load_image_as_base64(image_source)
    full_prompt = DETAIL_PREFIXES[detail_level].format(prompt=prompt)

    result = ""
    for attempt in range(VISION_RETRY + 1):
        result = await chat_with_image(b64_data, media_type, full_prompt)
        # 若模型偶发拒绝视觉，重试（新请求可绕开模型的非确定性拒绝）
        if _is_vision_refusal(result) and attempt < VISION_RETRY:
            continue
        break

    if _is_vision_refusal(result):
        return f"（模型未正确接收图片，已重试 {VISION_RETRY} 次仍失败）\n{result}"
    return result


# ---------- MCP 工具 ----------


@mcp.tool()
async def recognize_image(
    image: str,
    prompt: str = "请描述这张图片的内容",
    detail_level: str = "detailed",
) -> str:
    """
    识别单张图片的内容

    Args:
        image: 图片来源，支持 URL（http/https）、本地文件路径、base64编码
        prompt: 识别提示词，指导AI如何描述图片
        detail_level: 详细程度 brief / detailed / comprehensive

    Returns:
        图片内容的文本描述
    """
    try:
        return await recognize_single_image(image, prompt, detail_level)
    except Exception as e:
        return f"图片识别失败: {e}"


@mcp.tool()
async def recognize_images_batch(
    images: list[str],
    prompt: str = "请描述这些图片的内容",
    detail_level: str = "detailed",
    output_format: str = "combined",
) -> str:
    """
    批量识别多张图片（并发处理）

    Args:
        images: 图片来源列表（URL / 本地文件路径 / base64）
        prompt: 识别提示词
        detail_level: 详细程度 brief / detailed / comprehensive
        output_format: combined（合并） / separate（分开） / summary（综合总结）

    Returns:
        所有图片的文本描述
    """
    if not images:
        return "未提供任何图片"

    # 并发识别，信号量限制并发数避免打爆 API
    semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def _recognize(i: int, img: str) -> Tuple[int, str]:
        async with semaphore:
            try:
                result = await recognize_single_image(img, prompt, detail_level)
                return i, result
            except Exception as e:
                return i, f"识别失败: {e}"

    entries = await asyncio.gather(
        *(_recognize(i, img) for i, img in enumerate(images, 1))
    )

    results = [(i, t) for i, t in entries if not t.startswith("识别失败")]
    errors = [(i, t) for i, t in entries if t.startswith("识别失败")]

    def _format(entry: Tuple[int, str]) -> str:
        i, text = entry
        return f"【图片 {i}】\n{text}"

    if output_format == "separate":
        return "\n\n".join(_format(e) for e in entries)

    if output_format == "summary":
        if not results:
            return "\n\n".join(_format(e) for e in errors)
        detail_text = "\n\n".join(_format(e) for e in results)
        try:
            summary = await chat_text(f"请总结以下多张图片的内容：\n\n{detail_text}")
        except Exception as e:
            summary = f"（总结失败: {e}）"
        return f"【综合总结】\n{summary}\n\n【详细内容】\n{detail_text}"

    # combined（默认）
    return "\n\n".join(_format(e) for e in entries)


@mcp.tool()
async def get_supported_formats() -> str:
    """
    获取支持的图片格式和配置说明

    Returns:
        支持的格式和使用说明
    """
    return """
支持的图片来源格式:
1. URL - 以 http:// 或 https:// 开头的图片链接
2. 本地文件路径 - 支持绝对路径和相对路径
   例如: /path/to/image.jpg 或 ./images/photo.png
3. Base64编码 - 直接传递编码后的图片数据

支持的图片格式:
- JPEG (.jpg, .jpeg)
- PNG (.png)
- GIF (.gif)
- WebP (.webp)
- BMP (.bmp)
- TIFF (.tiff, .tif)

环境变量配置:
VISION_MODEL_BASE_URL      模型API的基础URL（必填）
VISION_MODEL_API_KEY       API密钥（必填）
VISION_MODEL_NAME          模型名称（必填）
VISION_MODEL_MAX_TOKENS    输出最大token（可选，默认4096）
VISION_BATCH_CONCURRENCY   批量识别并发数（可选，默认4）

示例配置（Kimi）:
export VISION_MODEL_BASE_URL="https://api.moonshot.cn/v1"
export VISION_MODEL_API_KEY="your-api-key"
export VISION_MODEL_NAME="moonshot-v1-8k-vision"

示例配置（通义千问）:
export VISION_MODEL_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export VISION_MODEL_API_KEY="your-api-key"
export VISION_MODEL_NAME="qwen-vl-plus"
"""


if __name__ == "__main__":
    # 注意：MCP 走 stdio 协议，stdout 是协议通道，严禁 print！
    # 所有诊断信息一律输出到 stderr。
    import sys

    # 压低第三方库（httpx/openai）的请求日志，避免刷屏
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    try:
        validate_config()
        print(
            f"[image-recognizer] 配置OK，模型: {get_model_config()['model']}",
            file=sys.stderr,
        )
    except ValueError as e:
        print(f"[image-recognizer] {e}", file=sys.stderr)

    # 运行MCP服务器（stdio 传输）
    mcp.run()
