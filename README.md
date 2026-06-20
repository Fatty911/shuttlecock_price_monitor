# 羽毛球最低到手价监控

用于监控淘宝/天猫、京东、拼多多上常用羽毛球型号的最低到手价，生成 GitHub Pages 静态页，域名配置为 `shuttlecocks.jiucai.eu.org`。

## 监控目标

- 每个电商渠道、每个羽毛球型号只保留一个最低到手价商品链接。
- 同时考虑单品价、跨店满减、店铺券/平台券、多商品购物车凑单后分摊到每筒的有效价格。
- 展示电商渠道、满减活动或领券渠道、羽毛球型号、球速、到手价、库存状态和置信度。
- 默认重点型号覆盖：尤尼克斯 AS05/AS20/AS30，亚狮龙 Classic/Supreme/Ultimate/1号/2号，李宁 G700/G800/C90，胜利大师3/大师4，澳加林 AC50，华美 GT900，以及骄点、翎美、文杰、航空/航宇等国产定位相近型号。

## 本地运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m shuttle_monitor.monitor --output
```

如需尝试真实抓取电商搜索页：

```bash
playwright install chromium
python -m shuttle_monitor.monitor --live --output
```

> 电商平台风控和优惠券定向较强，`--live` 结果只能作为筛选线索；下单前仍需用自己的账号确认券、支付优惠、地区库存和实际球速。

## 输出

- `site/index.html`：GitHub Pages 页面。
- `site/CNAME`：自定义域名 `shuttlecocks.jiucai.eu.org`。
- `site/data/results.json`：机器可读监控结果。

## GitHub Pages

工作流会按计划运行并使用 GitHub Pages 官方 Actions 部署静态文件。

## 代理与风控

工作流会读取仓库 Secret `PROXY_SUBSCRIPTIONS`，复用汽车/手机仓库的 mihomo 运行时：

- 支持普通订阅 URL、JSON 数组，或 `{"subscriptions": [...], "exclude_keywords": [...]}`。
- 自动下载 mihomo、解析订阅、生成 Clash 配置并暴露 `127.0.0.1:7890/7891`。
- 监控程序每次请求前会调用 mihomo 控制端口随机切换 `GLOBAL` 节点，尽量降低淘宝、京东、拼多多、什么值得买、中羽在线等页面的风控命中率。
- 如果没有配置 Secret 或订阅不可用，工作流会退化为直连并继续产出页面，避免整站中断。

## 爆料源

除电商搜索页外，`products.yaml` 已加入以下线索源：

- 什么值得买羽毛球搜索页。
- 中羽在线论坛装备/商家优惠板块。
- 企业微信“中羽羽球线报”：通过 `WEWORK_ZHONGYU_WIRE_JSON` 环境变量注入已收集的群消息 JSON 或逐行文本。

企业微信消息建议由外部机器人/转发脚本写入 GitHub Actions Secret 或定时 artifact，再由本项目统一解析价格和关键词。
