#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地回测脚本：用 backtrader 验证 auto_screen 选股 + 趋势持有策略的历史表现。

【用途】
  auto_screen 每天选出技术面强势股，但「选出≠赚钱」。这个脚本把选出的股票
  拿去回测一个与选股逻辑同源的趋势策略（趋势向上买入、破均线/超期卖出），
  看这套打法到底赚不赚、胜率多少、最大回撤多大。

【运行】（需本地 Python + backtrader + pandas，不在 GitHub Actions 跑）
  # 先用 auto_screen 选股再回测（DRY_RUN 不写回变量）
  python backtest_auto_screen.py
  # 直接指定股票列表回测
  python backtest_auto_screen.py --codes 600519,000001,300750
  # 调参数
  python backtest_auto_screen.py --maxhold 20 --cash 100000 --days 250

【策略】TrendHold
  买入：收盘 > MA20 > MA60（趋势向上，与选股同向）
  卖出：收盘 < MA20（短均失守） 或 单笔持有达到 maxhold 个交易日
  仓位：每次 95% 资金；佣金 0.03% + 卖出印花税 0.05%（贴近 A 股）

数据源复用 auto_screen.get_kline（腾讯前复权日 K，已验证可用）。
"""

import argparse
import datetime
import os
import sys

import backtrader as bt
import pandas as pd

# 复用选股脚本的数据获取
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_screen import get_kline, tx_code, run  # noqa: E402


class TrendHold(bt.Strategy):
    params = dict(ma_fast=20, ma_slow=60, maxhold=15, printlog=False)

    def __init__(self):
        self.ma_f = bt.indicators.SMA(period=self.p.ma_fast)
        self.ma_s = bt.indicators.SMA(period=self.p.ma_slow)
        self.entry_bar = None

    def next(self):
        if not self.position:
            if self.data.close[0] > self.ma_f[0] > self.ma_s[0]:
                self.buy()
                self.entry_bar = len(self)
        else:
            held = len(self) - self.entry_bar
            if self.data.close[0] < self.ma_f[0] or held >= self.p.maxhold:
                self.sell()


def load_df(code, days=250):
    """拉腾讯日 K 并转 pandas DataFrame（date 索引）。"""
    tcode = tx_code(code)
    if not tcode:
        return None
    rows = get_kline(tcode, days=days + 60)
    if len(rows) < 60:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    # 单位换算：腾讯 volume 为「手」，转「股」便于阅读（不影响收益比例）
    df["volume"] = df["volume"] * 100
    df = df[["open", "high", "low", "close", "volume"]]
    return df


def backtest_one(code, name, args):
    df = load_df(code, days=args.days)
    if df is None or len(df) < 60:
        return None
    cerebro = bt.Cerebro()
    cerebro.addsizer(bt.sizers.PercentSizer, percents=95)
    data = bt.feeds.PandasData(
        dataname=df,
        fromdate=df.index[-args.days],
        todate=df.index[-1],
    )
    cerebro.adddata(data)
    cerebro.addstrategy(TrendHold, maxhold=args.maxhold)
    cerebro.broker.setcash(float(args.cash))
    # 近似 A 股成本：佣金 0.03% + 卖出印花税 0.05%，backtrader 简单模型统一按
    # 双边 ~0.08% 计（不影响「策略是否赚钱」的方向判断）。
    cerebro.broker.setcommission(
        commission=0.0008,
        commtype=bt.CommInfoBase.COMM_PERC,
        stocklike=True,
    )
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="ret")

    start_val = cerebro.broker.getvalue()
    results = cerebro.run()
    strat = results[0]
    end_val = cerebro.broker.getvalue()

    ta = strat.analyzers.trades.get_analysis()
    total_trades = ta.get("total", {}).get("total", 0)
    won = ta.get("won", {}).get("total", 0)
    lost = ta.get("lost", {}).get("total", 0)
    dd = strat.analyzers.dd.get_analysis()
    max_dd = dd.get("max", {}).get("drawdown", 0.0)

    return {
        "code": code,
        "name": name,
        "total_return_pct": round((end_val / start_val - 1) * 100, 2),
        "trades": total_trades,
        "win_rate": round(won / total_trades * 100, 1) if total_trades else 0.0,
        "won": won,
        "lost": lost,
        "max_drawdown_pct": round(float(max_dd), 2),
        "end_value": round(end_val, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", help="逗号分隔股票代码，如 600519,000001")
    ap.add_argument("--maxhold", type=int, default=15)
    ap.add_argument("--cash", type=float, default=100000.0)
    ap.add_argument("--days", type=int, default=250)
    args = ap.parse_args()

    if args.codes:
        pairs = []
        for c in args.codes.split(","):
            c = c.strip()
            if c:
                pairs.append((c, c))
    else:
        print("[backtest] 无 --codes，先跑 auto_screen 选股（DRY_RUN）...")
        os.environ["DRY_RUN"] = "1"
        codes = run(
            int(os.getenv("UNIVERSE_SIZE", "150")),
            int(os.getenv("MAX_STOCKS", "25")),
            int(os.getenv("MIN_STOCKS", "8")),
        )
        pairs = [(c, c) for c in codes]
        print(f"[backtest] auto_screen 选出 {len(pairs)} 只，开始回测")

    rows = []
    for code, name in pairs:
        try:
            r = backtest_one(code, name, args)
            if r:
                rows.append(r)
                print(
                    f"  {r['code']} {r['name']} | 收益{r['total_return_pct']}% "
                    f"交易{r['trades']}笔 胜率{r['win_rate']}% "
                    f"最大回撤{r['max_drawdown_pct']}%"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[backtest] {code} 回测失败: {exc}")

    if not rows:
        print("[backtest] 无可用回测结果")
        return

    avg_ret = sum(r["total_return_pct"] for r in rows) / len(rows)
    win_stocks = sum(1 for r in rows if r["total_return_pct"] > 0)
    total_trades = sum(r["trades"] for r in rows)
    won_trades = sum(r["won"] for r in rows)
    avg_dd = sum(r["max_drawdown_pct"] for r in rows) / len(rows)
    print("\n========== 回测汇总（策略=TrendHold，区间近 %d 日）==========" % args.days)
    print(f"  回测股票数      : {len(rows)}")
    print(f"  盈利股占比      : {win_stocks}/{len(rows)} ({round(win_stocks/len(rows)*100,1)}%)")
    print(f"  平均区间收益    : {round(avg_ret,2)}%")
    print(f"  平均最大回撤    : {round(avg_dd,2)}%")
    print(f"  总交易笔数      : {total_trades}")
    if total_trades:
        print(f"  整体胜率(笔)    : {round(won_trades/total_trades*100,1)}%")
    # 同期沪深300大致基准提示（非精确）
    print("  提示：对比同区间沪深300涨跌幅，才能判断策略是否跑赢大盘。")


if __name__ == "__main__":
    main()
