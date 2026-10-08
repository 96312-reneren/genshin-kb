#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线测试用的假站点。只监听 127.0.0.1，端口随机。

它同时充当「靶子」和「证人」：所有被请求过的路径都记在 server.requests 里，
测试可以据此断言 —— 比如 `/private/hidden.html` 从始至终没被碰过。
"""
from __future__ import annotations

import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROBOTS = """\
# 假站点的 robots.txt
User-agent: *
Disallow: /private/
Disallow: /tmp-area
Allow: /private/public.html
Crawl-delay: 0.05

User-agent: BadBot
Disallow: /

Sitemap: /sitemap.xml
"""

PAGE_COUNT = 12


def _page_html(n: int, title: str, body: str, links: list[str]) -> str:
    anchors = "".join(f'<a href="{href}">link {i}</a> ' for i, href in enumerate(links))
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<style>.hidden{{display:none}}</style>
<script>var x = "这段脚本不该出现在正文里";</script>
</head>
<body>
<h1>{title}</h1>
<p>{body}</p>
<p>{body} 补充说明，用来把正文撑长一点，方便摘要环节有东西可总结。</p>
{anchors}
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeSite/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self):                                    # noqa: N802
        self.server.requests.append(self.path)           # 记账：谁被碰过
        path = self.path.split("?", 1)[0]

        if path == "/robots.txt":
            return self._send(200, ROBOTS, "text/plain; charset=utf-8")
        if path == "/private/hidden.html":
            return self._send(200, _page_html(0, "机密页", "这页被 robots 禁止，不该被抓。", []),
                              "text/html; charset=utf-8")
        if path == "/private/public.html":
            return self._send(200, _page_html(0, "白名单例外页", "虽然父目录被禁止，但 Allow 明确放行。", []),
                              "text/html; charset=utf-8")
        if path == "/" or path == "/index.html":
            links = [f"/page/{i}.html" for i in range(1, PAGE_COUNT + 1)]
            links += ["/private/hidden.html", "/private/public.html",
                      "http://127.0.0.1:1/never.html", "http://example.com/external.html"]
            return self._send(200, _page_html(0, "首页", "这是离线假站点的首页。", links),
                              "text/html; charset=utf-8")
        if path.startswith("/page/") and path.endswith(".html"):
            try:
                n = int(path[len("/page/"):-len(".html")])
            except ValueError:
                return self._send(404, "<html><body>bad</body></html>", "text/html")
            if 1 <= n <= PAGE_COUNT:
                body = f"第 {n} 页的正文。这一页讲的是离线测试主题 {n}，内容仅存在于本机回环。"
                return self._send(200, _page_html(n, f"第 {n} 页", body, ["/", "/index.html"]),
                                  "text/html; charset=utf-8")
        if path == "/sitemap.xml":
            return self._send(200, "<urlset></urlset>", "application/xml")
        return self._send(404, "<html><body>404</body></html>", "text/html; charset=utf-8")

    def _send(self, code: int, body: str, content_type: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):                        # 测试里不需要噪声
        pass


@contextlib.contextmanager
def serve():
    """启动假站点。yield (base_url, requests 列表, server)。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", server.requests, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":                               # 手动起一个看看
    with serve() as (base, requests, _srv):
        print(f"假站点在 {base}  （Ctrl+C 退出）")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print(f"\n共处理 {len(requests)} 个请求")
