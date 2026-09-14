#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术面信号自动选股脚本（零依赖 · 纯标准库，可直接跑在 GitHub Actions）

数据源（已实测绕过反爬，稳定可用）：
  - 粗筛：东方财富【延时】行情 push2delay.eastmoney.com（全市场快照，按成交额排序）
  - 精筛：腾讯历史日 K 线 web.ifzq.gtimg.cn（前复权，算指标）
  注：东方财富 push2his 接口在本环境/云端被代理拦截，故不用；akshare 底层也是这些源，
      引入反而增加依赖且更脆，因此保持零依赖直连。

技术面指标（全部纯 list 实现，不引 numpy/pandas，保证 Actions 秒起）：
  - 均线系统：MA20 / MA60（趋势方向）
  - MACD：DIF / DEA / HIST（12/26/9），含近期金叉检测
  - RSI(14)：Wilder 平滑（强势非超买过滤）
  - 布林带(20,2)：中轨/上轨/下轨（通道位置）
  - KDJ(9,3,3)：金叉/超买过滤
  - 量能：MA20 量能比（放量确认）

选股方式：打分制。硬门槛（趋势向上 + 放量 + 突破）必须过，其余指标按强度加分，
          按总分排序取 top N。比旧版「二值硬卡 MACD 金叉」更平滑、不再漏股。

写回：通过 GitHub REST API 更新（不存在则创建）名为 STOCK_LIST 的 Repository Variable。
      DSA 主任务已用 `vars.STOCK_LIST || secrets.STOCK_LIST`，会自动读取最新值。

环境变量：
  GITHUB_REPOSITORY  owner/repo（GitHub Actions 自动注入）
  GITHUB_TOKEN       用于写回 Variable（Actions 自动注入，需 actions: write 权限）
  UNIVERSE_SIZE      粗筛活跃股数量，默认 150
  MAX_STOCKS         最终入选上限，默认 25
  MIN_STOCKS         入选下限，不足则放宽条件 / 回退上期列表，默认 8
  DRY_RUN=1          只打印结果，不写回 Variable
  OUTPUT_FILE        可选，把结果同时写入本地文件
  STOCK_LIST_FALLBACK  可选，极端情况回退用的上期自选股（逗号分隔）
