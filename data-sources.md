# 数据来源清单

本文档按代码中出现并会被请求或导航的外部 URL 模板整理。`%s`、`%d` 等为运行时参数；同一接口重复出现时只列一次；纯 `Referer` / `Origin` 辅助站点不逐条重复展开。

## 股票/行情

| 链接/模板 | 作用 |
|---|---|
| `http://hq.sinajs.cn/rn=%d&list=%s` | 新浪股票/基金实时行情，A 股、美股、基金、持仓股票报价 |
| `http://qt.gtimg.cn/?_=%d&q=%s` | 腾讯实时行情，主要用于港股/部分沪深行情 |
| `http://api.tushare.pro` | Tushare，获取 `stock_basic`、`index_basic` 等基础数据，需要 `TushareToken` |
| `https://quote.eastmoney.com/%s.html` | 东方财富个股页面，浏览器抓取当前价/行情展示 |
| `https://stock.finance.sina.com.cn/usstock/quotes/%s.html` | 新浪美股页面 |
| `https://stock.finance.sina.com.cn/hkstock/quotes/%s.html` | 新浪港股页面 |
| `https://finance.sina.com.cn/realstock/company/` | 新浪 A 股实时行情页面 |
| `https://gushitong.baidu.com/stock/ab-%s` | 百度股市通 A 股页面 |
| `https://gushitong.baidu.com/stock/hk-...` / `https://gushitong.baidu.com/stock/us-...` | 百度股市通港股/美股页面 |
| `https://xueqiu.com/snowman/S/%s/detail#/ZYCWZB` | 雪球财务指标页面 |

## K 线/资金/F10

| 链接/模板 | 作用 |
|---|---|
| `https://push2his.eastmoney.com/api/qt/stock/kline/get` | 东方财富股票 K 线 |
| `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?...` | 腾讯复权 K 线 |
| `https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=%s` | 腾讯 A/H 分时 |
| `https://web.ifzq.gtimg.cn/appstock/app/UsMinute/query?code=%s` | 腾讯美股分时 |
| `http://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?...` | 新浪 K 线 |
| `https://quotes.sina.cn/cn/api/jsonp_v2.php/` | 新浪 JSONP K 线 |
| `https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get` | 东方财富个股资金流历史 |
| `https://push2.eastmoney.com/api/qt/clist/get?...` | 东方财富股票列表、资金排行、市场列表 |
| `https://data.eastmoney.com/dataapi/bkzj/getbkzj?key=f62&code=m%3A90%2Bs%3A4` | 东方财富板块资金流 |
| `https://data.eastmoney.com/dataapi/bkzj/getbkzj?key=f62&code=m%3A90%2Bt%3A3` | 东方财富概念资金流 |
| `https://push2ex.eastmoney.com/getAllStockChanges?...` | 东方财富股票异动 |
| `https://datacenter.eastmoney.com/securities/api/data/v1/get` | 东方财富 F10 通用接口 |
| `...reportName=RPT_F10_CORETHEME_BOARDTYPE` | 个股概念/题材 |
| `...reportName=RPT_F10_FINANCE_DUPONT` | 财务杜邦分析 |
| `...reportName=RPT_F10_EH_HOLDERNUM` | 股东户数 |
| `...reportName=RPT_RZRQ_STOCKS_DETAIL` | 融资融券 |
| `https://datacenter-web.eastmoney.com/web/api/data/v1/get?...RPT_MUTUAL_TOP10DEAL` | 沪深港通十大成交 |
| `https://datacenter-web.eastmoney.com/api/data/v1/get?...RPT_VALUEINDUSTRY_STA` | 行业估值 |
| `https://data.eastmoney.com/dataapi/xuangu/list?...` | 东方财富选股列表 |
| `https://stock.gtimg.cn/data/hk_rank.php?...` | 腾讯港股排行/基础列表 |
| `https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHKStockData?...` | 新浪港股列表 |

## 基金

