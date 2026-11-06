#!/usr/bin/env python3
"""YouTube 调研取数脚本（yuanskill-pro 阶段 2 · L5 视频层）。

只做两件确定性的事，判断留给模型：
  search      按关键词列候选视频（播放量、频道、时长、发布日期、播放/订阅比）
  transcript  下载一条视频的字幕并转成带时间戳的纯文本

依赖：yt-dlp（brew install yt-dlp deno）。缺依赖时退出码 3，按 references/05 §5 降级。

字幕纪律（2026-11 实测）：只取「人工字幕」和「原语言自动字幕（*-orig）」；
自动翻译字幕会被 YouTube 限流（HTTP 429），本脚本不请求。

用法：
  python3 yt_research.py search "claude code skills" -n 8 --meta 5
  python3 yt_research.py transcript https://www.youtube.com/watch?v=ID --out <目录>
  python3 yt_research.py info https://www.youtube.com/watch?v=ID      # 无字幕时的降级

退出码：0 成功；2 无可用字幕（走降级）；3 缺 yt-dlp；4 yt-dlp 调用失败。
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys


def need_ytdlp():
    if not shutil.which("yt-dlp"):
        print("缺 yt-dlp：先问用户能否 `brew install yt-dlp deno`；不装就按 references/05 §5 降级。",
              file=sys.stderr)
        sys.exit(3)


def run(cmd, timeout=180):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def fmt_views(n):
    if n is None:
        return "?"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def cmd_search(a):
    need_ytdlp()
    rc, out, err = run(["yt-dlp", f"ytsearch{a.n}:{a.query}", "--flat-playlist", "-j"])
    if rc != 0 and not out.strip():
        print(err.strip()[-800:], file=sys.stderr)
        sys.exit(4)
    rows = []
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append({
            "id": d.get("id"),
            "title": d.get("title"),
            "channel": d.get("channel") or d.get("uploader"),
            "views": d.get("view_count"),
            "duration_s": d.get("duration"),
            "url": f"https://www.youtube.com/watch?v={d.get('id')}",
        })
    # 前 meta 条补拉详情：发布日期、订阅数、原语言（每条一次请求，别开太大）
    for r in rows[: a.meta]:
        rc, o, _ = run(["yt-dlp", "-j", "--skip-download", "--no-warnings", r["url"]], timeout=120)
        if rc != 0 or not o.strip():
            r["meta_error"] = True
            continue
        d = json.loads(o.splitlines()[0])
        subs = d.get("channel_follower_count")
        r.update({
            "upload_date": d.get("upload_date"),
            "subscribers": subs,
            "views_per_sub": round(r["views"] / subs, 2) if r.get("views") and subs else None,
            "language": d.get("language"),
            "has_manual_subs": bool(d.get("subtitles")),
            "chapters": len(d.get("chapters") or []),
        })
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    print(f"| # | 标题 | 频道 | 播放 | 时长 | 发布 | 播放/订阅 | URL |")
    print("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        dur = f"{(r['duration_s'] or 0) // 60}m" if r.get("duration_s") else "?"
        print(f"| {i} | {(r['title'] or '')[:50]} | {r['channel']} | {fmt_views(r['views'])} | {dur} | "
              f"{r.get('upload_date', '-')} | {r.get('views_per_sub', '-')} | {r['url']} |")


TAG = re.compile(r"<[^>]+>")
TS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.\d{3} -->")


def vtt_to_text(path, chunk_s=30):
    """去掉 VTT 的逐词标签与滚动重复行，每 chunk_s 秒一段，段首带 [mm:ss]。"""
    segs, cur_start, cur, last_line, t = [], None, [], None, 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            m = TS.match(line)
            if m:
                h, mi, s = map(int, m.groups())
                t = h * 3600 + mi * 60 + s
                if cur_start is None:
                    cur_start = t
                if t - cur_start >= chunk_s and cur:
                    segs.append((cur_start, " ".join(cur)))
                    cur_start, cur = t, []
                continue
            if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or line.isdigit():
                continue
            text = TAG.sub("", line).strip()
            if text and text != last_line:
                cur.append(text)
                last_line = text
    if cur:
        segs.append((cur_start or 0, " ".join(cur)))
    return "\n".join(f"[{s // 60:02d}:{s % 60:02d}] {txt}" for s, txt in segs)


def cmd_transcript(a):
    need_ytdlp()
    os.makedirs(a.out, exist_ok=True)
    vid = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", a.url)
    vid = vid.group(1) if vid else a.url
    base = os.path.join(a.out, vid)
    common = ["yt-dlp", "--skip-download", "--sub-format", "vtt", "-t", "sleep", "--no-warnings",
              "-o", base + ".%(ext)s", a.url]
    # 第一轮：人工字幕（中/英）；第二轮：原语言自动字幕。从不请求自动翻译。
    attempts = [
        ("人工字幕", ["--write-subs", "--sub-langs", a.langs]),
        ("原语言自动字幕", ["--write-auto-subs", "--sub-langs", ".*-orig"]),
    ]
    for kind, extra in attempts:
        run(common[:1] + extra + common[1:])
        files = sorted(glob.glob(base + ".*.vtt"))
        if files:
            src = files[0]
            text = vtt_to_text(src)
            dst = base + ".txt"
            with open(dst, "w", encoding="utf-8") as f:
                f.write(f"# {a.url}\n# 来源：{kind}（{os.path.basename(src)}）"
                        f"{' · 自动识别，专有名词与数字须回画面/简介核对' if kind != '人工字幕' else ''}\n\n")
                f.write(text)
            print(json.dumps({"ok": True, "kind": kind, "vtt": src, "txt": dst,
                              "chars": len(text)}, ensure_ascii=False))
            return
    print(json.dumps({"ok": False, "url": a.url,
                      "next": "按 references/05 §5 降级：简介章节/置顶评论 → 作者博客或仓库 → 只记线索"},
                     ensure_ascii=False))
    sys.exit(2)


def cmd_info(a):
    """降级第一步：无字幕时取简介 + 章节 + 基本元数据。"""
    need_ytdlp()
    rc, o, err = run(["yt-dlp", "-j", "--skip-download", "--no-warnings", a.url], timeout=120)
    if rc != 0 or not o.strip():
        print(err.strip()[-800:], file=sys.stderr)
        sys.exit(4)
    d = json.loads(o.splitlines()[0])
    print(f"# {d.get('title')}\n频道：{d.get('channel')}（订阅 {fmt_views(d.get('channel_follower_count'))}）"
          f" · 播放 {fmt_views(d.get('view_count'))} · 发布 {d.get('upload_date')} · {a.url}")
    chs = d.get("chapters") or []
    if chs:
        print("\n## 章节")
        for c in chs:
            s = int(c.get("start_time") or 0)
            print(f"[{s // 60:02d}:{s % 60:02d}] {c.get('title')}")
    print("\n## 简介\n" + (d.get("description") or "（无简介）"))
    print("\n# 注意：这不是正文。据此写结论须标「未读正文·据简介/章节」。")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("-n", type=int, default=10, help="候选条数")
    s.add_argument("--meta", type=int, default=0, help="前 N 条补拉发布日期/订阅数（每条一次请求）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)
    t = sub.add_parser("transcript")
    t.add_argument("url")
    t.add_argument("--out", required=True, help="字幕存放目录（放调研目录，不放 skill 目录）")
    t.add_argument("--langs", default="zh-Hans,zh-Hant,zh-CN,zh-TW,zh,en,en-US,en-GB",
                   help="人工字幕优先语言")
    t.set_defaults(func=cmd_transcript)
    i = sub.add_parser("info", help="无字幕时取简介与章节（降级第一步）")
    i.add_argument("url")
    i.set_defaults(func=cmd_info)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
