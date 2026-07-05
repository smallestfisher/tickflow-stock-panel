"""news_source 解析函数单测 —— 喂固定 HTML/JSON 样本, 不打真网。"""
from __future__ import annotations

from app.services import news_source

# 财联社电报页面结构简化样本 (对齐 go-stock 的 .telegraph-content-box 选择器)
_CLS_HTML = """
<html><body>
  <div class="telegraph-content-box">
    <div class="telegraph-content-box">
      <span>10:30:00</span>
      <span class="c-de0422">重磅利好: 某政策落地</span>
    </div>
    <div>
      <a class="label-item">半导体</a>
      <a class="label-item link-label-item" href="https://cls.cn/detail/1">详情</a>
    </div>
    <div class="telegraph-stock-plate-box">
      <a>贵州茅台</a>
    </div>
  </div>
  <div class="telegraph-content-box">
    <div class="telegraph-content-box">
      <span>10:31:00</span>
      <span>普通消息</span>
    </div>
  </div>
</body></html>
"""


def test_parse_telegraph_html():
    items = news_source.parse_telegraph_html(_CLS_HTML)
    assert len(items) == 2
    first = items[0]
    assert first["time"] == "10:30:00"
    assert first["content"] == "重磅利好: 某政策落地"
    assert first["is_red"] is True
    assert first["source"] == "财联社电报"
    assert first["url"] == "https://cls.cn/detail/1"
    assert "半导体" in first["subjects"]
    assert "贵州茅台" in first["stocks"]
    # 第二条无红头/标签
    assert items[1]["is_red"] is False
    assert items[1]["content"] == "普通消息"


def test_parse_telegraph_empty():
    assert news_source.parse_telegraph_html("") == []
    assert news_source.parse_telegraph_html("<html></html>") == []


def test_parse_report_list():
    # 东财 report/list2 返回 {"data": [...]} 结构
    raw = {
        "data": [
            {
                "title": "买入评级报告",
                "orgSName": "某某证券",
                "publishDate": "2026-07-01 00:00:00",
                "researcher": "张三",
                "emRatingName": "买入",
                "infoCode": "AP123",
            }
        ]
    }
    items = news_source.parse_report_list(raw)
    assert len(items) == 1
    assert items[0]["title"] == "买入评级报告"
    assert items[0]["org"] == "某某证券"
    assert items[0]["rating"] == "买入"
    assert items[0]["url"].startswith("https://data.eastmoney.com/report/")


def test_parse_report_list_empty():
    assert news_source.parse_report_list({}) == []
    assert news_source.parse_report_list({"data": None}) == []


def test_parse_notice_list():
    raw = {
        "data": {
            "list": [
                {
                    "title": "关于股东减持的公告",
                    "notice_date": "2026-07-02 16:00:00",
                    "art_code": "AN456",
                    "codes": [{"stock_code": "600519", "short_name": "贵州茅台"}],
                    "columns": [{"column_name": "股东股本"}],
                }
            ]
        }
    }
    items = news_source.parse_notice_list(raw)
    assert len(items) == 1
    assert items[0]["title"] == "关于股东减持的公告"
    assert items[0]["date"] == "2026-07-02 16:00:00"
    assert items[0]["url"].startswith("https://data.eastmoney.com/notices/detail/")


def test_parse_notice_list_empty():
    assert news_source.parse_notice_list({}) == []
    assert news_source.parse_notice_list({"data": {}}) == []


def test_em_code_strips_suffix():
    assert news_source._em_code("600519.SH") == "600519"
    assert news_source._em_code("300750.SZ") == "300750"
    assert news_source._em_code("600519") == "600519"
