from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from xiaomi_ai_bridge.web_search import WEB_SEARCH_TOOL_NAME, WebSearchSource


def settings(**overrides):
    values = {
        "web_search_enabled": True,
        "web_search_base_url": "http://searxng.test:8080",
        "web_search_language": "zh-CN",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _searxng_payload():
    return {
        "query": "测试",
        "results": [
            {"title": "第一条", "url": "https://a.example.com/x?y=1", "content": " 摘要一 ", "engines": ["bing", "ddg"]},
            {"title": "坏行-无地址", "content": "缺 url"},
            {"title": "坏行-ftp", "url": "ftp://old.example.com/f", "content": "协议不允许"},
            {"title": "第三条", "url": "http://c.example.com/", "content": "摘要三"},
            "不是对象的行",
        ],
    }


class WebSearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_are_explicitly_gated(self):
        disabled = WebSearchSource(settings(web_search_enabled=False))
        self.assertEqual(disabled.tool_definitions(), [])
        self.assertEqual(
            await disabled.execute(WEB_SEARCH_TOOL_NAME, {"query": "测试"}),
            {"ok": False, "error": "网页搜索工具未启用，请先配置 WEB_SEARCH_TOOLS_ENABLED"},
        )
        enabled = WebSearchSource(settings())
        names = [item["function"]["name"] for item in enabled.tool_definitions()]
        self.assertEqual(names, [WEB_SEARCH_TOOL_NAME])

    def test_enabled_without_base_url_is_rejected(self):
        with self.assertRaises(ValueError):
            WebSearchSource(settings(web_search_base_url=""))

    def test_base_url_must_be_a_bare_http_host(self):
        for bad in ("ftp://h", "http://h/searxng", "http://h?x=1", "http://h#frag", "not-a-url"):
            with self.assertRaises(ValueError, msg=bad):
                WebSearchSource(settings(web_search_base_url=bad))
        good = WebSearchSource(settings(web_search_base_url="http://h:8080/"))
        self.assertEqual(good.base_url, "http://h:8080")

    async def test_results_are_trimmed_to_quotable_fields(self):
        source = WebSearchSource(settings())
        source._request_json = AsyncMock(return_value=_searxng_payload())
        result = await source.execute(WEB_SEARCH_TOOL_NAME, {"query": "测试"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["query"], "测试")
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(
            result["results"][0],
            {
                "title": "第一条",
                "url": "https://a.example.com/x?y=1",
                "site": "a.example.com",
                "snippet": "摘要一",
                "engines": "bing ddg",
            },
        )
        # 不回显上游原始响应里的其它字段
        self.assertNotIn("content", result["results"][0])
        self.assertTrue(result["read_only"])
        source._request_json.assert_awaited_once()

    async def test_identical_query_within_ttl_hits_the_cache(self):
        source = WebSearchSource(settings())
        source._request_json = AsyncMock(return_value=_searxng_payload())
        await source.execute(WEB_SEARCH_TOOL_NAME, {"query": "测试"})
        again = await source.execute(WEB_SEARCH_TOOL_NAME, {"query": "测试 "})
        source._request_json.assert_awaited_once()
        self.assertTrue(again["cached"])
        self.assertEqual(again["results"], (await source.execute(WEB_SEARCH_TOOL_NAME, {"query": "测试"}))["results"])

    async def test_cache_is_bounded_and_evicts_the_oldest(self):
        from xiaomi_ai_bridge import web_search as module

        source = WebSearchSource(settings())
        source._request_json = AsyncMock(return_value=_searxng_payload())
        for i in range(module.CACHE_MAX_ENTRIES + 8):
            await source.execute(WEB_SEARCH_TOOL_NAME, {"query": f"查询 {i}"})
        self.assertEqual(len(source._cache), module.CACHE_MAX_ENTRIES)
        # 最旧的「查询 0..7」被淘汰，留下的是最近的查询
        self.assertNotIn("查询 0", source._cache)
        self.assertIn(f"查询 {module.CACHE_MAX_ENTRIES + 7}", source._cache)
        # 命中会把条目挪到队尾：再次查询旧条目之前，先查一次新条目挤进去
        await source.execute(WEB_SEARCH_TOOL_NAME, {"query": "查询 100"})
        await source.execute(WEB_SEARCH_TOOL_NAME, {"query": f"查询 {module.CACHE_MAX_ENTRIES + 7}"})
        await source.execute(WEB_SEARCH_TOOL_NAME, {"query": "查询 100"})  # 已在缓存，且在最前
        await source.execute(WEB_SEARCH_TOOL_NAME, {"query": f"查询 {module.CACHE_MAX_ENTRIES + 7}"})
        keys = list(source._cache)
        self.assertLess(keys.index("查询 100"), len(keys) - 1, "命中条目应被挪到队尾")

    async def test_language_is_passed_to_the_engine(self):
        source = WebSearchSource(settings())
        source._request_json = AsyncMock(return_value=_searxng_payload())
        await source.execute(WEB_SEARCH_TOOL_NAME, {"query": "测试"})
        args = source._request_json.await_args.args
        self.assertEqual(args[0], "http://searxng.test:8080/search")
        self.assertEqual(args[1]["language"], "zh-CN")
        self.assertEqual(args[1]["format"], "json")

    async def test_bad_arguments_never_reach_the_network(self):
        source = WebSearchSource(settings())
        source._request_json = AsyncMock()
        for arguments in (None, "not json", [], {"query": ""}, {"query": "x" * 300}, {"nope": 1}):
            result = await source.execute(WEB_SEARCH_TOOL_NAME, arguments)
            self.assertFalse(result["ok"], arguments)
        source._request_json.assert_not_awaited()

    async def test_upstream_failure_is_reported_not_raised(self):
        source = WebSearchSource(settings())
        source._request_json = AsyncMock(side_effect=ValueError("搜索服务返回 HTTP 503，未取得结果"))
        result = await source.execute(WEB_SEARCH_TOOL_NAME, {"query": "测试"})
        self.assertFalse(result["ok"])
        self.assertIn("503", result["error"])

    async def test_request_json_only_talks_to_the_fixed_endpoint(self):
        source = WebSearchSource(settings())
        with self.assertRaises(ValueError):
            await source._request_json("http://evil.example.com/search", {"q": "x"})
        # 未启用时 base_url 为空，任何请求都拒绝
        dormant = WebSearchSource(settings(web_search_enabled=False))
        with self.assertRaises(ValueError):
            await dormant._request_json("http://h/search", {"q": "x"})


if __name__ == "__main__":
    unittest.main()
