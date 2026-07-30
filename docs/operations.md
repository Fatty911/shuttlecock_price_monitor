# 运维手册

## 离线与授权 live

离线测试运行 `python -m pytest -q` 和 `python -m compileall -q shuttle_monitor scripts custom_scripts`。fixture 只能验证解析契约；`python -m shuttle_monitor.monitor --output` 生成 `mode=fixture`，结构门与产品门预期失败。

授权 live 前确认公开抓取、代理订阅和平台条款允许当前速率；不登录、不绕验证码。运行 live 后依次执行 `--structure-gate`、Pages 部署、`scripts/post_deploy_verify.py` 和 `--quality-gate`。

## State 恢复

工作流从 main 的历史 run 中选择非当前 run 的最新 `state-*` artifact。恢复器要求 schema 4、branch/repo/config hash 一致，并限制 ZIP 路径、成员数、单文件和总大小。跨主机重定向会删除 Authorization。无合法 artifact 时从空 state 开始，不使用 Pages 旧价补齐。

恢复失败时保留原目录并记录“未找到语义有效 state”；不要手工解压不可信 artifact。需要回滚时，重新运行上一个已知良好 source SHA；历史只能作为 baseline，不得回填当前价格。

## 证书与 Pages

使用正常证书验证访问公开 `manifest.json`，不得以 `curl -k` 作为验收。SAN/CNAME/DNS 异常时停止 Release 和完成声明，修复域名配置后重跑完整 live 批次与部署后核验。

页面应明确区分加载中、结构阻断、live 阻断、确实无数据和仅有历史。不得加入示例价或 fixture fallback。

## 告警与 Release

结构/部署后/产品门失败时，workflow 以 audit fingerprint 创建或更新同一 `monitor-blocked:<fingerprint>` issue。恢复后自动留言并关闭打开的 monitor-blocked issue。验证码、403、TLS、网络或授权问题只告警，不触发自动改码。

羽毛球每天只归档首个全门通过的公开 Release。Release 只含 manifest、audit 和公开 JSON；原始响应、Cookie、订阅、节点名和浏览器数据只允许留在受控、限期 artifact 中，且敏感字段必须先删除。

## 交付与回滚

变更遵守 `AGENTS.md` 的两次独立 2/2 评审和 RED→GREEN 证据。交付前执行全套测试、安全/XSS/敏感扫描与 `git diff --check`。只允许非 force `HEAD:main`；远端前移时先整合 main，重新测试并重审完整 staged diff。
