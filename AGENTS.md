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


## Git 提交作者身份规则（Fatty911 全局要求，2026-08-04）

本仓库所有 Git 提交必须遵守以下作者命名规则：

1. **全局兜底身份**：`Fatty911 <xuerui911@gmail.com>`。禁止使用 `bot@users.noreply.github.com` 邮箱（该邮箱关联 GitHub 用户名 `bot`，网页端会显示纯 `bot`）。
2. **Agent 工具显式提交**：使用动态格式 `<实际工具名>-<实际模型>`。工具名 = 实际执行提交的 Agent 工具（如 hermes-agent / codex / opencode / openclaw / mimocode / qoder）。模型名 = 本次实际处理会话的模型 ID 的小写紧凑写法（如 GLM-5.2 → `glm5.2`、GPT-5.6-Sol → `gpt5.6sol`、Kimi-K3 → `kimi-k3`、DeepSeek-V4-Flash → `deepseek-v4-flash`）。示例：`opencode-kimi-k3`、`hermes-agent-glm5.2`、`codex-gpt5.5`。
3. 禁止纯 `bot` 名称或系统 bot 身份冒充源码/文档提交；`github-actions[bot]` 仅限数据/进度自动提交。
4. 邮箱一律使用 `xuerui911@gmail.com`。