| 链接/模板 | 作用 |
|---|---|
| `http://fund.eastmoney.com/%s.html` | 东方财富基金详情页 |
| `http://fund.eastmoney.com/pingzhongdata/%s.js` | 基金基础、走势、阶段收益等 |
| `http://api.fund.eastmoney.com/f10/lsjz?...` | 基金历史净值 |
| `https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?...` | 基金搜索 |
| `https://fund.eastmoney.com/allfund.html` | 全量基金列表 |
| `https://fundgz.1234567.com.cn/js/%s.js` | 天天基金估值 |
| `https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo?...` | 东方财富移动端基金信息 |
| `https://fund.eastmoney.com/data/rankhandler.aspx` | 基金排行 |
| `https://fundf10.eastmoney.com/FundArchivesDatas.aspx?...` | 基金持仓 |
| `http://hq.sinajs.cn/list=fu_%s` / `http://hq.sinajs.cn/list=f_%s` | 新浪基金估值/净值 |

## 新闻/研报/宏观/热点

| 链接/模板 | 作用 |
|---|---|
| `https://www.cls.cn/api/cache?name=telegraph...` | 财联社电报 |
| `https://www.cls.cn/api/cache?name=telegraphList...` | 财联社快讯列表 |
| `https://x-quote.cls.cn/quote/index/home?...` | 财联社市场统计、涨跌分布 |
| `https://www.cls.cn/api/calendar/web/list?...` | 财联社财经日历 |
| `https://www.cls.cn/api/csw?...` | 财联社相关内容接口 |
| `https://zhibo.sina.com.cn/api/zhibo/feed?...` | 新浪财经直播 |
| `https://proxy.finance.qq.com/ifzqgtimg/appstock/app/rank/indexRankDetail2` | 腾讯全球指数 |
| `https://proxy.finance.qq.com/ifzqgtimg/appstock/app/mktHs/rank?...` | 腾讯行业排行 |
| `https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow...` | 新浪行业/个股资金流排行 |
| `https://reportapi.eastmoney.com/report/list` | 东方财富行业研报 |
| `https://reportapi.eastmoney.com/report/list2` | 东方财富个股研报 |
| `https://reportapi.eastmoney.com/report/bk` | 东方财富行业字典/研报板块 |
| `https://reportapi.eastmoney.com/report/jg?...` | 东方财富机构研报 |
| `https://np-anotice-stock.eastmoney.com/api/security/ann?...` | 东方财富公告 |
| `https://news-mediator.tradingview.com/news-flow/v2/news?...` | TradingView 新闻流 |
| `https://news-headlines.tradingview.com/v3/story?id=%s...` | TradingView 新闻详情 |
| `https://stock.xueqiu.com/v5/stock/hot_stock/list.json?...` | 雪球热股 |
| `https://xueqiu.com/hot_event/list.json?...` | 雪球热门事件 |
| `https://gubatopic.eastmoney.com/interface/GetData.aspx?...` | 东方财富股吧热门话题 |
| `https://app.jiuyangongshe.com/jystock-app/api/v1/timeline/list` | 韭研公社投资日历 |
| `https://www.reuters.com/pf/api/v3/content/fetch/...` | 路透新闻 |
| `https://irm.cninfo.com.cn/newircs/index/search?...` | 巨潮互动易问答 |
| `https://api.zizizaizai.com/v3/open/review/uplimit/hot?...` | 涨停复盘/热门涨停 |
| `https://datacenter-web.eastmoney.com/api/data/v1/get?...RPT_ECONOMY_GDP/CPI/PPI/PMI` | 东方财富宏观 GDP/CPI/PPI/PMI |

## 搜索/AI/辅助服务

