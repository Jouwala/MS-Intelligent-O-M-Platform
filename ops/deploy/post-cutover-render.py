# -*- coding: utf-8 -*-
"""看板页真实渲染探针（post-cutover-check.ps1 -Render 的伴生脚本）。

逐页用 Playwright 无头浏览器打开，收集：
  - console error 与 pageerror
  - /api/ 下的 4xx/5xx 响应
  - 主内容区文本长度（判断不是白屏）
  - canvas 数量（图表页是否真画出来）
只往 stdout 打 JSON，日志走 stderr，方便 PowerShell 侧直接 ConvertFrom-Json。

用法：
    python post-cutover-render.py [--base http://127.0.0.1/myapp/] [--wait 5000]
"""
import argparse
import json
import sys


DEFAULT_PAGES = [
    "finebi_cn_kpi",        # 原生看板（含图表）
    "cn_kpi",               # 自研聚合页
    "vmaxCxoDashboard",     # iframe 旧页
    "avail_county",         # 表格 + 下钻
    "nur",                  # 大表
    "packet_loss",          # 地图类
    "battery",              # 表格类
    "gis",                  # 多 canvas
    "alarmManagement",      # iframe + 外部代理
    "incident_overview",    # iframe
]


def render(base, wait_ms, pages):
    from playwright.sync_api import sync_playwright  # 延迟导入：缺依赖时给出明确提示

    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        for key in pages:
            url = base + "?page=" + key
            page = ctx.new_page()
            console_err, page_err, bad = [], [], []
            # favicon.ico 的 404 是全站都有的装饰性问题（*.ico 被 gitignore，没进构建产物），
            # 记进来只会每天刷一条假 WARN，这里直接忽略。
            page.on("console",
                    lambda m: console_err.append(m.text[:160])
                    if m.type == "error" and "favicon" not in m.text.lower() else None)
            page.on("pageerror", lambda e: page_err.append(str(e)[:160]))
            page.on("response",
                    lambda r: bad.append("%s %s" % (r.status, r.url[:120]))
                    if r.status >= 400 and "/api/" in r.url else None)
            rec = {"page": key, "ok": True}
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(wait_ms)
                info = page.evaluate(
                    """() => {
                        const cv = [...document.querySelectorAll('canvas')];
                        const main = document.querySelector('main') || document.body;
                        return {
                          text_len: (main.innerText || '').length,
                          canvas: cv.length,
                          canvas_max_w: cv.length ? Math.max(...cv.map(x => x.width)) : 0,
                          empty: document.querySelectorAll('.ant-empty').length,
                          spinning: document.querySelectorAll('.ant-spin-spinning').length,
                          iframes: document.querySelectorAll('iframe').length
                        };
                    }""")
                rec.update(info)
            except Exception as exc:  # noqa: BLE001
                rec.update({"ok": False, "error": str(exc)[:200]})
            rec["console_errors"] = len(console_err)
            rec["console_sample"] = console_err[:2]
            rec["page_errors"] = len(page_err)
            rec["bad_api"] = bad[:3]
            out.append(rec)
            page.close()
        browser.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1/myapp/")
    ap.add_argument("--wait", type=int, default=5000)
    ap.add_argument("--pages", default="")
    args = ap.parse_args()
    pages = [x.strip() for x in args.pages.split(",") if x.strip()] or DEFAULT_PAGES
    try:
        data = render(args.base, args.wait, pages)
    except ImportError:
        print("playwright 未安装：pip install playwright && playwright install chromium", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print("render failed: %r" % (exc,), file=sys.stderr)
        return 4
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
