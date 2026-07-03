# 免费公开数据源 adapter 设计

- 日期:2026-07-03
- 状态:已通过设计评审,待写实现计划
- 相关文件:`backend/app/tickflow/client.py`、`backend/app/tickflow/policy.py`、`backend/app/tickflow/capabilities.py`、`backend/app/services/*`(消费方,不改)

## 1. 背景与目标

项目当前所有行情/财务数据依赖 TickFlow SDK,付费档(starter/pro/expert)才解锁实时行情、分钟K、五档、财务等能力。`data-sources.md` 实测确认:东方财富、新浪、腾讯的公开 HTTP 接口已能覆盖 TickFlow 付费档的全部能力,无需 TickFlow 付费 key。

目标:新建 `backend/app/tickflow/free_adapter.py`,产出一个**鸭子类型对象** `FreeSourceClient`,属性结构与方法签名对齐 TickFlow SDK。`client.py` 在「免费源模式」下返回该对象,使十几个直接调 `tf.klines.batch(...)` / `tf.quotes.get(...)` 等的 service **零改动**。

## 2. 范围

首版覆盖 7 类 SDK 能力(全量):

1. `exchanges.get_instruments` — 全市场标的列表(沪/深/北)
2. `klines.batch` / `klines.get`(日K、分钟K)— 前复权 K 线
3. `klines.intraday` / `intraday_batch` — 分时
4. `quotes.get` / `get_by_symbols` / `get_by_universes` — 实时行情(含五档)
5. `depth.batch` — 五档盘口
6. `financials.metrics` / `income` / `balance_sheet` / `cash_flow` — 财务
7. `universes.list` — 标的池列表(本地固定)

不在范围内:WebSocket 实时推送——`Cap.WEBSOCKET` 仍在 capset 中(避免 `require` 报错),但实时行情走 `quote_service` 既有的 HTTP 轮询 + SSE 实现,不依赖真 WS。

## 3. 触发方式

新增独立开关 `data_backend`,取值 `"tickflow"`(默认)/ `"free_source"`,持久化在 `secrets_store`。设置页提供单选,用户显式选择,**与是否有 TickFlow key 无关**(付费用户也能切到免费源做对照)。

- 切到 `free_source`:不清除 tickflow key(留作切回);`get_client()` 等优先返回 `FreeSourceClient`,无视 key 与档位。
- 切回 `tickflow`:按原逻辑(有 key 走探测、无 key 走 none 档 free-api)。

## 4. 能力与档位表示

`policy.detect_capabilities()` 增加 `data_backend == "free_source"` 分支:

- **不走** `_probe_real`,直接构造**全能力 capset**:所有 `Cap` 均在,限速取 `tiers.yaml` 的 expert 档值(公开源无 key 限制,用此值做自我节流)。
- `_persist(label="免费源(东财/新浪/腾讯)", invalid_key=False)`。
- `base_tier_name()` 返回 `"free_source"`。
- `client.py`:
  - `_should_use_free_server()` 增加 `free_source` 分支 → 返回 `False`(走 adapter,不走 free-api)。
  - `current_mode()` 增加 `"free_source"` 态。
  - `get_client()` / `get_async_client()` / `get_paid_realtime_client()` 在 `data_backend == "free_source"` 时返回 `FreeSourceClient` 实例。

业务代码里所有 `capset.require(Cap.X)` 全部通过;`depth_service` 的 `capset.limits(Cap.DEPTH5_BATCH)` 拿到 batch/rpm 用于切片节流。

## 5. 接口映射与字段对齐

adapter 内部用 httpx(同步 `httpx.Client` + 异步 `httpx.AsyncClient`),统一 UA(`Mozilla/5.0 ... Chrome/120`)/ Referer(`https://quote.eastmoney.com/` 等)/ 超时 10s / 瞬时错误退避重试 2 次。每条映射独立私有函数,返回结构与 TickFlow SDK `as_dataframe=False` 的 dict/list[dict] 一致,使 service 层解析逻辑零改动。

