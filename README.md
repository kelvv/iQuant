# iQuant — A 股量化自动交易

国信 iQuant（迅投 QMT）miniQMT 自动交易。核心场景：**监控推特 → 推文讨论某只 A 股 → 自动下单**（先模拟盘）。

## 架构

```
推特监控(monitor) → Grok 判定"实质讨论某只 A 股" → 提取股票代码
                                                      ↓
                                          风控(金额/次数上限)
                                                      ↓
                                     trader.py → xtquant.xttrader
                                                      ↓
                                        miniQMT 客户端(极速交易)
                                                      ↓
                                            模拟/实盘账户成交
```

## 运行环境

**必须在 Win11 那台机器上跑**（miniQMT 是本地进程通信，xttrader 连本机客户端）。

- 机器: Win11 台式机 `100.86.132.48`（`ssh win`）
- 客户端: `D:\国信iQuant策略交易平台`，`XtItClient.exe` 必须开着
- Python: `C:\Py311\python.exe`（独立 3.11，xtquant 只支持到 3.11，系统 3.12 跑不了）
- xtquant: 优先用客户端自带 `bin.x64\Lib\site-packages\xtquant`（版本与客户端匹配）

## 前置条件（关键）

1. 客户端打开，登录**极速交易 / 独立交易**模块（不是普通行情登录）
2. 拿到**资金账号**（极速交易登录后界面显示的那串数字）→ 填 `.env` 的 `QMT_ACCOUNT_ID`
3. miniQMT 模式启用（客户端设置里勾「极速交易」）

没满足第 1、3 条，`connect()` 会返回非 0，下不了单。

## 用法

```bash
# 复制配置
cp .env.example .env   # 填 QMT_ACCOUNT_ID

# 1. 连接测试（只读，安全）
C:\Py311\python.exe trader\trader.py conn

# 2. 查持仓 / 委托
C:\Py311\python.exe trader\trader.py positions
C:\Py311\python.exe trader\trader.py orders

# 3. 下单（模拟盘）—— 买 浦发银行 100 股 最新价
C:\Py311\python.exe trader\trader.py buy 600000 100
# 指定价
C:\Py311\python.exe trader\trader.py buy 600000 100 10.50
# 卖
C:\Py311\python.exe trader\trader.py sell 600000 100
# 撤单
C:\Py311\python.exe trader\trader.py cancel <order_id>
```

## 风控（硬门，超限直接拒）

`.env` 配置：
- `MAX_ORDER_AMOUNT` — 单笔最大金额（元）
- `MAX_ORDERS_PER_STOCK` — 单只票单日最大下单次数

## 模块

| 目录 | 内容 |
|------|------|
| `trader/` | miniQMT 交易封装（连接/查询/买卖/撤单/风控） |
| `monitor/` | 推特监控 + Grok 判定 + 下单触发 |
| `scripts/` | 部署 / 同步脚本 |
| `docs/` | 官方 API 手册（不入 git） |

## 踩坑

- **系统 Python 3.12 import xtquant 必失败** —— `.pyd` 只编译到 cp311。用 `C:\Py311`。
- **session_id 必须每进程唯一** —— trader.py 用时间戳，多脚本并行不冲突。
- **A 股按手** —— 下单数量必须 100 的整数倍。
- **平台内置策略(passorder)模拟运行下单无效** —— 那条路不能试模拟单，所以走 miniQMT/xttrader。
