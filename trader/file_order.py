# -*- coding: utf-8 -*-
"""
文件单下单 —— 国信 iQuant 官方对外下单方式（操作手册 5.3.2.12）。

机制：客户端「文件单」策略监听一个目标 txt 路径，外部程序每次往该路径写一行
指令，平台读取后自动下单。这是国信官方文档明确支持的外部程序下单接口，
不依赖 miniQMT/xttrader（那条需要管理端单独开 miniQMT 交易接口权限）。

txt 格式（文档原文，英文逗号分隔，一行一单）：
    下单类型,通道号,报价方式,下单代码,下单总量
官方示例：
    23,1,8.9,600004,200
    └─ 23=买入  1=通道号  8.9=报价(指定价8.9元)  600004=代码  200=数量

前提（文档 3134 行）：管理端须开启「允许使用函数交易下单」权限，且在客户端
「文件单」窗口里新建并【启动】一条策略，绑定账号组 + 指定本脚本要写的目标路径。
"""
import os
import time

# ---- 下单类型（与 xtconstant 对齐，文档示例 23=买入）----
ORDER_BUY = 23    # 股票买入（xtconstant.STOCK_BUY）
ORDER_SELL = 24   # 股票卖出（xtconstant.STOCK_SELL）

# ---- 报价方式 ----
# 文档示例直接写价格数字(8.9)=指定价。"最新价/市价"的具体编码文档图里没列全，
# 暂以「指定价数字」为主路径；最新价用客户端右侧面板默认报价方式兜底（写 0 占位）。
# 待客户端实测确认枚举后在此补全。
PRICE_LATEST = 0  # 占位：交由文件单策略面板的"报价方式=最新价"决定


def normalize_code(code):
    """'600000.SH' -> '600000'；文件单只要 6 位裸代码。"""
    return str(code).strip().upper().split(".")[0]


def build_line(direction, code, volume, price=PRICE_LATEST, channel=1, remark=""):
    """拼一行文件单指令。direction: 'buy'/'sell'。"""
    otype = ORDER_BUY if direction == "buy" else ORDER_SELL
    code = normalize_code(code)
    if volume <= 0 or volume % 100 != 0:
        raise ValueError(f"A股按手(100股整数倍)，volume={volume} 非法")
    # 报价：>0 写指定价数字；<=0 写 0（由面板报价方式决定）
    px = price if price and price > 0 else 0
    # 文档格式：下单类型,通道号,报价方式,下单代码,下单总量[,投资备注]
    fields = [str(otype), str(channel), str(px), code, str(volume)]
    if remark:
        fields.append(remark)
    return ",".join(fields)


def place(file_path, direction, code, volume, price=PRICE_LATEST, channel=1, remark=""):
    """写一单到文件单监听路径。平台读到即下单。

    文档「中间文件导入后启动文件单策略，需要再次将中间文件导入到目标路径下」——
    即每次写一个新文件触发一单。这里用「写临时文件 → 原子 rename 到目标路径」
    保证平台不会读到写一半的内容。
    """
    line = build_line(direction, code, volume, price, channel, remark)
    tmp = file_path + ".tmp"
    with open(tmp, "w", encoding="gbk") as f:  # 国信客户端默认 GBK，避免中文备注乱码
        f.write(line + "\n")
    os.replace(tmp, file_path)  # 原子替换，防半行
    print(f"[FILE-ORDER] -> {file_path}\n              {line}")
    return line


# ---- CLI ----
def main():
    import sys
    if len(sys.argv) < 4:
        print("用法: python file_order.py <buy|sell> <代码> <数量> [指定价] [--path 监听路径]")
        print("  例: python file_order.py buy 600004 200")
        print("      python file_order.py buy 600004 200 8.9")
        print("  监听路径默认读 .env 的 FILE_ORDER_PATH，或用 --path 覆盖")
        sys.exit(1)

    # 读 .env 拿默认监听路径
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")
    file_path = None
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8-sig") as f:
            for line in f:
                if line.startswith("FILE_ORDER_PATH="):
                    file_path = line.split("=", 1)[1].strip()

    direction, code, volume = sys.argv[1], sys.argv[2], int(sys.argv[3])
    price = PRICE_LATEST
    args = sys.argv[4:]
    if args and args[0] not in ("--path",):
        price = float(args[0]); args = args[1:]
    if "--path" in args:
        file_path = args[args.index("--path") + 1]

    if not file_path:
        print("[FATAL] 未指定文件单监听路径（.env 的 FILE_ORDER_PATH 或 --path）")
        sys.exit(1)

    place(file_path, direction, code, volume, price)


if __name__ == "__main__":
    main()