| 链接/模板 | 作用 |
|---|---|
| `https://www.bing.com/search?q=%s` | Bing 搜索 |
| `https://www.baidu.com/s?wd=%s` | 百度搜索 |
| `https://openapi.iwencai.com/v1/query2data` | 同花顺问财查询，需要 `IwencaiApiKey` |
| `https://openapi.iwencai.com/v1/comprehensive/search` | 问财研报/新闻/公告搜索 |
| `https://ai-saas.eastmoney.com/proxy/entity/dialogTagsV2` | 东方财富 AI 实体识别，需要 `EmApiKey` |
| `https://ai-saas.eastmoney.com/proxy/app-robo-advisor-api/assistant/...` | 东方财富 AI 研报、业绩点评、问答、行业研究等 |
| `https://ai-saas.eastmoney.com/proxy/b/mcp/tool/searchData` | 东方财富 AI 数据搜索 |
| `https://ai-saas.eastmoney.com/proxy/b/mcp/tool/searchNews` | 东方财富 AI 新闻搜索 |
| `https://np-tjxg-g.eastmoney.com/api/smart-tag/stock/v3/pw/search-code` | 东方财富股票搜索 |
| `https://np-tjxg-b.eastmoney.com/api/smart-tag/bkc/v3/pw/search-code` | 东方财富板块搜索 |
| `https://np-tjxg-b.eastmoney.com/api/smart-tag/etf/v3/pw/search-code` | 东方财富 ETF 搜索 |
| `https://np-ipick.eastmoney.com/recommend/stock/heat/ranking?...` | 东方财富热门选股策略 |
| `https://backtest.10jqka.com.cn/strategysquare/list?...` | 同花顺策略广场 |
| `https://timor.tech/api/holiday/year/%s/` | 节假日年份查询 |
| `https://timor.tech/api/holiday/info/%s` | 节假日/是否交易日辅助判断 |
| `http://go-stock.sparkmemory.top:1918/api` | Prompt Plaza 代理默认地址 |
| `http://go-stock.sparkmemory.top:16688/upload` | 分享文本/AI 分析上传 |
| 用户配置的 `AIConfig.BaseUrl` | OpenAI 兼容模型接口，具体地址由设置页填写 |
| 用户配置的 MCP Server URL/Command | 外部 MCP 工具，具体来源由用户配置 |
| `https://fonts.googleapis.com/css2?...` | Markdown 转图片时加载字体，不是财经数据源 |

## 内置/项目自建资源

| 链接/路径 | 作用 |
|---|---|
| `build/stock_basic.json` | 首次启动导入 A 股基础数据 |
| `build/stock_base_info_hk.json` | 首次启动导入港股基础数据 |
| `build/stock_base_info_us.json` | 首次启动导入美股基础数据 |
| `http://8.134.249.145:18080/go-stock/stock_basic.json` | 远程更新 A 股基础数据 |
| `http://8.134.249.145:18080/go-stock/stock_base_info_hk.json` | 远程更新港股基础数据 |
| `http://8.134.249.145:18080/go-stock/stock_base_info_us.json` | 远程更新美股基础数据 |
| `data/stock.db` | 本地 SQLite 数据库，保存自选、分组、交易记录、设置、缓存等 |

## 关键代码入口

| 文件 | 说明 |
|---|---|
| `backend/data/stock_data_api.go` | 股票实时行情、基础资料、资金、F10 等 |
| `backend/data/eastmoney_kline_api.go` | 东方财富 K 线 |
| `backend/data/sina_kline_api.go` | 新浪/腾讯 K 线补充 |
| `backend/data/fund_data_api.go` | 基金详情、净值、估值、排行、持仓 |
| `backend/data/market_news_api.go` | 新闻、快讯、研报、宏观、热点 |
| `backend/data/web_search_api.go` | Bing/百度搜索 |
| `backend/data/iwencai_api.go` | 同花顺问财 |
| `backend/data/eastmoney_api.go` | 东方财富 AI 接口 |
| `backend/data/search_stock_api.go` | 东方财富选股/板块/ETF 搜索、热门策略 |
| `backend/data/wallstreetcn_api.go` | 华尔街见闻 |
| `server/modules/stockbase.go` | 远程基础股票列表更新 |
| `server/modules/share.go` | 分享上传 |
| `server/prompt_plaza_proxy.go` | Prompt Plaza 代理 |
