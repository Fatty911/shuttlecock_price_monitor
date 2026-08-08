# 羽毛球真实价格监控

本仓每轮固定监控 31 个型号 × 淘宝/天猫、京东、拼多多，共 93 个稳定任务。`products.yaml` 的 `config_revision`、目标速度和显式退休列表共同约束任务集合；速度进入任务 ID 和搜索条件，不能被静默忽略。

## 真实性边界

搜索页只负责发现候选。一个任务只有同时满足下列条件才是 `success`：

- 标题、发现价和商品链接来自同一卡片；
- 链接落在平台官方详情域，且原生商品 ID 在发现页和详情页一致；
- 详情页再次出现匹配型号，并且只有一个无歧义的正价格；
- 批次是带 GitHub run、attempt、40 位 source SHA 和真实网络证据的 `mode=live`。

配件、赠品、分期价、跨卡价格、验证码、登录页、风控页、多价格详情和跨域重定向均不得成为价格。不会破解验证码，不使用 fixture、占位价或旧价回填当前轮。

结果语义统一为 `success`、`blocked`、`rejected`、`error`、`out_of_stock`。非成功状态不允许携带价格或商品 URL。

## 调度与代理

任务按配置优先级执行；每个平台按批次轮换至少三个型号做 canary。请求有全局/每域并发限制、可取消 deadline、重试预算和平台熔断。恢复的 checkpoint 只用于同批调度与历史，不会把上一轮完成状态冒充本轮结果。平台 canary 全部被阻断且无可用代理时，该平台任务直接跳过（快速 blocked，不再逐任务请求或开浏览器），状态守恒不变。

代理是可选能力。Mihomo 固定为校验过 SHA-256 的版本，只从控制面识别叶子节点；Selector、URLTest、Fallback、LoadBalance 等组不会被当作节点。订阅地址、节点名、Cookie、授权头和浏览器 profile 不进入日志、artifact、Pages 或 issue。爬虫出口只允许机场订阅节点，禁止自建 VPS 节点作为落地；机场节点全部不可用时诚实禁用代理并输出 blocked，不切换自建节点、不伪装代理可用。

## v4 输出与两层门

Pages 保留兼容文件名，并新增批次契约：

```text
site/data/status.json          精确 93 条本轮状态
site/data/prices.json          仅本轮详情复核 success
site/data/price_history.json   180 天追加事件，不回填当前价
site/data/summary.json         结果守恒与分平台摘要
site/data/live-evidence.json   有界、脱敏诊断
site/data/batch.json           schema v4 完整 envelope
site/manifest.json             batch/SHA/config 与文件摘要
site/audit.json                结构门、产品门、fingerprint
```

部署前结构门要求 v4、精确 93 个唯一任务、状态守恒、SHA/config/file hash 一致，且非成功记录无价格。结构有效时，即使上游 blocked，也允许状态页展示真实失败。

部署后必须通过正常 TLS 拉取公开 manifest 并逐文件核验。产品门还要求淘宝、京东、拼多多各至少一个本轮真实同卡、详情复核、官方 URL 的商品价。任一平台为零、Pages TLS 异常或公开 manifest 不一致时，workflow 失败并告警，禁止宣称完成或生成合格 Release。

## 本地验证

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
playwright install chromium
python -m pytest -q
python -m compileall -q shuttle_monitor scripts custom_scripts
python -m shuttle_monitor.monitor --output
```

不带 `--live` 的输出固定为 `mode=fixture`，只生成 93 条 `offline_smoke_no_network` 状态；结构门和产品门都必须失败。授权 live 运行使用：

```bash
python -m shuttle_monitor.monitor --live --output
python -m shuttle_monitor.monitor --structure-gate
python -m shuttle_monitor.monitor --quality-gate
```

详细字段见 `docs/schema.md`，恢复、证书、告警、回滚和 Release 操作见 `docs/operations.md`。