| SDK 调用 | 免费源 | 返回对齐 |
|---|---|---|
| `exchanges.get_instruments(ex, instrument_type="stock")` | 东财 `push2/clist` 按 `fs` 分市场(沪/深/北)翻页 | list[dict] → `symbol/name/code/exchange` |
| `klines.batch(symbols, period="1d", count=N, adjust=..., as_dataframe=True)` | 东财 `push2his/kline?klt=101&fqt=1`(前复权);`adjust="none"` 用 `fqt=0` | dict{symbol: list[kline dict]} → `date/open/high/low/close/volume/amount` |
| `klines.get(sym, period="1d"...)` | 同上单只 | 单 symbol 的 list |
| `klines.batch(..., period="1m")` | 东财 `klt=1`(1 分钟) | 同结构 |
| `klines.intraday(sym)` / `intraday_batch` | 腾讯 `web.ifzq.gtimg.cn/.../minute/query` | 分时点 list |
| `klines.ex_factors(symbols)` | **不调接口**——东财/腾讯直接返回前复权价,无原始因子 | 空 dict(`kline_sync.sync_ex_factors` 拿到空自然跳过,复权已服务端完成) |
| `quotes.get(symbols=...)` / `get_by_symbols` | 新浪 `hq.sinajs.cn/list=`(一次多只,含五档) | list[dict] → `symbol/name/last_price/prev_close/open/high/low/volume/amount/ext{change_amount,change_pct,turnover_rate}` |
| `quotes.get_by_universes(universes=...)` | 东财 `push2/clist` 按 `fs` 拉对应板块(沪A/深A/ETF/指数) | 同上 list[dict] |
| `depth.batch(symbols)` | 新浪 `hq.sinajs.cn/list=`(行情串第 10~29 字段即五档) | dict{symbol: MarketDepth 结构} |
| `financials.metrics` / `income` / `balance_sheet` / `cash_flow` | 东财 `datacenter` F10(`RPT_LICO_FN_CPD` 利润、`RPT_DMSK_FN_BALANCE` 资产负债、`RPT_F10_FINANCE_DUPONT` 杜邦含 ROE 等) | dict,字段映射成 SDK 财务字段 |
| `universes.list()` | 本地固定列表(`CN_Equity_A`/`CN_ETF`/`CN_Index` 等) | list[dict] 同 SDK 结构 |

### 关键取舍

- **`ex_factors` 返回空**:复权由东财/腾讯服务端完成(`fqt=1`/`qfq`),`kline_sync.sync_ex_factors` 拿到空结果自然跳过,不影响前复权日K。
- **WebSocket 不实现**:adapter 是同步 HTTP 轮询,实时行情走 `quote_service` 已有轮询循环 + SSE,不依赖真 WS。`Cap.WEBSOCKET` 仍在 capset(避免 `require` 报错)。

## 6. 错误处理、限流

**错误处理**:所有 HTTP 调用包统一重试(瞬时错误 5xx/超时/连接 → 退避 2 次),权限/参数错误(4xx 非瞬时)直接抛——抛出异常类名复用 SDK 习惯(`PermissionError` 等),使 `policy.py` 的 `_is_transient` / `try_call` 分类逻辑在免费源意外触探时也能复用。单只标的失败不拖垮整批(逐 symbol try/except,失败 warn 跳过,与现有 service 风格一致)。

**限流/节流**:公开源无硬 key 限制,但东财对高频会临时封 IP。adapter 在 `quotes.batch`、`clist` 翻页、`klines.batch` 三处做自我节流——用 capset 里 expert 档的 rpm/batch 值切片 + 批间 sleep(复用 `depth_service._call_depth_batch` 的 `60/rpm` 匀速模式)。

## 7. 配置 API

`secrets_store` 新增字段 `data_backend`。新增 settings API:

- `GET /api/settings` 返回增加 `data_backend` 字段。
- `POST /api/settings/data-backend`(body `{backend: "tickflow"|"free_source"}`):写 `secrets_store` → `tf_client.reset_clients()` → `detect_capabilities(force=True)` → 返回新 mode/label。

前端设置页加单选(TickFlow 付费数据源 / 免费公开数据源),切完刷新档位显示。前端改动留到实现期,本设计只定接口契约。

## 8. 测试

`tests/tickflow/`,全 mock(不写真实网络测试):

- `test_free_adapter_mapping.py`:用 `respx` mock 东财/新浪/腾讯响应,断言每个映射函数输出字段结构与 SDK `as_dataframe=False` 一致(symbol/name/open/high/... 齐全、类型正确)。
- `test_free_adapter_capabilities.py`:免费源模式下 `detect_capabilities()` 返回全 capset、label 正确、`base_tier_name()=="free_source"`。
- `test_free_adapter_client_routing.py`:`get_client()` 在 `data_backend` 各取值下返回正确类型(`FreeSourceClient` vs `TickFlow`)。

## 9. 不改动的部分

- 业务 service 层(`kline_sync`/`quote_service`/`depth_service`/`financial_sync`/`watchlist`/`pools`/`instrument_sync`/`index_sync`)零改动——它们调 SDK 方法,adapter 对齐签名。
- `data_providers/` 抽象层不接入(本设计用鸭子类型对齐 SDK,不走 provider 抽象)。
- `capabilities.py` 的 `Cap` 枚举不动。
- `tiers.yaml` 不动(只读其 expert 档限速值做节流)。