"""

import datetime
import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_REF_EM = "https://quote.eastmoney.com/"
_REF_TX = "https://gu.qq.com/"

_ctx = ssl.create_default_context()


def http_get_json(url, timeout=15, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 网络抖动重试
            last_err = exc
            if attempt < retries:
                time.sleep(1.0)
    raise last_err


def tx_code(code):
    """6 位 A 股代码 -> 腾讯格式 sh/sz 前缀；北交所等返回 None。"""
    if len(code) != 6 or not code.isdigit():
        return None
    if code[0] in ("6", "9"):
        return "sh" + code
    if code[0] in ("0", "3"):
        return "sz" + code
    return None


def get_universe(top_n):
    """东财延时行情：拉沪深 A 股快照，按成交额降序取活跃 top_n。"""
    fs = "m:0+t:2,m:0+t:23,m:1+t:2,m:1+t:23"  # 沪深主板 + 创业板 + 科创板
    fields = "f12,f14,f3,f5,f6,f62"
    url = (
        "https://push2delay.eastmoney.com/api/qt/clist/get?"
        "pn=1&pz=5000&po=1&np=1&fltt=2&invt=2&fid=f6&"
        f"fs={urllib.parse.quote(fs, safe=':')}&fields={fields}"
    )
    data = http_get_json(url)
    items = (data.get("data") or {}).get("diff") or []
    result = []
    for it in items:
        code = it.get("f12")
        name = it.get("f14") or ""
        if not code or not tx_code(code):
            continue
        if "ST" in name or "退" in name:
            continue
        result.append(
            {
                "code": code,
                "name": name,
                "change": it.get("f3"),
                "amount": it.get("f6") or 0,
                "net_inflow": it.get("f62"),
            }
        )
    result.sort(key=lambda x: x["amount"], reverse=True)
    return result[:top_n]


def get_kline(tcode, days=320):
    """腾讯日 K 线（前复权），返回 [{date,close,high,low,volume}]。"""
    end = datetime.date.today().strftime("%Y-%m-%d")
    start = (datetime.date.today() - datetime.timedelta(days=days * 1.6)).strftime("%Y-%m-%d")
    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        f"param={tcode},day,{start},{end},{days},qfq"
    )
    data = http_get_json(url)
    node = (data.get("data") or {}).get(tcode) or {}
    key = "qfqday" if "qfqday" in node else "day"
    arr = node.get(key) or []
    rows = []
    for p in arr:
        if len(p) < 6:
            continue
        try:
            rows.append(
                {
                    "date": p[0],
                    "open": float(p[1]),
                    "close": float(p[2]),
                    "high": float(p[3]),
                    "low": float(p[4]),
                    "volume": float(p[5]),  # 手
                }
            )
        except ValueError:
            continue
    return rows


# ---------- 指标（纯标准库实现） ----------

def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def ema(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(closes, fast=12, slow=26, signal=9):
    ef = ema(closes, fast)
    es = ema(closes, slow)
    dif = [ef[i] - es[i] for i in range(len(closes))]
    dea = ema(dif, signal)
    hist = [(dif[i] - dea[i]) * 2 for i in range(len(closes))]
    return dif, dea, hist


def rsi(closes, n=14):
    """Wilder RSI。返回最近一个值；数据不足返回 None。"""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def boll(closes, n=20, k=2.0):
    if len(closes) < n:
        return None, None, None
    window = closes[-n:]
    mid = sum(window) / n
    var = sum((x - mid) ** 2 for x in window) / n
    sd = math.sqrt(var)
    return mid, mid + k * sd, mid - k * sd


def kdj(highs, lows, closes, n=9, m1=3, m2=3):
    """返回 (K, D, J) 最近值；数据不足返回 (None,None,None)。"""
    if len(closes) < n:
        return None, None, None
    rsv_list = []
    for i in range(n - 1, len(closes)):
        hh = max(highs[i - n + 1 : i + 1])
        ll = min(lows[i - n + 1 : i + 1])
        if hh == ll:
            rsv_list.append(50.0)
        else:
            rsv_list.append((closes[i] - ll) / (hh - ll) * 100.0)
    # K/D 初值 50，按 EMA(m1/m2) 递推
    k_val = 50.0
    d_val = 50.0
    for rsv in rsv_list:
        k_val = (m1 - 1) / m1 * k_val + 1 / m1 * rsv
        d_val = (m2 - 1) / m2 * d_val + 1 / m2 * k_val
    j_val = 3 * k_val - 2 * d_val
    return k_val, d_val, j_val


def golden_cross_recent(dif, dea, within=20):
    """返回 (是否金叉, 距今天数)。金叉 = 最近 within 日内 DIF 上穿 DEA。"""
    n = len(dif)
    if n < 2:
        return False, 999
    for i in range(n - 1, max(n - 1 - within, 0) - 1, -1):
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            return True, (n - 1 - i)
    return False, 999


# ---------- 选股（打分制） ----------

def score_screen(rows, relaxed=False):
    """技术面打分。

    硬门槛（relaxed=False 必须全过）：
      1) 中期趋势向上：MA20 > MA60 且 收盘 > MA20
      2) 放量：当日量能 >= MA20量能 * 1.3
      3) 突破：收盘 >= 近 20 日最高价 * 0.995
    满足后按下列加分排序：
      + 量能比（每 0.5x 记 0.5 分，上限 2）
      + 突破强度（创新高 +1.5）
      + MACD 多头（DIF>DEA 且 HIST>0 +1.5；近 20 日金叉额外 +1）
      + RSI 处于 40~70 强势非超买 +1；>75 超买 -1
      + 收盘在布林中轨上方 +0.5
      + KDJ 金叉且 J<80 +1；J>100 超买 -0.5

    relaxed=True：只要求趋势向上 + 放量，其余全作加分（用于补足下限）。
    数据不足 60 根直接返回 None。
    """
    if len(rows) < 60:
        return None
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]

    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    if ma20 is None or ma60 is None:
        return None

    trend_up = (ma20 > ma60) and (closes[-1] > ma20)
    if not relaxed and not trend_up:
        return None

    vol_ma20 = sma(vols, 20)
    if vol_ma20 is None or vol_ma20 <= 0:
        return None
    vol_ratio = vols[-1] / vol_ma20
    if not relaxed and vol_ratio < 1.3:
        return None

    recent_high = max(highs[-21:-1]) if len(highs) >= 21 else max(highs[:-1])
    broke = closes[-1] >= recent_high * 0.995
    if not relaxed and not broke:
        return None

    # ---- 加分项 ----
    score = 0.0
    score += min(vol_ratio / 0.5 * 0.5, 2.0)  # 量能
    if broke:
        score += 1.5

    dif, dea, hist = macd(closes)
    if dif[-1] > dea[-1] and hist[-1] > 0:
        score += 1.5
    crossed, days = golden_cross_recent(dif, dea, within=20)
    if crossed:
        score += 1.0

    r = rsi(closes, 14)
    if r is not None:
        if 40 <= r <= 70:
            score += 1.0
        elif r > 75:
            score -= 1.0

    mid, upper, lower = boll(closes, 20, 2.0)
    if mid is not None and closes[-1] > mid:
        score += 0.5

    k_val, d_val, j_val = kdj(highs, lows, closes)
    if k_val is not None:
        if k_val > d_val and j_val < 80:
            score += 1.0
        elif j_val > 100:
            score -= 0.5

    return {
        "score": round(score, 2),
        "days_since_cross": days,
        "vol_ratio": round(vol_ratio, 2),
        "rsi": round(r, 1) if r is not None else None,
        "close": closes[-1],
    }


def run(universe_size, max_stocks, min_stocks):
    print(f"[auto-screen] 拉取活跃股 top {universe_size} ...")
    universe = get_universe(universe_size)
    print(f"[auto-screen] 候选池 {len(universe)} 只")

    strict, relaxed = [], []
    for idx, u in enumerate(universe, 1):
        tcode = tx_code(u["code"])
        if not tcode:
            continue
        try:
            rows = get_kline(tcode)
            r = score_screen(rows, relaxed=False)
            if r:
                strict.append((u["code"], u["name"], r))
            else:
                r2 = score_screen(rows, relaxed=True)
                if r2:
                    relaxed.append((u["code"], u["name"], r2))
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
            print(f"[auto-screen] 跳过 {u['code']}: {exc}")
        if idx % 25 == 0:
            print(f"[auto-screen] 已扫描 {idx}/{len(universe)}")
        time.sleep(0.08)  # 轻量限流

    strict.sort(key=lambda x: -x[2]["score"])
    codes = [p[0] for p in strict[:max_stocks]]
    detail = strict[:max_stocks]

    # 不足下限：补充放宽信号（仅趋势+放量，其余作加分）
    if len(codes) < min_stocks and relaxed:
        relaxed.sort(key=lambda x: -x[2]["score"])
        for p in relaxed:
            if p[0] not in codes:
                codes.append(p[0])
                detail.append(p)
            if len(codes) >= min_stocks:
                break

    # 仍不足：回退上期列表
    if len(codes) < min_stocks:
        fb = os.getenv("STOCK_LIST_FALLBACK", "")
        fb_codes = [c.strip() for c in fb.split(",") if c.strip()]
        for c in fb_codes:
            if c not in codes:
                codes.append(c)
            if len(codes) >= min_stocks:
                break
        if fb_codes:
            print(f"[auto-screen] 信号不足，已回退上期列表 {len(fb_codes)} 只")

    print(f"[auto-screen] 严格命中 {len(strict)} 只，最终入选 {len(codes)} 只")
    for p in detail:
        r = p[2]
        print(
            f"  {p[0]} {p[1]} | 分{r['score']} 金叉{r['days_since_cross']}天 "
            f"放量{r['vol_ratio']}x RSI{r['rsi']} 收{r['close']}"
        )
    return codes


def update_variable(repo, name, value, token):
    base = f"https://api.github.com/repos/{repo}/actions/variables"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    exists = False
    try:
        req = urllib.request.Request(f"{base}/{name}", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15, context=_ctx) as r:
            exists = r.status == 200
    except urllib.error.HTTPError as e:
        exists = e.code != 404
    except Exception:  # noqa: BLE001
        exists = False

    body = json.dumps({"value": value}).encode("utf-8")
    if exists:
        req = urllib.request.Request(
            f"{base}/{name}", data=body, headers=headers, method="PATCH"
        )
        verb = "PATCH"
    else:
        req = urllib.request.Request(
            base,
            data=json.dumps({"name": name, "value": value}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        verb = "POST"
    with urllib.request.urlopen(req, timeout=15, context=_ctx) as r:
        print(f"[auto-screen] GitHub API {verb} variable {name}: HTTP {r.status}")


def main():
    universe_size = int(os.getenv("UNIVERSE_SIZE", "150"))
    max_stocks = int(os.getenv("MAX_STOCKS", "25"))
    min_stocks = int(os.getenv("MIN_STOCKS", "8"))

    codes = run(universe_size, max_stocks, min_stocks)
    value = ",".join(codes)
    print(f"[auto-screen] 结果({len(codes)}): {value}")

    out = os.getenv("OUTPUT_FILE")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(value)
        print(f"[auto-screen] 已写入本地文件 {out}")

    if os.getenv("DRY_RUN") == "1":
        print("[auto-screen] DRY_RUN=1，不写回 Variable")
        return

    repo = os.getenv("GITHUB_REPOSITORY")
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if repo and token:
        update_variable(repo, "STOCK_LIST", value, token)
    else:
        print("[auto-screen] 缺少 GITHUB_REPOSITORY / token，跳过写回")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[auto-screen] 致命错误: {exc}", file=sys.stderr)
        sys.exit(1)
