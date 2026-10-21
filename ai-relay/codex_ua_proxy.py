#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地 UA 注入代理 —— 让 Codex 能连上被 Cloudflare 挡住的中转服务。

## 为什么需要它

实测发现：tokken.cc 架在 Cloudflare 后面，而 Cloudflare 的
Browser Integrity Check 会把「没有浏览器特征的请求」当成爬虫，
返回 403 + `error code: 1010`。

    curl 裸请求     -> HTTP 403 (error code: 1010)
    加上浏览器 UA   -> HTTP 200 正常返回模型列表

Codex CLI 发出的请求不带浏览器 UA，于是被拦。
本代理在本地转发时补上 UA，问题即解。

## 用法

1. 启动：  python codex_ua_proxy.py --port 8787
2. 改 ~/.codex/config.toml，把
       base_url = "https://tokken.cc/v1"
   改成
       base_url = "http://127.0.0.1:8787/v1"
3. 正常用 Codex 即可。

## 注意

- 这是让合法的 API 客户端访问你自己购买的服务，不是绕过鉴权：
  Authorization 头原样透传，本代理不读写任何凭据。
- 代理只在本地回环地址监听，不对外暴露。
"""

from __future__ import annotations

import argparse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://tokken.cc"

# 转发时用哪个出网代理。
# 不能直接依赖环境变量里的 http_proxy —— 那可能是别的程序塞进去的内部代理，
# 只能访问它自己的域名（实测遇到：走它访问 tokken.cc 直接 502）。
OPENER = None


def build_opener(explicit: str | None = None):
    """优先用显式指定；否则探测本机常见翻墙端口；都没有就直连（TUN 模式）。"""
    import socket

    if explicit:
        px = {"http": explicit, "https": explicit}
        print(f"出网代理：{explicit}（手动指定）")
        return urllib.request.build_opener(urllib.request.ProxyHandler(px))

    for port in (7890, 7891, 7897, 1080, 10808, 2080):
        with socket.socket() as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                px = f"http://127.0.0.1:{port}"
                print(f"出网代理：{px}（自动探测）")
                return urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": px, "https": px}))

    print("未检测到本机代理端口，改为直连（FlClash 开 TUN 模式时正常工作）")
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 这些头由转发层重新决定，不能原样带给上游
DROP_HEADERS = {"host", "content-length", "connection", "accept-encoding"}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 只记录一行摘要，避免刷屏
        print(f"[{self.log_date_time_string()}] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def _forward(self):
        global OPENER
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None

        hdrs = {
            k: v for k, v in self.headers.items()
            if k.lower() not in DROP_HEADERS
        }
        hdrs["User-Agent"] = UA          # 关键的一行
        hdrs["Accept-Encoding"] = "identity"  # 避免压缩流转发带来麻烦

        req = urllib.request.Request(
            UPSTREAM + self.path, data=body, headers=hdrs, method=self.command
        )

        try:
            response_headers = None
            try:
                with (OPENER or urllib.request).open(req, timeout=180) as r:
                    data = r.read()
                code = r.status
                response_headers = r.headers
            except urllib.error.HTTPError as e:
                data = e.read()
                code = e.code
                response_headers = e.headers
            except Exception:
                # 连接层失败（超时/拒连/翻墙刚开）：上游探测结果可能过期了。
                # 重新探测一次出网代理并重试 —— 这样 FlClash 后开也无需重启本代理。
                OPENER = build_opener(None)
                with (OPENER or urllib.request).open(req, timeout=180) as r:
                    data = r.read()
                code = r.status
                response_headers = r.headers
            self.send_response(code)
            for k in ("Content-Type", "Cache-Control"):
                if response_headers and response_headers.get(k):
                    self.send_header(k, response_headers.get(k))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            msg = f"proxy error: {e}".encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_PATCH = _forward
    do_DELETE = _forward
    do_HEAD = _forward


def main():
    global UPSTREAM
    ap = argparse.ArgumentParser(description="给 Codex 请求补浏览器 UA 的本地代理")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--upstream", default=UPSTREAM)
    ap.add_argument("--upstream-proxy", default=None,
                    help="出网代理，如 http://127.0.0.1:7890；不填则自动探测")
    args = ap.parse_args()

    UPSTREAM = args.upstream.rstrip("/")

    global OPENER
    OPENER = build_opener(args.upstream_proxy)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), ProxyHandler)
    print(f"UA 注入代理已启动： http://127.0.0.1:{args.port}  ->  {UPSTREAM}")
    print("把 ~/.codex/config.toml 的 base_url 改成 http://127.0.0.1:%d/v1 即可" % args.port)
    print("Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
