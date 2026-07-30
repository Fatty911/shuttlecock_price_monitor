# 仓库交付规则

- 只修改用户明确授权的文件；`site/`、`out/`、`state/`、`raw/`、浏览器 profile、缓存和响应正文均为运行产物，不得提交。
- 任何生产修改严格先写 RED、真实运行确认预期失败，再做最小 GREEN；完整命令和输出必须可追溯。
- 修改前方案评审首批恰好两个不同模型家族，规则为 `N=2`、`M=2`，两票均 PASS 才实施。信任根和最终完整 staged diff 分别重新执行同样的 2/2 只读评审。
- AGENTS、hook、delivery gate、workflow、阈值、任务集合、依赖和 schema 属于信任根；自动修复不得修改。
- 第三方 GitHub Action 只允许审计白名单中的维护项目，并按用户偏好使用 `@main`/`@master`；GitHub 官方 `actions/*` 使用受支持主版本 tag。
- 禁止 fixture、搜索卡、占位价、旧价回填成为 live；验证码、403、TLS、授权或上游条款问题只能标记 blocked 并停止产品完成声明。
- 保留现有 git author；禁止修改 `user.name`/`user.email`。只允许非 force `git push origin HEAD:main`，禁止 `--force`、`--no-verify` 和绕过评审/测试。
- 提交前运行全套 pytest、compileall、YAML/schema/workflow/安全/XSS/敏感扫描与 `git diff --check`。提交后 hook 必须核对远端 SHA；CI、live workflow、TLS Pages manifest 和产品门未全部一致前不得宣称完成。
- 两位最终审查者必须审查同一个 staged diff SHA-256。提交信息必须带 `Review-Model-Family-1/2`、两个 `Review-Result-1/2: PASS` 和匹配的 `Reviewed-Diff-SHA256` trailers；repo 内 gate 会在 push 前验证家族不同、2/2 PASS、diff 摘要和授权路径。
