#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wennec 转写：转写当天所有未转写的语音，默认本地模式。
用法：python3 backend/fennec_transcribe_today.py
依赖：应用已运行在 http://127.0.0.1:8778
"""
import argparse
import json
import re
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8778"
DATE_RE = re.compile(r"rec_(\d{4}-\d{2}-\d{2})_")


def http_get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r)


def http_post(path):
    req = urllib.request.Request(BASE + path, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def today_str():
    return time.strftime("%Y-%m-%d")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['local', 'aliyun'], default='local', help='aliyun 会上传音频并可能产生费用')
    args = parser.parse_args()
    today = today_str()
    try:
        sessions = http_get("/api/sessions")
    except Exception as e:
        print(f"[错误] 无法连接应用：{e}，请先启动 Wennec")
        return 1

    # 筛选当天 + 未转写（status != done）
    targets = []
    for s in sessions:
        m = DATE_RE.search(s.get("name", ""))
        if not m or m.group(1) != today:
            continue
        if s.get("status") == "done":
            continue
        targets.append(s)

    if not targets:
        print(f"[完成] 今天（{today}）没有未转写的语音。")
        return 0

    print(f"[开始] 今天（{today}）未转写语音 {len(targets)} 条，使用 {args.engine} 引擎逐个触发转写...")
    triggered, cached, skipped = [], [], []
    for s in targets:
        sid = s["id"]
        name = s["name"]
        try:
            res = http_post(f"/api/sessions/{sid}/transcribe?engine={args.engine}")
            if res.get("cached"):
                cached.append(name)
                print(f"   · {name} → 已缓存(done)")
            elif res.get("status") == "transcribing":
                triggered.append(name)
                print(f"   · {name} → 已触发 {args.engine} 转写")
            else:
                skipped.append((name, res))
                print(f"   · {name} → 跳过({res})")
        except Exception as e:
            skipped.append((name, str(e)))
            print(f"   · {name} → 异常({e})")

    print(f"\n[汇总] 触发 {len(triggered)} 条，已缓存 {len(cached)} 条，跳过/异常 {len(skipped)} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
