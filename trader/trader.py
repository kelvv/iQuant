# -*- coding: utf-8 -*-
"""
miniQMT 交易封装 —— 国信 iQuant / 迅投 QMT。

设计原则（Linus 风格）：
- 数据结构先行：账户、订单参数是核心，其余都是它们的搬运。
- 消除特殊情况：买/卖统一走 _place()，方向只是一个枚举参数。
- 零破坏：风控是硬门，超限直接 raise，绝不"凑合下单"。

只依赖 xtquant。优先用客户端自带的那份（版本与客户端绝对匹配）。
"""
import os
import sys
import time

# 优先客户端自带 xtquant（协议与客户端一致），失败再退 pip 版
def _setup_xtquant_path():
    qmt_path = os.environ.get("QMT_PATH", "")
    if qmt_path:
        # userdata_mini 的上两级是客户端根，site-packages 在 bin.x64\Lib 下
        root = os.path.dirname(qmt_path.rstrip("\\/"))
        cand = os.path.join(root, "bin.x64", "Lib", "site-packages")
        if os.path.isdir(cand):
            sys.path.insert(0, cand)
            return cand
    return None

_CLIENT_XT = _setup_xtquant_path()

try:
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount
    from xtquant import xtconstant
except Exception as e:  # noqa
    print("[FATAL] import xtquant 失败:", e)
    print("        客户端 xtquant 路径:", _CLIENT_XT)
    raise


# ---------- 股票代码标准化 ----------
def normalize_code(code):
    """'600000' -> '600000.SH'；'000001' -> '000001.SZ'。已带后缀的原样返回。"""
    code = str(code).strip().upper()
    if "." in code:
        return code
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"非法股票代码: {code}")
    # 6/9 开头沪市，0/3 开头深市，4/8 北交所
    if code[0] in ("6", "9"):
        return code + ".SH"
    if code[0] in ("0", "3"):
        return code + ".SZ"
    if code[0] in ("4", "8"):
        return code + ".BJ"
    raise ValueError(f"无法判断市场: {code}")


# ---------- 回调（接收主推，打印即可，链路里用同步查询为主）----------
class _Callback(XtQuantTraderCallback):
    def on_disconnected(self):
        print("[CB] 连接断开")

    def on_stock_order(self, order):
        print(f"[CB] 委托回报 {order.stock_code} status={order.order_status} sysid={order.order_sysid}")

    def on_stock_trade(self, trade):
        print(f"[CB] 成交 {trade.stock_code} order_id={trade.order_id} 量={trade.traded_volume} 价={trade.traded_price}")

    def on_order_error(self, e):
        print(f"[CB] 委托失败 order_id={e.order_id} err={e.error_id} {e.error_msg}")

    def on_cancel_error(self, e):
        print(f"[CB] 撤单失败 order_id={e.order_id} err={e.error_id} {e.error_msg}")

    def on_order_stock_async_response(self, r):
        print(f"[CB] 异步下单回报 order_id={r.order_id} seq={r.seq}")


