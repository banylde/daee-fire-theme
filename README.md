# 火部 · 四王盗火 题材短线看板

大鹅基金火部（题材短线）的研究看板：纯静态页面 + GitHub Actions 每个交易日盘后抓数。
**只做研究展示，不下单，不构成推荐。** 仓库内没有账户、密钥或真实持仓。短线是负和游戏，实盘前请核对最新交易规则。

页面标题「四王盗火」，副标题「火部 · 题材短线」。

## 结构

- `config.json`：四王只数、仓位、梯队门槛、题材词典、人工催化（`manual_catalyst`）
- `scripts/build.py`：抓最近一个 A 股交易日的涨停结构，写 `data/latest.json`、`data/official.json`，并把当次调仓写入 `data/history.json`（仅 Python 标准库）
- `data/history.json`：正式调仓记录。字段是日期、调入、调出、调仓后权重、原因。只收 `official.json` 里已经发生的调仓
- `.github/workflows/update.yml`：周一至周五北京时间 15:40 运行，也可在 Actions 页手动 Run workflow
- `index.html` / `manifest.webmanifest` / `sw.js` / `icons/`：前端与 PWA

本地：`python3 scripts/build.py`。指定日期：`ASOF=2026-11-06 python3 scripts/build.py`。

## 本轮已经做的

- 情绪面板：涨停数、跌停数、炸板率、连板高度、昨日涨停股今日平均收益，以及相对前一交易日的断板、跌停增减
- 题材表：按同花顺涨停原因归类，四条（催化、梯队、容量、天数）逐项给「通过 / 未过 / 待人工确认 / 缺数据」
- 股票池：只展示结构，未核实的条件标「缺数据」，状态为「未正式入池」
- 四王：四条没有全部核实时留空，不凑数
- 仓位口径：固定 4 只、四个题材，单只 7.5%，合计不超过 30%。原策略单只 10%–15%、最多 3 只，与「总仓位 ≤30% 且持有四只」对不上，页面写明改成单只 7.5%。退潮日新开仓压到半仓以下（最多 1 只）

催化要人工写入 `config.json` 的 `manual_catalyst`（题材名 → 要点）才算通过。脚本判定不了的保持「待人工确认」。

## 数据源

| 用途 | 来源 | 说明 |
| --- | --- | --- |
| 涨停 / 跌停 / 炸板 / 昨日涨停 | 东方财富 `push2ex.eastmoney.com` 的 `getTopicZTPool`、`getTopicDTPool`、`getTopicZBPool`、`getYesterdayZTPool` | 免费接口，家数以返回的 `tc` 为准 |
| 涨停原因、一字 / T 字 / 换手 | 同花顺 `data.10jqka.com.cn/dataapi/limit_up/limit_up_pool` | 非官方。题材按原因里第一个词典关键词归类 |
| 交易日 | 东财上证指数日 K | 用来确定数据日和前一交易日 |
| 板块成交额 | 东财板块日 K `secid=90.BKxxxx` | 只说明相对前 5 日中位数放大了没有；中军本轮未核 |
| 重点监控 | `mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json` | 东财整理的在册区间，含基金。已过期摘牌的历史监管可能不在文件里 |
| 交易日备用 | 腾讯财经上证指数日 K，`web.ifzq.gtimg.cn` | 只在东财日 K 失败时用，用来判断最近一个交易日 |
| 涨停池备用 | 同花顺 `data.10jqka.com.cn/dataapi/limit_up/limit_up_pool` | 只在东财涨停池失败或为空时用。连板只认「首板」「N连板」和「N天N板」；天数和板数不同的「N天M板」不算连板，标缺数据 |
| 跌停 / 炸板备用 | 同花顺 `lower_limit_pool`、`open_limit_pool` 的 `total` | 只在东财对应池失败时用，只取家数 |
| 开盘啦题材强度 | `apphq.longhuvip.com` / `apphis.longhuvip.com` | 本环境请求通，但 `list` 为空（`errcode=0`）。缺少 App 设备参数时拿不到强度，**不要把空列表当成「没有题材」** |

数字都来自上述接口。对不上或没抓到的字段写「缺数据」，不补造。备用源只在主源失败时启用，成功日仍用东财涨停池。

## 纸面成交约定

信号在信号日收盘后计算，纸面成交记在下一交易日开盘（T+1 open）。涨停买不进，跌停卖不出。这是记账约定，不是下单。胜率、盈亏比、最大单笔亏损没有成交记录，保持缺数据，不编造。

## 数据过期

数据日落后于最近一个已收盘的 A 股交易日时，`data/latest.json` 写 `"stale": true`，页面顶部显示「数据过期」。东财涨停池和同花顺备用都失败，或交易日历主源和备用都失败时，同样把上一份 `latest.json` 标成 `stale: true` 再写回，不改里面的行情数字，也不新编报价。页面在 `stale` 为真，或 `data_date` 早于 `expected_date` 时展示这面旗。

同花顺与东财的涨停家数偶尔差 1–2 只（口径或延迟）。梯队个数以「东财涨停池 ∩ 同花顺原因归类」为准，情绪里的涨停家数用东财 `tc`。

## 海外 Runner 取不到时

GitHub 托管 Runner 在海外，东财或同花顺可能超时、重置连接。主源失败时按上一节改用备用源。主源和备用都失败时，脚本把上一份数据标成 `stale: true` 并正常结束，让页面显示「数据过期」，**不会改用猜的数字**。页面顶部仍会列出失败项。

替代：

1. 在能访问东财、同花顺的机器上运行 `python3 scripts/build.py`，把生成的 `data/*.json` 提交到 `main`。
2. 页面只读仓库里的 JSON，不在浏览器里直连行情源。
3. 开盘啦继续空返回时，维持「不用开盘啦」；题材仍以同花顺原因为准，词典不够就改 `config.json`。

## GitHub Pages

与「八友御风」「五绝归山」相同，站点文件在仓库根目录，没有构建步骤。

1. 仓库 Settings → Pages → Build and deployment → **Deploy from a branch**
2. Branch 选 `main`，Folder 选 `/ (root)`，保存
3. 站点为 `https://banylde.github.io/daee-fire-theme/`
4. Actions 推送 `data/latest.json` 后刷新即可。手机浏览器可「添加到主屏幕」

## Vercel（可选，与风部相同）

Framework Preset 选 Other，Build Command 和 Output Directory 留空。`vercel.json` 只给 `data/` 和 `sw.js` 加了 no-cache。

## 下一轮

- 每个交易日把题材和涨停落档，用来计算题材是第几天。这和 `data/history.json` 的调仓记录不是同一份。回填不了就继续缺数据，不推定
- 容量里的中军：流通市值约 50–300 亿、收盘在 20 日均线之上
- 硬过滤：近 20 日日均成交额 > 2 亿；30 日内减持、立案、业绩暴雷公告
- 正式入选后的次日观察买点（涨停价或关键支撑，高开超过 8% 不追）和止损位
- 纸面统计仍缺成交记录：胜率、盈亏比、最大单笔亏损不编造。成交约定见上文（信号日收盘后计算，下一交易日开盘价，涨停买不进、跌停卖不出）。调入调出写在 `data/history.json`

## 风险

短线是负和游戏。本看板是个人研究工具，不是推荐，也不连接任何交易账户。
