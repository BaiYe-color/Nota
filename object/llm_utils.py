"""LLM 调用共享工具：重试、token 估算、输入/输出检查、客户端工厂、markdown 代码块剥离。"""
import os
import random
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from openai import APIConnectionError, APITimeoutError, RateLimitError, InternalServerError

# 幂等：每个使用 env 的模块自己 load 一次，不假设主入口已加载
load_dotenv(Path(__file__).parent / ".env")

VISION_MODEL = os.getenv("VISION_MODEL", "gpt-5.4")
GPT_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://yapi.click/v1")

# 三档模型分工：
# - cards_node：结构化 JSON 生成
# - outline：章节大纲 JSON
# - chapter writer：单章 Markdown 撰写（图片路径 / 公式格式关键）
# 默认 cards + chapter writer 走 GPT（图片路径引用更稳）；outline 用 DeepSeek 便宜
_GPT_DEFAULT = os.getenv("GPT_MODEL", "gpt-5.4")
_DEEPSEEK_DEFAULT = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

CARDS_MODEL = os.getenv("CARDS_MODEL", _GPT_DEFAULT)
OUTLINE_MODEL = os.getenv("OUTLINE_MODEL", _DEEPSEEK_DEFAULT)
WRITER_MODEL = os.getenv("WRITER_MODEL", _GPT_DEFAULT)


# ---------- 客户端工厂 ----------
# 单次调用超时 300s——GPT-5 / reasoning 系模型偶尔单章会跑 60s+，留足余量但仍避免永久挂起
_HTTP_TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "300"))


def vision_client() -> OpenAI:
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=GPT_BASE_URL, timeout=_HTTP_TIMEOUT)


def gpt_client() -> OpenAI:
    """走 yapi.click 的 OpenAI 兼容端点，用 OPENAI_API_KEY。"""
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=GPT_BASE_URL, timeout=_HTTP_TIMEOUT)


def text_client() -> OpenAI:
    """走 DeepSeek 官方端点。"""
    return OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com", timeout=_HTTP_TIMEOUT)


def client_for_model(model: str) -> OpenAI:
    """按 model 名字自动路由到对应 client。约定：deepseek-* 走 DeepSeek，其它走 GPT。"""
    return text_client() if model.startswith("deepseek") else gpt_client()


# ---------- 重试 ----------
_RETRYABLE_EXC = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)


def call_with_retry(fn, *, label: str, max_attempts: int = 3, base_delay: float = 1.5):
    """指数退避重试。可重试异常：连接错误、超时、429、5xx；其他异常直接抛。"""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except _RETRYABLE_EXC as e:
            if attempt == max_attempts:
                print(f"    [retry {label}] 第 {attempt}/{max_attempts} 次失败，放弃: {type(e).__name__}: {str(e)[:120]}")
                raise
            delay = base_delay * (3 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"    [retry {label}] 第 {attempt}/{max_attempts} 次失败，{delay:.1f}s 后重试: {type(e).__name__}: {str(e)[:120]}")
            time.sleep(delay)
    raise RuntimeError("unreachable")


# ---------- 上下文预算 ----------
# DeepSeek deepseek-chat 上下文 64K。给输出预留 16K，输入警戒线定 40K，abort 线定 55K
INPUT_TOKEN_WARN = 40000
INPUT_TOKEN_ABORT = 55000


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文 1 字/token，英文 4 字符/token。"""
    cjk = sum(1 for c in text if '一' <= c <= '鿿')
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def check_input_size(prompt: str, node: str) -> None:
    est = estimate_tokens(prompt)
    if est >= INPUT_TOKEN_ABORT:
        raise RuntimeError(
            f"[{node}] 输入过大：约 {est} tokens ≥ {INPUT_TOKEN_ABORT}。"
            f"DeepSeek deepseek-chat 上下文 64K，无法处理该长度。请缩短输入或分批处理。"
        )
    if est >= INPUT_TOKEN_WARN:
        print(f"[{node}] 警告：输入约 {est} tokens（阈值 {INPUT_TOKEN_WARN}），接近 context 上限，模型输出可能不完整")


def check_output_finish(resp, node: str) -> None:
    """检测输出是否被 max_tokens 截断。截断时抛异常防止半截结果被缓存。"""
    try:
        reason = resp.choices[0].finish_reason
    except (AttributeError, IndexError):
        return
    if reason == "length":
        usage = getattr(resp, "usage", None)
        used = usage.completion_tokens if usage else "?"
        raise RuntimeError(
            f"[{node}] 输出被 max_tokens 截断（completion_tokens={used}）——"
            f"半截结果未写入缓存。请提高 max_tokens 或缩短输入后重跑"
        )


# ---------- 输出清理 ----------
def atomic_write_text(path, content: str, encoding: str = "utf-8") -> None:
    """先写 .tmp 再 os.replace，避免 Ctrl-C / 崩溃留下半截文件被下次运行误读为缓存。"""
    import os
    from pathlib import Path
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content, encoding=encoding)
    os.replace(tmp, p)


def build_chat_kwargs(model: str, *, temperature: float, max_tokens: int, json_mode: bool = False) -> dict:
    """按 model 兼容性构造 chat.completions.create 的关键参数。

    GPT-5 系列不接受非默认 temperature 且用 max_completion_tokens；其余模型走标准 max_tokens/temperature。
    """
    is_gpt5 = model.startswith("gpt-5")
    kwargs: dict = {"model": model}
    if is_gpt5:
        kwargs["max_completion_tokens"] = max_tokens
        # gpt-5 仅支持默认 temperature=1，传其它值会 400
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = temperature
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return kwargs


def strip_code_fence(text: str) -> str:
    """剥掉整段被 ```lang ... ``` 包裹的外壳；保留内部的代码块。

    只有当 text 整体形如 `\\`\\`\\`lang\\n...\\n\\`\\`\\`` 时才剥（首尾均为 fence）。
    否则原样返回，避免误伤"文本里恰好有 ``` 的正常内容"。
    """
    t = text.strip()
    if not t.startswith("```") or not t.endswith("```"):
        return t
    # 掐头：去掉首行 ```lang
    first_nl = t.find("\n")
    if first_nl == -1:
        return t  # 单行 ``` 异常，原样返回
    lang_line = t[3:first_nl].strip()  # ```json / ```markdown / 空
    body_start = first_nl + 1
    # 去尾：从字符串末尾找最后一个 ``` 位置
    body_end = t.rfind("```")
    if body_end <= body_start:
        return t  # 找不到闭合 fence
    body = t[body_start:body_end].rstrip()
    # lang_line 只允许纯字母数字或空——避免"``` 后跟乱七八糟内容"的误剥
    if lang_line and not lang_line.isalnum():
        return t
    return body