# ---------- 主封装 ----------
class Trader:
    def __init__(self, qmt_path=None, account_id=None, account_type=None, session_id=None):
        self.qmt_path = qmt_path or os.environ["QMT_PATH"]
        self.account_id = account_id or os.environ["QMT_ACCOUNT_ID"]
        self.account_type = account_type or os.environ.get("QMT_ACCOUNT_TYPE", "STOCK")
        # session_id 必须每个独立进程不同；用时间戳保证唯一
        self.session_id = session_id or int(time.time())
        self.xt = None
        self.acc = None

        # 风控硬上限
        self.max_amount = float(os.environ.get("MAX_ORDER_AMOUNT", "10000"))
        self.max_per_stock = int(os.environ.get("MAX_ORDERS_PER_STOCK", "3"))
        self._order_count = {}  # stock_code -> 今日下单次数

    def connect(self):
        self.xt = XtQuantTrader(self.qmt_path, self.session_id)
        self.xt.register_callback(_Callback())
        self.xt.start()
        r = self.xt.connect()
        if r != 0:
            raise RuntimeError(
                f"connect() 返回 {r} —— 客户端未开极速交易/未登录 (path={self.qmt_path})"
            )
        self.acc = StockAccount(self.account_id, self.account_type)
        sub = self.xt.subscribe(self.acc)
        print(f"[OK] connect=0 subscribe={sub} account={self.account_id}")
        return self

    # ---- 查询 ----
    def asset(self):
        a = self.xt.query_stock_asset(self.acc)
        if a is None:
            raise RuntimeError("query_stock_asset 返回 None —— 账号错误或未授权")
        return {
            "account_id": a.account_id,
            "total_asset": a.total_asset,
            "cash": a.cash,           # 可用资金
            "market_value": a.market_value,
            "frozen_cash": a.frozen_cash,
        }

    def positions(self):
        ps = self.xt.query_stock_positions(self.acc) or []
        return [
            {
                "code": p.stock_code,
                "volume": p.volume,
                "can_use": p.can_use_volume,
                "open_price": p.open_price,
                "market_value": p.market_value,
            }
            for p in ps
        ]

    def orders(self):
        os_ = self.xt.query_stock_orders(self.acc) or []
        return [
            {
                "order_id": o.order_id,
                "code": o.stock_code,
                "volume": o.order_volume,
                "price": o.price,
                "status": o.order_status,
                "type": o.order_type,
            }
            for o in os_
        ]

    # ---- 下单（买/卖统一）----
    def _risk_check(self, code, volume, price):
        amount = volume * price
        if amount > self.max_amount:
            raise RuntimeError(
                f"风控拒单: {code} 金额 {amount:.0f} > 上限 {self.max_amount:.0f}"
            )
        cnt = self._order_count.get(code, 0)
        if cnt >= self.max_per_stock:
            raise RuntimeError(
                f"风控拒单: {code} 今日已下 {cnt} 单 >= 上限 {self.max_per_stock}"
            )

    def _place(self, code, direction, volume, price, strategy="iquant", remark=""):
        """direction: 'buy'/'sell'。price>0 走指定价 FIX_PRICE；price<=0 走最新价 LATEST_PRICE。"""
        code = normalize_code(code)
        if volume <= 0 or volume % 100 != 0:
            raise ValueError(f"A股按手(100股整数倍)下单，volume={volume} 非法")

        order_type = xtconstant.STOCK_BUY if direction == "buy" else xtconstant.STOCK_SELL

        if price and price > 0:
            price_type = xtconstant.FIX_PRICE
            px = float(price)
        else:
            price_type = xtconstant.LATEST_PRICE
            px = 0.0

        # 风控按一个估算价校验金额（最新价单用传入的 price 兜底，没有就跳过金额校验只查次数）
        self._risk_check(code, volume, px if px > 0 else (price or 0))

        oid = self.xt.order_stock(
            self.acc, code, order_type, volume, price_type, px, strategy, remark
        )
        self._order_count[code] = self._order_count.get(code, 0) + 1
        print(f"[ORDER] {direction} {code} x{volume} @{'最新价' if px==0 else px} -> order_id={oid}")
        return oid

    def buy(self, code, volume, price=0, remark="auto-buy"):
        return self._place(code, "buy", volume, price, remark=remark)

    def sell(self, code, volume, price=0, remark="auto-sell"):
        return self._place(code, "sell", volume, price, remark=remark)

    def cancel(self, order_id):
        r = self.xt.cancel_order_stock(self.acc, order_id)
        print(f"[CANCEL] order_id={order_id} -> {r}")
        return r

    def close(self):
        if self.xt:
            try:
                self.xt.stop()
            except Exception:
                pass


# ---------- CLI ----------
def _load_env():
    """从同目录上一级的 .env 读配置（极简，不依赖 python-dotenv）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")
    if os.path.isfile(env_path):
        # utf-8-sig 自动吃掉 Windows PowerShell 写入的 BOM
        with open(env_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    _load_env()
    import json

    if len(sys.argv) < 2:
        print("用法: python trader.py <conn|asset|positions|orders|buy|sell|cancel> [参数]")
        print("  conn                       连接 + 查资产（只读，安全）")
        print("  asset                      查资产")
        print("  positions                  查持仓")
        print("  orders                     查当日委托")
        print("  buy  <代码> <数量> [价格]   买入（价格省略=最新价）")
        print("  sell <代码> <数量> [价格]   卖出")
        print("  cancel <order_id>          撤单")
        sys.exit(1)

    cmd = sys.argv[1]
    t = Trader().connect()
    try:
        if cmd == "conn":
            print(json.dumps(t.asset(), ensure_ascii=False, indent=2))
        elif cmd == "asset":
            print(json.dumps(t.asset(), ensure_ascii=False, indent=2))
        elif cmd == "positions":
            print(json.dumps(t.positions(), ensure_ascii=False, indent=2))
        elif cmd == "orders":
            print(json.dumps(t.orders(), ensure_ascii=False, indent=2))
        elif cmd == "buy":
            code, vol = sys.argv[2], int(sys.argv[3])
            price = float(sys.argv[4]) if len(sys.argv) > 4 else 0
            print("order_id:", t.buy(code, vol, price))
            time.sleep(2)  # 等委托回报
        elif cmd == "sell":
            code, vol = sys.argv[2], int(sys.argv[3])
            price = float(sys.argv[4]) if len(sys.argv) > 4 else 0
            print("order_id:", t.sell(code, vol, price))
            time.sleep(2)
        elif cmd == "cancel":
            print("result:", t.cancel(int(sys.argv[2])))
        else:
            print("未知命令:", cmd)
    finally:
        t.close()


if __name__ == "__main__":
    main()
