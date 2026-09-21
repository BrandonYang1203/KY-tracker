#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用法：
    python3 kt_tracker.py snap                 # 抓一次快照存進 votes.db
    python3 kt_tracker.py report --me 079      # 產生 report.html
    python3 kt_tracker.py snap --then-report --me 079
    python3 kt_tracker.py loop --minutes 30 --me 079   # 常駐，每 30 分鐘一輪
    python3 kt_tracker.py export               # 匯出 history.csv（長格式）

只需要標準函式庫，不用裝任何套件。
"""

import argparse
import csv
import html
import math
import os
import re
import sqlite3
import sys
import time
import urllib.request
import gzip
import io
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))  # Asia/Taipei

URL = ("https://hellokittyemotion.sanrio.com.tw/"
       "%E4%BD%9C%E5%93%81%E4%B8%80%E8%A6%BD-hello-kitty-x-"
       "%E5%8F%B0%E7%81%A3%E6%84%9F%E6%80%A7%E6%94%9D%E5%BD%B1%E5%B1%95/")

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "votes.db")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


# ---------------------------------------------------------------- 抓取與解析

def fetch(url=URL, timeout=120):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


HEAD_FULL = re.compile(r"^(\d{3})[\s\u3000]+(\S.*)$")
HEAD_ONLY = re.compile(r"^(\d{3})$")
VOTE_RE = re.compile(r"^(\d+)\s*票$")
SKIP = {"投票", "", "✕"}


def extract_entries(raw_html):
    """回傳 [(編號, 名稱, 票數), ...]。對版面改動容忍度做高一點。"""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw_html)
    s = re.sub(r"(?s)<[^>]+>", "\n", s)
    s = html.unescape(s)
    lines = [ln.strip() for ln in s.split("\n")]
    lines = [ln for ln in lines if ln]

    out, cur = [], None
    for ln in lines:
        m = VOTE_RE.match(ln)
        if m and cur:
            out.append((cur[0], cur[1].strip() or "(無標題)", int(m.group(1))))
            cur = None
            continue
        m = HEAD_FULL.match(ln)
        if m:
            cur = [m.group(1), m.group(2)]
            continue
        m = HEAD_ONLY.match(ln)
        if m:
            cur = [m.group(1), ""]
            continue
        # 標題被拆到下一行的情況
        if cur and not cur[1] and ln not in SKIP:
            cur[1] = ln

    # 去重（保留第一次出現）
    seen, uniq = set(), []
    for no, name, v in out:
        if no in seen:
            continue
        seen.add(no)
        uniq.append((no, name, v))
    return uniq


# ---------------------------------------------------------------- 資料庫

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS snap(
        ts TEXT NOT NULL, no TEXT NOT NULL, name TEXT, votes INTEGER,
        PRIMARY KEY (ts, no))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_no ON snap(no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_ts ON snap(ts)")
    return conn


def save_snapshot(entries, ts=None):
    ts = ts or datetime.now(TZ).replace(microsecond=0).isoformat()
    conn = db()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO snap(ts, no, name, votes) VALUES (?,?,?,?)",
            [(ts, no, name, v) for no, name, v in entries])
    conn.close()
    return ts


def all_times(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT ts FROM snap ORDER BY ts")]


def snapshot_at(conn, ts):
    rows = conn.execute(
        "SELECT no, name, votes FROM snap WHERE ts=? ORDER BY no", (ts,)).fetchall()
    return {no: (name, v) for no, name, v in rows}


def nearest_before(times, target_iso):
    prior = [t for t in times if t <= target_iso]
    return prior[-1] if prior else None


def ranked(snap):
    """snap: {no:(name,votes)} -> {no: rank}（同票以編號小的在前）"""
    items = sorted(snap.items(), key=lambda kv: (-kv[1][1], kv[0]))
    return {no: i + 1 for i, (no, _) in enumerate(items)}, items


# ---------------------------------------------------------------- SVG 圖表

PALETTE = {
    "ink": "#1b1a1f",
    "muted": "#8b8791",
    "line": "#e3dfe6",
    "accent": "#d21f4b",
    "accent2": "#3a6ea8",
    "bg": "#ffffff",
}


def _nice(lo, hi):
    if hi <= lo:
        hi = lo + 1
    span = hi - lo
    step = 10 ** math.floor(math.log10(span / 3 if span > 3 else 1))
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= 5:
            step = step * m
            break
    lo2 = math.floor(lo / step) * step
    hi2 = math.ceil(hi / step) * step
    ticks = []
    t = lo2
    while t <= hi2 + 1e-9:
        ticks.append(t)
        t += step
    return lo2, hi2, ticks


def line_chart(series, width=760, height=230, color=None, invert=False,
               unit="", label=""):
    """series: [(datetime, value)]，invert=True 用於名次（1 在上）。"""
    color = color or PALETTE["accent"]
    if len(series) < 2:
        return ('<p class="empty">還需要至少兩次快照才畫得出趨勢。'
                '先讓它跑一段時間。</p>')
    pad_l, pad_r, pad_t, pad_b = 52, 14, 14, 26
    W, H = width - pad_l - pad_r, height - pad_t - pad_b
    xs = [d.timestamp() for d, _ in series]
    ys = [v for _, v in series]
    x0, x1 = min(xs), max(xs)
    lo, hi, ticks = _nice(min(ys), max(ys))

    def px(x):
        return pad_l + (x - x0) / (x1 - x0 or 1) * W

    def py(y):
        f = (y - lo) / (hi - lo or 1)
        return pad_t + (f * H if invert else (1 - f) * H)

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" '
             f'role="img" aria-label="{html.escape(label)}">']
    for t in ticks:
        y = py(t)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" '
                     f'y2="{y:.1f}" stroke="{PALETTE["line"]}" stroke-width="1"/>')
        txt = f"{int(t)}" if abs(t - int(t)) < 1e-9 else f"{t:g}"
        parts.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" text-anchor="end" '
                     f'class="ax">{txt}{unit}</text>')
    d = " ".join(("M" if i == 0 else "L") + f"{px(x):.1f},{py(y):.1f}"
                 for i, (x, y) in enumerate(zip(xs, ys)))
    parts.append(f'<path d="{d}" fill="none" stroke="{color}" '
                 f'stroke-width="2" stroke-linejoin="round"/>')
    lx, ly = px(xs[-1]), py(ys[-1])
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" fill="{color}"/>')
    for x, lab in ((x0, series[0][0]), (x1, series[-1][0])):
        anchor = "start" if x == x0 else "end"
        parts.append(f'<text x="{px(x):.1f}" y="{height-6}" text-anchor="{anchor}" '
                     f'class="ax">{lab.strftime("%m/%d %H:%M")}</text>')
    parts.append("</svg>")
    return "".join(parts)


def bar_chart(labels, values, width=760, height=200, color=None, unit=""):
    color = color or PALETTE["accent2"]
    if not values or max(values) == 0:
        return '<p class="empty">還沒有足夠的增票資料。</p>'
    pad_l, pad_r, pad_t, pad_b = 46, 10, 12, 24
    W, H = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(values)
    bw = W / n
    hi = _nice(0, max(values))[1]
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart">']
    for frac in (0, .5, 1):
        y = pad_t + (1 - frac) * H
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" '
                     f'stroke="{PALETTE["line"]}"/>')
        parts.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" text-anchor="end" '
                     f'class="ax">{int(hi*frac)}{unit}</text>')
    for i, (lab, v) in enumerate(zip(labels, values)):
        h = v / hi * H
        x = pad_l + i * bw + bw * 0.15
        parts.append(f'<rect x="{x:.1f}" y="{pad_t+H-h:.1f}" width="{bw*0.7:.1f}" '
                     f'height="{h:.1f}" fill="{color}" rx="1.5"/>')
        if n <= 24 and i % 2 == 0:
            parts.append(f'<text x="{x+bw*0.35:.1f}" y="{height-6}" '
                         f'text-anchor="middle" class="ax">{lab}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- 報表

def arrow(delta):
    if delta is None:
        return '<span class="flat">新</span>'
    if delta > 0:
        return f'<span class="up">▲{delta}</span>'
    if delta < 0:
        return f'<span class="down">▼{abs(delta)}</span>'
    return '<span class="flat">—</span>'


def fmt_gain(g):
    if g is None:
        return '<span class="flat">—</span>'
    return f'<span class="gain">+{g}</span>' if g > 0 else '<span class="flat">0</span>'


def build_report(me=None, target_rank=10, out="report.html"):
    conn = db()
    times = all_times(conn)
    if not times:
        print("資料庫還是空的，先跑一次 snap。")
        return
    now_ts = times[-1]
    now_dt = datetime.fromisoformat(now_ts)
    cur = snapshot_at(conn, now_ts)
    cur_rank, cur_items = ranked(cur)

    def past(hours):
        t = nearest_before(times, (now_dt - timedelta(hours=hours)).isoformat())
        if not t or t == now_ts:
            return None, None, None
        s = snapshot_at(conn, t)
        r, _ = ranked(s)
        return t, s, r

    t24, s24, r24 = past(24)
    t01, s01, r01 = past(1)
    t72, s72, _ = past(72)

    def gain(no, snap):
        if not snap or no not in snap:
            return None
        return cur[no][1] - snap[no][1]

    def rankmove(no, rmap):
        if not rmap or no not in rmap:
            return None
        return rmap[no] - cur_rank[no]   # 正數 = 名次進步

    total_now = sum(v for _, v in cur.values())
    total_24 = sum(v for _, v in s24.values()) if s24 else None

    # 每小時全站增票（用相鄰快照差，落在整點桶）
    hourly = {}
    prev_ts, prev_tot = None, None
    for t in times:
        s = snapshot_at(conn, t)
        tot = sum(v for _, v in s.values())
        if prev_tot is not None and tot >= prev_tot:
            hourly.setdefault(datetime.fromisoformat(t).hour, []).append(tot - prev_tot)
        prev_ts, prev_tot = t, tot
    hours = list(range(24))
    hourly_avg = [round(sum(hourly.get(h, [0])) / max(len(hourly.get(h, [1])), 1), 1)
                  for h in hours]

    # 全站總票數趨勢
    total_series = []
    for t in times:
        s = snapshot_at(conn, t)
        total_series.append((datetime.fromisoformat(t), sum(v for _, v in s.values())))

    # 我的歷史
    my_votes_series, my_rank_series = [], []
    if me:
        for t in times:
            s = snapshot_at(conn, t)
            if me in s:
                r, _ = ranked(s)
                d = datetime.fromisoformat(t)
                my_votes_series.append((d, s[me][1]))
                my_rank_series.append((d, r[me]))

    css = """
    :root{--ink:#1b1a1f;--muted:#8b8791;--line:#e3dfe6;--accent:#d21f4b;
      --accent2:#3a6ea8;--paper:#fff;--soft:#faf7f8;--up:#1c7a4a;--down:#b2364e;}
    *{box-sizing:border-box}
    body{margin:0;padding:32px 20px 72px;background:var(--paper);color:var(--ink);
      font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif;
      font-size:15px;line-height:1.6;-webkit-text-size-adjust:100%}
    .wrap{max-width:880px;margin:0 auto}
    h1{font-size:22px;font-weight:700;margin:0 0 4px;letter-spacing:.01em}
    .stamp{color:var(--muted);font-size:13px;margin:0 0 32px}
    h2{font-size:15px;font-weight:700;margin:40px 0 12px;
      padding-bottom:8px;border-bottom:2px solid var(--ink)}
    .hero{border:1px solid var(--line);border-left:4px solid var(--accent);
      padding:20px 22px;background:var(--soft);margin-bottom:8px}
    .hero .title{font-size:17px;font-weight:700;margin:0 0 14px}
    .stats{display:flex;flex-wrap:wrap;gap:28px 40px}
    .stat .k{font-size:12px;color:var(--muted)}
    .stat .v{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums;
      line-height:1.15}
    .stat .v small{font-size:14px;font-weight:500;color:var(--muted)}
    .note{margin:14px 0 0;color:var(--muted);font-size:13px}
    table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
    th,td{padding:7px 8px;text-align:right;border-bottom:1px solid var(--line)}
    th{font-size:12px;color:var(--muted);font-weight:500;white-space:nowrap}
    td.name,th.name{text-align:left;font-variant-numeric:normal}
    td.no{color:var(--muted);font-size:13px}
    tr.me{background:#fdeef2}
    tr.me td{font-weight:700}
    tr.cut td{border-bottom:2px dashed var(--accent)}
    .up{color:var(--up);font-weight:600}
    .down{color:var(--down);font-weight:600}
    .flat{color:var(--muted)}
    .gain{color:var(--accent);font-weight:600}
    .chart{width:100%;height:auto;display:block;margin:4px 0 6px}
    .ax{font-size:10px;fill:var(--muted);font-family:inherit}
    .empty{color:var(--muted);font-size:13px;padding:14px 0}
    .grid{display:grid;gap:26px}
    @media(max-width:620px){.stats{gap:18px 26px}.stat .v{font-size:24px}
      th,td{padding:6px 5px;font-size:13px}}
    """

    P = []
    P.append("<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>")
    P.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    P.append("<title>票數追蹤</title><style>" + css + "</style></head><body><div class='wrap'>")
    P.append("<h1>台灣感性攝影展 · 票數追蹤</h1>")
    P.append(f"<p class='stamp'>資料時間 {now_dt.strftime('%Y-%m-%d %H:%M')}"
             f"　·　{len(cur)} 件作品　·　已累積 {len(times)} 次快照</p>")

    # 我的狀態
    if me and me in cur:
        name, v = cur[me]
        rk = cur_rank[me]
        g24, g01 = gain(me, s24), gain(me, s01)
        mv24 = rankmove(me, r24)
        above = cur_items[rk - 2] if rk >= 2 else None
        below = cur_items[rk] if rk < len(cur_items) else None
        P.append("<div class='hero'>")
        P.append(f"<p class='title'>{html.escape(name)}　<span style='color:var(--muted);font-weight:400'>#{me}</span></p>")
        P.append("<div class='stats'>")
        P.append(f"<div class='stat'><div class='k'>目前名次</div>"
                 f"<div class='v'>{rk}<small> / {len(cur)}</small></div></div>")
        P.append(f"<div class='stat'><div class='k'>票數</div><div class='v'>{v}</div></div>")
        P.append(f"<div class='stat'><div class='k'>24 小時增票</div>"
                 f"<div class='v'>{'+' + str(g24) if g24 is not None else '—'}</div></div>")
        P.append(f"<div class='stat'><div class='k'>24 小時名次</div>"
                 f"<div class='v' style='font-size:22px'>{arrow(mv24)}</div></div>")
        P.append("</div>")

        lines = []
        if above:
            an, (anm, av) = above
            lines.append(f"前一名是 #{an}「{html.escape(anm)}」{av} 票，差 {av - v} 票。")
        if below:
            bn, (bnm, bv) = below
            lines.append(f"後一名是 #{bn}「{html.escape(bnm)}」{bv} 票，領先 {v - bv} 票。")
        # 進榜配速
        if target_rank and len(cur_items) >= target_rank:
            cut_no, (cut_name, cut_v) = cur_items[target_rank - 1]
            if rk > target_rank:
                need = cut_v - v + 1
                # 近 3 天日均
                g72 = gain(me, s72)
                pace = (g72 / 3) if g72 is not None else (g24 or 0)
                if pace > 0:
                    eta = need / pace
                    lines.append(f"要擠進前 {target_rank} 名還差 {need} 票；"
                                 f"以你近期日均 {pace:.1f} 票的速度，約需 {eta:.1f} 天"
                                 f"（前提是門檻不動，而它會動）。")
                else:
                    lines.append(f"要擠進前 {target_rank} 名還差 {need} 票，"
                                 f"但你近期增速是 0，照這樣不會追上。")
            else:
                lines.append(f"你在前 {target_rank} 名內。守門線是第 {target_rank + 1} 名"
                             f"（{cur_items[target_rank][1][1]} 票），"
                             f"緩衝 {v - cur_items[target_rank][1][1]} 票。")
        if lines:
            P.append("<p class='note'>" + "<br>".join(lines) + "</p>")
        P.append("</div>")
    elif me:
        P.append(f"<div class='hero'><p class='title'>找不到編號 {html.escape(me)}</p>"
                 "<p class='note'>檢查一下編號格式，要三位數，例如 079。</p></div>")

    # 我的曲線
    if me and len(my_votes_series) >= 2:
        P.append("<h2>你的票數與名次</h2>")
        P.append(line_chart(my_votes_series, label="我的票數", unit=""))
        P.append("<p class='note'>票數</p>")
        P.append(line_chart(my_rank_series, color=PALETTE["accent2"], invert=True,
                            label="我的名次"))
        P.append("<p class='note'>名次（愈上面愈好）</p>")

    # 排行榜
    show = max(target_rank + 10, 25)
    P.append(f"<h2>排行榜 前 {show} 名</h2>")
    P.append("<table><thead><tr><th>#</th><th>24h</th><th class='name'>作品</th>"
             "<th>編號</th><th>票數</th><th>+1h</th><th>+24h</th></tr></thead><tbody>")
    for i, (no, (name, v)) in enumerate(cur_items[:show], start=1):
        cls = []
        if no == me:
            cls.append("me")
        if i == target_rank:
            cls.append("cut")
        c = f" class='{' '.join(cls)}'" if cls else ""
        P.append(f"<tr{c}><td>{i}</td><td>{arrow(rankmove(no, r24))}</td>"
                 f"<td class='name'>{html.escape(name)}</td><td class='no'>{no}</td>"
                 f"<td>{v}</td><td>{fmt_gain(gain(no, s01))}</td>"
                 f"<td>{fmt_gain(gain(no, s24))}</td></tr>")
    P.append("</tbody></table>")
    if target_rank and len(cur_items) > target_rank:
        P.append(f"<p class='note'>虛線是第 {target_rank} 名的位置。</p>")

    # 衝最快
    if s24:
        movers = sorted(((gain(no, s24) or 0, no) for no in cur),
                        reverse=True)[:12]
        P.append("<h2>24 小時衝最快</h2>")
        P.append("<table><thead><tr><th>+票</th><th class='name'>作品</th>"
                 "<th>編號</th><th>現名次</th><th>名次變動</th></tr></thead><tbody>")
        for g, no in movers:
            if g <= 0:
                continue
            nm = cur[no][0]
            c = " class='me'" if no == me else ""
            P.append(f"<tr{c}><td><span class='gain'>+{g}</span></td>"
                     f"<td class='name'>{html.escape(nm)}</td><td class='no'>{no}</td>"
                     f"<td>{cur_rank[no]}</td><td>{arrow(rankmove(no, r24))}</td></tr>")
        P.append("</tbody></table>")
        P.append("<p class='note'>每人每天一票，所以這欄約等於對方昨天動員到的人數。"
                 "突然從個位數跳到幾十票，通常是有人開始在社群拉票。</p>")

    # 作者合計
    by_author = {}
    for no, (name, v) in cur.items():
        key = name.strip().lower()
        a = by_author.setdefault(key, {"name": name, "n": 0, "v": 0, "best": 0})
        a["n"] += 1
        a["v"] += v
        a["best"] = max(a["best"], v)
    multi = sorted((a for a in by_author.values() if a["n"] >= 2),
                   key=lambda a: -a["v"])[:10]
    if multi:
        P.append("<h2>同名多件作品</h2>")
        P.append("<table><thead><tr><th class='name'>名稱</th><th>件數</th>"
                 "<th>票數合計</th><th>最高單件</th></tr></thead><tbody>")
        for a in multi:
            P.append(f"<tr><td class='name'>{html.escape(a['name'])}</td>"
                     f"<td>{a['n']}</td><td>{a['v']}</td><td>{a['best']}</td></tr>")
        P.append("</tbody></table>")
        P.append("<p class='note'>票分散在多件的人，單件威脅較小；"
                 "真正的對手是把票集中在一件上的。</p>")

    # 全站
    P.append("<h2>全站動態</h2>")
    P.append("<div class='stats'>")
    P.append(f"<div class='stat'><div class='k'>總票數</div><div class='v'>{total_now}</div></div>")
    if total_24 is not None:
        P.append(f"<div class='stat'><div class='k'>24 小時全站增票</div>"
                 f"<div class='v'>+{total_now - total_24}</div></div>")
    P.append(f"<div class='stat'><div class='k'>作品數</div><div class='v'>{len(cur)}</div></div>")
    P.append("</div>")
    P.append(line_chart(total_series, color=PALETTE["accent2"], label="全站總票數"))
    P.append("<p class='note'>全站總票數</p>")
    if any(hourly_avg):
        P.append(bar_chart([f"{h}" for h in hours], hourly_avg))
        P.append("<p class='note'>各時段平均增票（0–23 時）。"
                 "看得出大家習慣什麼時候投票，提醒自己的人也挑那之前。</p>")

    P.append("</div></body></html>")

    path = out if os.path.isabs(out) else os.path.join(HERE, out)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(P))
    conn.close()
    print(f"報表已產生：{path}")


# ---------------------------------------------------------------- 指令

def cmd_snap(args):
    soft = getattr(args, "soft_fail", False)
    attempts = 4
    raw = None
    for i in range(1, attempts + 1):
        try:
            raw = fetch()
            break
        except Exception as e:
            print(f"第 {i} 次抓取失敗：{e}")
            if i < attempts:
                wait = 15 * i
                print(f"  {wait} 秒後重試")
                time.sleep(wait)
    if raw is None:
        print("連續失敗，這一輪跳過。下次排程會再試。")
        return 0 if soft else 1

    entries = extract_entries(raw)
    if len(entries) < 50:
        print(f"只解析到 {len(entries)} 筆，網站版面可能改了，這次不存。")
        return 0 if soft else 1
    ts = save_snapshot(entries)
    total = sum(v for _, _, v in entries)
    print(f"[{ts}] {len(entries)} 件，總票數 {total}")
    if args.me:
        for no, name, v in entries:
            if no == args.me:
                rk = sorted(entries, key=lambda e: (-e[2], e[0])).index((no, name, v)) + 1
                print(f"  你的 #{no}「{name}」：{v} 票，第 {rk} 名")
    return 0


def cmd_report(args):
    build_report(me=args.me, target_rank=args.target, out=args.out)
    return 0


def cmd_loop(args):
    print(f"每 {args.minutes} 分鐘抓一次，Ctrl+C 停止。")
    while True:
        try:
            cmd_snap(args)
            build_report(me=args.me, target_rank=args.target, out=args.out)
        except KeyboardInterrupt:
            print("\n停止。")
            return 0
        except Exception as e:
            print(f"這輪出錯（略過）：{e}")
        try:
            time.sleep(args.minutes * 60)
        except KeyboardInterrupt:
            print("\n停止。")
            return 0


def _data_files(d):
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".csv")]


def cmd_ingest(args):
    """把 data/*.csv（差分格式）讀回 votes.db，用前值遞補成完整快照。"""
    d = args.dir if os.path.isabs(args.dir) else os.path.join(HERE, args.dir)
    files = _data_files(d)
    if not files:
        print(f"{d} 沒有資料檔，當作全新開始。")
        return 0
    state = {}          # no -> (name, votes)
    pending_ts = None
    conn = db()
    rows_out, n_snaps = [], 0

    def flush():
        nonlocal rows_out, n_snaps
        if pending_ts and state:
            rows_out.extend((pending_ts, no, nm, v) for no, (nm, v) in state.items())
            n_snaps += 1

    for path in files:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if not row or row[0] in ("時間", "ts"):
                    continue
                ts, no, name, votes = row[0], row[1], row[2], int(row[3])
                if ts != pending_ts:
                    flush()
                    pending_ts = ts
                state[no] = (name, votes)
    flush()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO snap(ts,no,name,votes) VALUES (?,?,?,?)", rows_out)
    conn.close()
    print(f"讀回 {n_snaps} 次快照（{len(rows_out)} 列）")
    return 0


def cmd_emit(args):
    """把最新一次快照中『有變動的部分』附加到 data/YYYY-MM.csv。"""
    conn = db()
    times = all_times(conn)
    if not times:
        print("資料庫是空的。")
        return 1
    now_ts = times[-1]
    cur = snapshot_at(conn, now_ts)
    prev = snapshot_at(conn, times[-2]) if len(times) >= 2 else {}
    conn.close()

    changed = [(no, nm, v) for no, (nm, v) in sorted(cur.items())
               if prev.get(no, (None, None)) != (nm, v)]
    if not changed:
        print("這次沒有任何變動，不寫檔。")
        return 0

    d = args.dir if os.path.isabs(args.dir) else os.path.join(HERE, args.dir)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, now_ts[:7] + ".csv")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["時間", "編號", "名稱", "票數"])
        for no, nm, v in changed:
            w.writerow([now_ts, no, nm, v])
    print(f"寫入 {len(changed)} 列變動到 {path}")
    return 0


def cmd_export(args):
    conn = db()
    path = os.path.join(HERE, args.out if args.out.endswith(".csv") else "history.csv")
    rows = conn.execute("SELECT ts, no, name, votes FROM snap ORDER BY ts, no")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["時間", "編號", "名稱", "票數"])
        n = 0
        for r in rows:
            w.writerow(r)
            n += 1
    conn.close()
    print(f"匯出 {n} 列到 {path}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="台灣感性攝影展票數追蹤")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--me", help="你的作品編號，三位數如 079")
        p.add_argument("--target", type=int, default=10, help="想擠進的名次，預設 10")
        p.add_argument("--out", default="report.html")

    p = sub.add_parser("snap", help="抓一次快照")
    common(p)
    p.add_argument("--then-report", action="store_true")
    p.add_argument("--soft-fail", action="store_true",
                   help="抓不到時不視為錯誤（排程用）")

    p = sub.add_parser("report", help="產生 HTML 報表")
    common(p)

    p = sub.add_parser("loop", help="常駐定時抓取")
    common(p)
    p.add_argument("--minutes", type=int, default=30)

    p = sub.add_parser("export", help="匯出長格式 CSV")
    p.add_argument("--out", default="history.csv")

    p = sub.add_parser("ingest", help="從 data/*.csv 還原資料庫（CI 用）")
    p.add_argument("--dir", default="data")

    p = sub.add_parser("emit", help="把最新快照的變動附加到 data/（CI 用）")
    p.add_argument("--dir", default="data")

    args = ap.parse_args()
    if args.cmd == "ingest":
        return cmd_ingest(args)
    if args.cmd == "emit":
        return cmd_emit(args)
    if args.cmd == "snap":
        rc = cmd_snap(args)
        if getattr(args, "then_report", False) and rc == 0:
            cmd_report(args)
        return rc
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "loop":
        return cmd_loop(args)
    if args.cmd == "export":
        return cmd_export(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
