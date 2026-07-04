"""自然语言 agent —— 把用户的自由文本路由成一次命令调用, 复用 telegram_commands 的能力层。

为什么用「单跳意图路由」而非原生 function-calling:
  - AI 层要同时兼容任意 OpenAI 兼容中转站(gpt-5.5 等)与 Codex CLI(只读沙盒, 无 function API),
    原生多轮 tool-calling 无法在两端都可靠工作。
  - 单跳路由: 把「能力目录 + 用户文本」交给模型, 让它回一个小 JSON {tool,args} 或 {reply}。
    命中命令 → 执行同一处理器(与 /screen 等零重复); 只是闲聊/能直接答 → 回 reply。
  - 任何模型都能用(不依赖 function-calling API), Codex 也能跑; 处理器完全复用。
    将来要升级成自主多步 agent, 只替换本文件的路由逻辑, COMMANDS 处理器不动。

设计: 路由失败 / AI 未配置 / JSON 解析失败 → 静默降级为「无法理解, 请用 /help」提示,
     绝不抛异常(轮询循环不能因单条消息崩)。
"""
from __future__ import annotations

import json
import logging
import re

from app.services.telegram_commands import COMMANDS, CommandContext, dispatch_command

logger = logging.getLogger(__name__)


def _build_catalog() -> str:
    """把 COMMANDS 表渲染成给模型看的能力目录(名字 / 描述 / 参数语义)。"""
    lines: list[str] = []
    for c in COMMANDS.values():
        arg = f" | 参数: {c.args_hint}" if c.args_hint else " | 无参数"
        kind = "写操作" if c.write else "读操作"
        lines.append(f"- {c.name} ({kind}): {c.description}{arg}")
    return "\n".join(lines)


_SYSTEM_PROMPT = """你是 A 股量化面板的指令路由器。用户会用中文自然语言下达指令或提问。
你的唯一任务: 把用户输入映射到下面的「能力目录」中最合适的一个, 并抽取其文本参数。

能力目录:
{catalog}

规则:
1. 只返回一个 JSON 对象, 不要任何解释、markdown 代码块或多余文字。
2. 命中某个能力时返回: {{"tool": "<能力名>", "args": "<抽取的参数文本>"}}
   - args 是要传给该能力的原始文本(如股票代码、策略id、关注点、on/off、数量), 无参数则用空字符串。
   - 股票用户可能说名字(如「茅台」), 若你知道对应代码就填代码(如 600519), 不确定就填用户原话。
3. 用户只是闲聊、问你做不到的事、或能力目录里没有对应项时, 返回:
   {{"reply": "<简短中文回复, 说明能做什么或引导用户>"}}
4. 拿不准时优先选最接近的读操作能力, 不要臆造写操作。

示例:
用户: 看看贵州茅台怎么样 → {{"tool": "analyze", "args": "600519"}}
用户: 跑一下趋势突破前十 → {{"tool": "screen", "args": "trend_breakout 10"}}
用户: 把宁德时代加自选 → {{"tool": "add", "args": "300750"}}
用户: 今天大盘如何 → {{"tool": "overview", "args": ""}}
用户: 帮我复盘一下 → {{"tool": "recap", "args": ""}}
用户: 关掉实时行情 → {{"tool": "realtime", "args": "off"}}
用户: 你好 → {{"reply": "我是 TickFlow 量化助手, 可以帮你选股、分析个股、复盘大盘、管理自选。发 /help 看全部指令。"}}
"""


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个 JSON 对象。容忍 ```json 包裹与前后噪声。"""
    if not text:
        return None
    s = text.strip()
    # 去掉可能的 markdown 代码围栏
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    # 回退: 抓第一个 { ... } 片段
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


async def route_and_run(ctx: CommandContext, text: str) -> str:
    """把自然语言文本路由到某个命令并执行, 返回给用户的消息。

    流程: 构建能力目录 → LLM 返回 {tool,args} 或 {reply} → 命中则复用命令处理器。
    AI 未配置 / 失败 → 友好降级提示。
    """
    from app.services.ai_provider import ai_configured, generate_ai_text

    text = (text or "").strip()
    if not text:
        return "请说点什么, 或发 /help 查看指令。"

    if not ai_configured():
        return (
            "未配置 AI, 无法理解自然语言指令。\n"
            "可在「设置 → AI」配置后再用自然语言, 或直接用 /help 里的结构化命令。"
        )

    system = _SYSTEM_PROMPT.format(catalog=_build_catalog())
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]
    try:
        raw = await generate_ai_text(
            messages,
            temperature=0.0,
            max_tokens=400,
            timeout=60.0,
            live_search=False,  # 路由本身不需要联网检索
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("NL 路由 AI 调用失败: %s", e)
        return f"⚠️ 指令理解失败: {e}\n可改用 /help 里的结构化命令。"

    obj = _extract_json(raw)
    if obj is None:
        logger.debug("NL 路由未解析出 JSON, 原文: %s", raw[:200])
        return "没太理解这条指令, 换个说法或用 /help 查看可用命令。"

    # 直接回复(闲聊 / 无对应能力)
    if "reply" in obj and "tool" not in obj:
        return str(obj.get("reply") or "").strip() or "发 /help 看看我能做什么。"

    tool = str(obj.get("tool") or "").strip().lstrip("/")
    args = str(obj.get("args") or "").strip()
    if not tool or tool not in COMMANDS:
        reply = str(obj.get("reply") or "").strip()
        return reply or "没太理解这条指令, 换个说法或用 /help 查看可用命令。"

    result = await dispatch_command(ctx, tool, args)
    if result is None:
        return "没太理解这条指令, 换个说法或用 /help 查看可用命令。"
    return result
