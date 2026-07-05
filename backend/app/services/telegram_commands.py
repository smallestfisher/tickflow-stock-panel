"""Telegram 机器人命令注册表 + 处理器 —— 结构化命令与自然语言 agent 的共享能力层。

设计要点:
  - 每个「能力」= 一个 Command(name/description/usage/handler/write)。
  - 结构化命令(`/screen trend_breakout 10`)与 NL agent(「帮我跑一下趋势突破前十」)
    走同一张 COMMANDS 表:agent 只是把自然语言路由成 (command, args), 复用同一处理器,
    零逻辑重复。将来换成原生 function-calling 也只需替换路由层, 处理器不动。
  - 处理器在 FastAPI 请求之外运行, 只依赖注入的 app.state(不碰 HTTP / 认证中间件)。
    授权边界由 telegram_bot 的 chat_id 白名单负责, 不在此层。
  - 每个处理器自行 try/except, 永远返回给用户可读的字符串, 不向上抛(轮询循环不能因单条命令崩)。

处理器签名: `async def handler(ctx: CommandContext, args: str) -> str`
  返回 Telegram 消息正文(纯文本 / 少量 HTML)。ctx.app_state 提供 repo / 各服务单例。
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)


# ================================================================
# 命令上下文 + 注册模型
# ================================================================

@dataclass
class CommandContext:
    """处理器运行上下文 —— 承载单例引用, 避免每个处理器都从 app_state 里翻找。"""
    app_state: object

    @property
    def repo(self):
        return getattr(self.app_state, "repo", None)

    @property
    def data_dir(self):
        repo = self.repo
        return repo.store.data_dir if repo is not None else None

    @property
    def quote_service(self):
        return getattr(self.app_state, "quote_service", None)

    @property
    def depth_service(self):
        return getattr(self.app_state, "depth_service", None)

    @property
    def strategy_engine(self):
        return getattr(self.app_state, "strategy_engine", None)

    @property
    def capabilities(self):
        return getattr(self.app_state, "capabilities", None)


Handler = Callable[[CommandContext, str], Awaitable[str]]


@dataclass
class Command:
    name: str
    description: str          # 供 /help 与 agent 目录使用(简明动作描述)
    handler: Handler
    usage: str = ""           # 参数格式, 如 "<symbol> [focus]"
    write: bool = False       # 是否为写操作(改设置/加自选/触发同步等)
    args_hint: str = ""       # 供 agent 理解 args 语义(自然语言路由用)


COMMANDS: dict[str, Command] = {}


def command(
    name: str,
    description: str,
    *,
    usage: str = "",
    write: bool = False,
    args_hint: str = "",
) -> Callable[[Handler], Handler]:
    """注册一个命令处理器到全局表。"""
    def deco(fn: Handler) -> Handler:
        COMMANDS[name] = Command(
            name=name,
            description=description,
            handler=fn,
            usage=usage,
            write=write,
            args_hint=args_hint,
        )
        return fn
    return deco


# ================================================================
# 公共辅助
# ================================================================

def normalize_symbol(raw: str) -> str:
    """把用户输入的裸代码补全交易所后缀。已带后缀则原样(大写)返回。

    600519 → 600519.SH; 000001 → 000001.SZ; 300750 → 300750.SZ;
    430/830/920 → .BJ。无法判定时默认 .SZ(深市, 与 free_adapter 兜底一致)。
    """
    s = (raw or "").strip().upper()
    if not s:
        return ""
    if "." in s:
        return s
    if not s.isdigit():
        return s  # 交给下游报错, 不猜
    if s.startswith(("60", "68", "51", "58")):  # 沪市主板/科创板/沪市ETF
        return f"{s}.SH"
    if s.startswith(("430", "830", "920", "870", "871", "872")):  # 北交所
        return f"{s}.BJ"
    return f"{s}.SZ"  # 深市主板/创业板/深市ETF/兜底


def _latest_date(ctx: CommandContext) -> date | None:
    """当前 enriched 最新数据日。"""
    from app.services.screener import ScreenerService
    repo = ctx.repo
    if repo is None:
        return None
    try:
        return ScreenerService(repo).latest_date()
    except Exception as e:  # noqa: BLE001
        logger.debug("latest_date 失败: %s", e)
        return None


def _name_map(ctx: CommandContext, symbols: list[str]) -> dict[str, str]:
    """批量取 symbol → name(从 instruments 维表)。缺失返回空 dict。"""
    repo = ctx.repo
    if repo is None or not symbols:
        return {}
    try:
        df = repo.get_instruments()
        if df.is_empty() or "symbol" not in df.columns or "name" not in df.columns:
            return {}
        sel = df.filter(df["symbol"].is_in(symbols)).select(["symbol", "name"])
        return {row["symbol"]: row["name"] for row in sel.iter_rows(named=True) if row.get("name")}
    except Exception as e:  # noqa: BLE001
        logger.debug("name_map 失败: %s", e)
        return {}


def _fmt_num(v, digits: int = 2) -> str:
    """数值格式化, None/非数 → '-'。"""
    try:
        f = float(v)
        return f"{f:.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_pct(v) -> str:
    try:
        f = float(v)
        sign = "+" if f > 0 else ""
        return f"{sign}{f:.2f}%"
    except (TypeError, ValueError):
        return "-"


async def _collect_ndjson_stream(agen: AsyncIterator[str]) -> tuple[str, str]:
    """把 stock_analyzer / market_recap 的 NDJSON 流收全为一段文本。

    返回 (summary, body):
      - summary: meta 事件里的摘要(价位摘要 / 复盘摘要), 可能为空
      - body:    所有 delta 拼接的正文
      - 遇 error 事件: body 置为错误说明。
    """
    summary = ""
    parts: list[str] = []
    async for line in agen:
        line = (line or "").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        t = obj.get("type")
        if t == "meta":
            summary = str(obj.get("summary") or "")
        elif t == "delta":
            parts.append(str(obj.get("content") or ""))
        elif t == "error":
            return "", f"⚠️ {obj.get('message') or '分析失败'}"
        # done: 无操作
    return summary, "".join(parts).strip()


def _parse_bool_arg(args: str) -> bool | None:
    """把 on/off/开/关/true/false/1/0 解析为布尔。无法解析返回 None。"""
    s = (args or "").strip().lower()
    if s in ("on", "开", "true", "1", "启用", "打开"):
        return True
    if s in ("off", "关", "false", "0", "停用", "关闭"):
        return False
    return None


# ================================================================
# 读命令
# ================================================================

@command("help", "显示所有可用命令及用法")
async def _cmd_help(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    lines = ["<b>TickFlow 机器人 · 命令列表</b>", ""]
    read_cmds = [c for c in COMMANDS.values() if not c.write]
    write_cmds = [c for c in COMMANDS.values() if c.write]
    lines.append("📊 <b>查询</b>")
    for c in read_cmds:
        usage = f" {c.usage}" if c.usage else ""
        lines.append(f"/{c.name}{usage} — {c.description}")
    lines.append("")
    lines.append("✏️ <b>操作</b>")
    for c in write_cmds:
        usage = f" {c.usage}" if c.usage else ""
        lines.append(f"/{c.name}{usage} — {c.description}")
    lines.append("")
    lines.append("💬 也可直接用自然语言下达指令, 例如「看看贵州茅台」「跑一下趋势突破前十」。")
    return "\n".join(lines)


@command("status", "系统状态: 运行档位 / 实时行情 / 最新数据日 / 今日告警数")
async def _cmd_status(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    from app.services import preferences
    from app.tickflow import client as tf_client
    from app.tickflow.policy import tier_label

    lines = ["<b>系统状态</b>"]
    try:
        lines.append(f"运行模式: {tf_client.current_mode()} · 档位: {tier_label()}")
    except Exception:  # noqa: BLE001
        pass

    qs = ctx.quote_service
    if qs is not None:
        try:
            rt = "开" if preferences.get_realtime_quotes_enabled() else "关"
            allowed = "可用" if qs.is_realtime_allowed() else "当前档位不支持"
            lines.append(f"实时行情: {rt}({allowed})")
        except Exception:  # noqa: BLE001
            pass

    d = _latest_date(ctx)
    lines.append(f"最新数据日: {d.isoformat() if d else '暂无(请先 /sync)'}")

    # 今日告警数
    try:
        from app.services import alert_store
        if ctx.data_dir is not None:
            today_alerts = alert_store.list_recent(ctx.data_dir, days=1, limit=500)
            lines.append(f"近 24h 告警: {len(today_alerts)} 条")
    except Exception:  # noqa: BLE001
        pass

    try:
        from app import secrets_store
        lines.append(f"联网检索: {'开' if secrets_store.get_ai_live_search() else '关'}")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


@command("watchlist", "查看自选股列表")
async def _cmd_watchlist(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    from app.services import watchlist
    rows = watchlist.list_symbols()
    if not rows:
        return "自选股为空。用 /add &lt;代码&gt; 添加。"
    symbols = [str(r["symbol"]) for r in rows if r.get("symbol")]
    names = _name_map(ctx, symbols)
    lines = [f"<b>自选股 ({len(rows)})</b>"]
    for r in rows:
        sym = r.get("symbol") or ""
        nm = names.get(sym, "")
        note = r.get("note") or ""
        note_s = f" · {note}" if note else ""
        lines.append(f"{sym} {nm}{note_s}")
    return "\n".join(lines)


@command("strategies", "列出所有可用策略(内置 + 自定义 + AI 生成)")
async def _cmd_strategies(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    engine = ctx.strategy_engine
    if engine is None:
        return "策略引擎未就绪。"
    strategies = engine.list_strategies()
    if not strategies:
        return "暂无策略。"
    lines = [f"<b>策略 ({len(strategies)})</b>"]
    for s in strategies:
        sid = s.get("id") or ""
        name = s.get("name") or sid
        lines.append(f"<code>{sid}</code> — {name}")
    lines.append("")
    lines.append("用 /screen &lt;策略id&gt; [数量] 运行选股。")
    return "\n".join(lines)


@command(
    "screen", "运行指定策略选股, 返回命中的股票",
    usage="<策略id> [数量]",
    args_hint="策略id(见 /strategies), 可选返回条数(默认 10)",
)
async def _cmd_screen(ctx: CommandContext, args: str) -> str:
    engine = ctx.strategy_engine
    if engine is None:
        return "策略引擎未就绪。"
    parts = (args or "").split()
    if not parts:
        return "用法: /screen &lt;策略id&gt; [数量]。策略id 见 /strategies。"
    strategy_id = parts[0]
    limit = 10
    if len(parts) > 1:
        try:
            limit = max(1, min(50, int(parts[1])))
        except ValueError:
            pass

    d = _latest_date(ctx)
    if d is None:
        return "暂无数据, 请先 /sync 同步日K。"

    # 优先走 PRESET(与选股页一致), 无则走文件策略引擎
    try:
        from app.services.screener import PRESET_STRATEGIES, ScreenerService
        if strategy_id in PRESET_STRATEGIES:
            svc = ScreenerService(ctx.repo)
            result = svc.run_preset(strategy_id, as_of=d, display_limit=limit)
            rows, total, name = result.rows, result.total, PRESET_STRATEGIES[strategy_id]["name"]
        elif engine.has(strategy_id):
            res = engine.run(strategy_id, d)
            rows, total = res.rows[:limit], res.total
            meta = engine.get(strategy_id).meta
            name = meta.get("name", strategy_id)
        else:
            return f"未知策略: {strategy_id}。用 /strategies 查看可用策略。"
    except Exception as e:  # noqa: BLE001
        logger.warning("screen 执行失败: %s", e)
        return f"⚠️ 选股失败: {e}"

    if not rows:
        return f"<b>{name}</b> ({d.isoformat()})\n命中 0 只。"

    symbols = [str(r["symbol"]) for r in rows if r.get("symbol")]
    names = _name_map(ctx, symbols)
    lines = [f"<b>{name}</b> · {d.isoformat()} · 命中 {total} 只 (显示前 {len(rows)})", ""]
    for i, r in enumerate(rows, 1):
        sym = r.get("symbol") or ""
        nm = names.get(sym, "")
        close = _fmt_num(r.get("close"))
        chg = _fmt_pct(r.get("change_pct"))
        score = r.get("score")
        score_s = f" · 评分{_fmt_num(score, 0)}" if score is not None else ""
        lines.append(f"{i}. {sym} {nm} · {close} · {chg}{score_s}")
    return "\n".join(lines)


@command(
    "analyze", "对个股做 AI 四维分析(技术/价位/资金/消息面)",
    usage="<代码> [关注点]",
    args_hint="股票代码(6位或带后缀), 可选附加关注点文字",
)
async def _cmd_analyze(ctx: CommandContext, args: str) -> str:
    from app.services.stock_analyzer import analyze_stock_stream
    parts = (args or "").split(maxsplit=1)
    if not parts:
        return "用法: /analyze &lt;代码&gt; [关注点]。例: /analyze 600519"
    symbol = normalize_symbol(parts[0])
    focus = parts[1] if len(parts) > 1 else ""
    if ctx.repo is None or ctx.data_dir is None:
        return "数据层未就绪。"
    try:
        agen = analyze_stock_stream(ctx.repo, ctx.data_dir, symbol, focus)
        summary, body = await _collect_ndjson_stream(agen)
    except Exception as e:  # noqa: BLE001
        logger.warning("analyze 失败: %s", e)
        return f"⚠️ 分析失败: {e}"
    if not body:
        return f"⚠️ {symbol} 未生成分析(可能无数据或 AI 未配置)。"
    head = f"<b>个股分析 · {symbol}</b>"
    if summary:
        head += f"\n{summary}"
    return f"{head}\n\n{body}"


@command(
    "recap", "生成 AI 大盘复盘",
    usage="[日期YYYY-MM-DD]",
    args_hint="可选复盘日期, 缺省取最新交易日",
)
async def _cmd_recap(ctx: CommandContext, args: str) -> str:
    from app.services.market_recap import recap_market_stream
    if ctx.repo is None:
        return "数据层未就绪。"
    as_of = None
    arg = (args or "").strip()
    if arg:
        try:
            as_of = date.fromisoformat(arg)
        except ValueError:
            return f"日期格式应为 YYYY-MM-DD, 收到: {arg}"
    try:
        agen = recap_market_stream(ctx.repo, ctx.quote_service, ctx.depth_service, as_of, "")
        summary, body = await _collect_ndjson_stream(agen)
    except Exception as e:  # noqa: BLE001
        logger.warning("recap 失败: %s", e)
        return f"⚠️ 复盘失败: {e}"
    if not body:
        return "⚠️ 未生成复盘(可能无数据或 AI 未配置)。"
    head = "<b>大盘复盘</b>"
    if summary:
        head += f"\n{summary}"
    return f"{head}\n\n{body}"


@command(
    "alerts", "查看最近的监控告警",
    usage="[数量]",
    args_hint="可选返回条数(默认 10)",
)
async def _cmd_alerts(ctx: CommandContext, args: str) -> str:
    from app.services import alert_store
    if ctx.data_dir is None:
        return "数据层未就绪。"
    limit = 10
    arg = (args or "").strip()
    if arg:
        try:
            limit = max(1, min(50, int(arg)))
        except ValueError:
            pass
    events = alert_store.list_recent(ctx.data_dir, days=7, limit=limit)
    if not events:
        return "近 7 天无告警。"
    src_label = {"strategy": "策略", "signal": "信号", "price": "价格", "market": "异动"}
    lines = [f"<b>最近告警 ({len(events)})</b>"]
    for ev in events:
        src = src_label.get(ev.get("source", ""), ev.get("source", ""))
        sym = ev.get("symbol") or ""
        nm = ev.get("name") or ""
        msg = ev.get("message") or ""
        lines.append(f"[{src}] {sym} {nm} {msg}".strip())
    return "\n".join(lines)


@command("overview", "大盘概览: 指数 / 涨跌家数 / 涨停 / 情绪")
async def _cmd_overview(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    from app.services.market_overview_builder import build_market_overview
    if ctx.repo is None:
        return "数据层未就绪。"
    try:
        ov = build_market_overview(ctx.repo, ctx.quote_service, ctx.depth_service)
    except Exception as e:  # noqa: BLE001
        logger.warning("overview 失败: %s", e)
        return f"⚠️ 概览失败: {e}"
    as_of = ov.get("as_of")
    if not as_of:
        return "暂无数据, 请先 /sync。"
    lines = [f"<b>大盘概览 · {as_of}</b>", ""]
    # 指数
    for idx in (ov.get("indices") or [])[:4]:
        nm = idx.get("name") or idx.get("symbol") or ""
        lines.append(f"{nm}: {_fmt_num(idx.get('close'))} ({_fmt_pct(idx.get('change_pct'))})")
    lines.append("")
    b = ov.get("breadth") or {}
    lines.append(f"涨跌: 涨 {b.get('up', 0)} / 跌 {b.get('down', 0)} / 平 {b.get('flat', 0)}")
    lim = ov.get("limit") or {}
    lines.append(f"涨停 {lim.get('limit_up', 0)} · 炸板 {lim.get('broken', 0)} · 跌停 {lim.get('limit_down', 0)} · 最高 {lim.get('max_boards', 0)} 板")
    emo = ov.get("emotion") or {}
    lines.append(f"情绪: {emo.get('label', '-')} ({emo.get('score', '-')})")
    return "\n".join(lines)


# ================================================================
# 写命令
# ================================================================

@command(
    "add", "把股票加入自选",
    usage="<代码> [备注]", write=True,
    args_hint="股票代码(6位或带后缀), 可选备注文字",
)
async def _cmd_add(ctx: CommandContext, args: str) -> str:
    from app.services import watchlist
    parts = (args or "").split(maxsplit=1)
    if not parts:
        return "用法: /add &lt;代码&gt; [备注]。例: /add 600519 核心仓"
    symbol = normalize_symbol(parts[0])
    note = parts[1] if len(parts) > 1 else ""
    try:
        watchlist.add(symbol, note)
    except Exception as e:  # noqa: BLE001
        return f"⚠️ 添加失败: {e}"
    nm = _name_map(ctx, [symbol]).get(symbol, "")
    return f"✅ 已加入自选: {symbol} {nm}".strip()


@command(
    "remove", "从自选移除股票",
    usage="<代码>", write=True,
    args_hint="要移除的股票代码",
)
async def _cmd_remove(ctx: CommandContext, args: str) -> str:
    from app.services import watchlist
    arg = (args or "").strip().split()
    if not arg:
        return "用法: /remove &lt;代码&gt;"
    symbol = normalize_symbol(arg[0])
    try:
        watchlist.remove(symbol)
    except Exception as e:  # noqa: BLE001
        return f"⚠️ 移除失败: {e}"
    return f"✅ 已移出自选: {symbol}"


@command("sync", "立即触发盘后数据同步(拉日K + 算指标 + 跑监控)", write=True)
async def _cmd_sync(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    import asyncio

    from app.jobs import daily_pipeline
    repo = ctx.repo
    capset = ctx.capabilities
    if repo is None or capset is None:
        return "数据层未就绪。"

    def _run() -> dict:
        return daily_pipeline.run_now(repo, capset)

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _run)
        try:
            from app.api.data import invalidate_storage_cache
            invalidate_storage_cache()
        except Exception:  # noqa: BLE001
            pass
        repo.refresh_cache()
    except Exception as e:  # noqa: BLE001
        logger.warning("sync 失败: %s", e)
        return f"⚠️ 同步失败: {e}"
    d = _latest_date(ctx)
    summary = ""
    if isinstance(result, dict):
        # 尽量给出简报, 结构未知时静默
        parts = [f"{k}={v}" for k, v in result.items() if isinstance(v, (int, str, float))][:6]
        summary = " · ".join(parts)
    return f"✅ 同步完成。最新数据日: {d.isoformat() if d else '?'}\n{summary}".strip()


@command(
    "realtime", "开关实时行情",
    usage="on|off", write=True,
    args_hint="on 开启 / off 关闭",
)
async def _cmd_realtime(ctx: CommandContext, args: str) -> str:
    from app.services import preferences
    val = _parse_bool_arg(args)
    if val is None:
        cur = "开" if preferences.get_realtime_quotes_enabled() else "关"
        return f"当前实时行情: {cur}。用 /realtime on|off 切换。"
    qs = ctx.quote_service
    if val and qs is not None and not qs.is_realtime_allowed():
        preferences.save({"realtime_quotes_enabled": False})
        return "⚠️ 当前档位不支持实时行情, 已保持关闭。"
    preferences.save({"realtime_quotes_enabled": val})
    if qs is not None:
        try:
            qs.enable() if val else qs.disable()
        except Exception as e:  # noqa: BLE001
            logger.debug("realtime 切换副作用失败: %s", e)
    return f"✅ 实时行情已{'开启' if val else '关闭'}。"


@command(
    "livesearch", "开关 AI 联网检索(个股分析消息面实时检索新闻)",
    usage="on|off", write=True,
    args_hint="on 开启 / off 关闭",
)
async def _cmd_livesearch(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    from app import secrets_store
    from app.config import settings
    val = _parse_bool_arg(args)
    if val is None:
        cur = "开" if secrets_store.get_ai_live_search() else "关"
        return f"当前联网检索: {cur}。用 /livesearch on|off 切换。"
    secrets_store.save({"ai_live_search": val})
    settings.ai_live_search = val
    return f"✅ AI 联网检索已{'开启' if val else '关闭'}。"


# ================================================================
# 市场资讯命令 (快讯 / 研报 / 公告)
# ================================================================

@command("news", "最近市场快讯 (财联社电报)", usage="[条数]",
         args_hint="可选条数, 如 10; 留空默认 10 条")
async def _cmd_news(ctx: CommandContext, args: str) -> str:
    from app.services import news_store

    limit = 10
    a = (args or "").strip()
    if a.isdigit():
        limit = max(1, min(int(a), 30))
    repo = ctx.repo
    if repo is None:
        return "数据尚未就绪。"
    db = repo.store.data_dir / "news.db"
    if not db.exists():
        return "暂无快讯 (轮询未开启或尚未抓到)。可在「设置」开启快讯轮询。"
    rows = news_store.list_telegraphs(db, limit=limit)
    if not rows:
        return "暂无快讯。"
    lines = ["<b>📰 市场快讯</b>"]
    for r in rows:
        flag = "🔴 " if r.get("is_red") else ""
        t = r.get("time") or ""
        content = str(r.get("content") or "")
        lines.append(f"\n{flag}<b>{t}</b>  {content}")
        subs = r.get("subjects") or []
        if subs:
            lines.append(f"  🏷 {' · '.join(subs)}")
    return "\n".join(lines)


@command("report", "个股研报 (最近 90 天)", usage="<代码>",
         args_hint="股票代码或名字, 如 600519")
async def _cmd_report(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    from app.services import news_source

    code = normalize_symbol((args or "").strip())
    if not code:
        return "用法: /report &lt;代码&gt;, 如 /report 600519"
    items = news_source.fetch_stock_report(code)
    if not items:
        return f"未查到 {code} 的研报 (或近 90 天无)。"
    lines = [f"<b>📑 {code} 个股研报</b>"]
    for it in items[:10]:
        rating = f"[{it['rating']}] " if it.get("rating") else ""
        lines.append(f"\n{rating}<b>{it.get('title', '')}</b>")
        meta = " · ".join(x for x in [it.get("org", ""), it.get("date", "")] if x)
        if meta:
            lines.append(f"  {meta}")
    return "\n".join(lines)


@command("ireport", "行业研报 (最近 90 天)", usage="[行业代码]",
         args_hint="行业代码, 留空=全行业")
async def _cmd_ireport(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    from app.services import news_source

    industry = (args or "").strip()
    items = news_source.fetch_industry_report(industry)
    if not items:
        return "未查到行业研报 (或近 90 天无)。"
    lines = ["<b>📊 行业研报</b>"]
    for it in items[:10]:
        lines.append(f"\n<b>{it.get('title', '')}</b>")
        meta = " · ".join(x for x in [it.get("org", ""), it.get("date", "")] if x)
        if meta:
            lines.append(f"  {meta}")
    return "\n".join(lines)


@command("notice", "个股公告", usage="<代码>",
         args_hint="股票代码或名字, 如 600519")
async def _cmd_notice(ctx: CommandContext, args: str) -> str:  # noqa: ARG001
    from app.services import news_source

    code = normalize_symbol((args or "").strip())
    if not code:
        return "用法: /notice &lt;代码&gt;, 如 /notice 600519"
    items = news_source.fetch_stock_notice(code)
    if not items:
        return f"未查到 {code} 的公告。"
    lines = [f"<b>📢 {code} 公告</b>"]
    for it in items[:10]:
        cols = f"[{' '.join(it['columns'])}] " if it.get("columns") else ""
        lines.append(f"\n{cols}<b>{it.get('title', '')}</b>")
        if it.get("date"):
            lines.append(f"  {it['date']}")
    return "\n".join(lines)


# ================================================================
# 结构化命令分发
# ================================================================

async def dispatch_command(ctx: CommandContext, name: str, args: str) -> str | None:
    """执行一个已注册命令。未知命令返回 None(交给上层决定是否走 NL agent)。"""
    cmd = COMMANDS.get(name.lstrip("/").lower())
    if cmd is None:
        return None
    return await cmd.handler(ctx, args)
