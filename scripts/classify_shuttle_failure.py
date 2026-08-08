#!/usr/bin/env python3
"""Classify shuttlecock monitor failures before invoking AI auto-fix.

Deterministic, no LLM.  External blocks (e-commerce anti-bot, proxy
degradation) are reported but never auto-fixed; only site/CI breakage
reaches the Agent repair path.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# External block markers: e-commerce search pages are JS-shell / login-walled
# for anonymous IPs (verified across Azure runner, VPS proxies and home IPs).
EXTERNAL_BLOCK_PATTERNS = [
    r"platform_success_zero",
    r"no_same_card_candidate",
    r"risk_handler",
    r"platform_unreachable",
    r"canary-skip",
    r"browser_circuit_open",
    r"browser:TimeoutError",
    r"proxy_unavailable",
    r"captcha",
    r"验证码",
    r"请登录",
    r"需要登录",
    r"登录页面",
    r"login page",
    r"login required",
    r"signin",
]

# Proxy degradation markers (airport subscription / mihomo / DNS failures).
# 注意：setup 的逐节点 delay test 是例行输出（即使代理启用成功也会打印
# 503），不能作为代理降级信号；只认最终判定类信号。
PROXY_DEGRADED_PATTERNS = [
    r"代理连通性测试失败",
    r"所有代理节点连通性测试失败",
    r"可用节点: 0/",
    r"订阅已获取但没有解析到可用节点",
    r"mihomo 控制端口未就绪",
    r"mihomo 不可用",
    r"无法解析到可用节点",
    r"未配置 PROXY_SUBSCRIPTIONS",
    r"Name or service not known",
    r"Temporary failure in name resolution",
]

# Transient GitHub Actions infrastructure failures: artifact upload/download
# and OIDC attestation network hiccups.  Retry, do not change code.
INFRA_TRANSIENT_PATTERNS = [
    r"Failed to FinalizeArtifact",
    r"Failed to CreateArtifact",
    r"ECONNRESET",
    r"Unable to make request",
    r"Unable to download artifact",
    r"Artifact not found",
    r"Failed to get ID token",
    r"Client network socket disconnected",
    r"secure TLS connection",
    r"Internal Service Error",
    r"502 Bad Gateway",
    r"504 Gateway Timeout",
    r"runner system failure",
    r"Remote host terminated the handshake",
]

# Site breakage: parser / selector errors inside our code.
SITE_BREAKAGE_PATTERNS = [
    r"parse_product_cards",
    r"AttributeError",
    r"KeyError",
    r"TypeError",
    r"IndexError",
    r"ValueError",
    r"Traceback \(most recent call last\)",
    r"shuttle_monitor/monitor\.py.*Error",
    r"MarkupClassification",
    r"ProductCandidate",
]

# CI breakage: tests or compile failures.
CI_BREAKAGE_PATTERNS = [
    r"pytest.*(failed|error)",
    r"FAILED tests/",
    r"ERROR tests/",
    r"ModuleNotFoundError",
    r"ImportError",
    r"SyntaxError",
    r"IndentationError",
    r"compileall.*(error|failed)",
]

# Structural gate failures are expected when upstream is blocked and are not
# code defects; they are reported, not fixed.
STRUCTURAL_GATE_PATTERNS = [
    r"structure_gate",
    r"结构门",
    r"quality_gate",
    r"产品门",
    r"product_gate",
    r"live_price_quality_gate",
    r"violations",
]


MAX_LOG_BYTES = 4 * 1024 * 1024  # 单文件最多读 4MB，避免 GB 级日志 OOM


def read_logs(paths: list[str]) -> str:
    chunks = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.exists() and path.is_file():
            size = path.stat().st_size
            if size > MAX_LOG_BYTES:
                # 只读尾部（失败相关日志通常在末尾）
                with open(path, "rb") as handle:
                    handle.seek(-MAX_LOG_BYTES, 2)
                    chunks.append(handle.read().decode("utf-8", errors="replace"))
            else:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def classify(text: str, conclusion: str) -> tuple[str, str, bool]:
    """Return (classification, reason, should_diagnose)."""
    if conclusion == "success":
        return "expected_success", "workflow 成功且未发现跑偏迹象", False

    if not text.strip():
        return "unknown", "无日志可分析（可能日志下载失败）", True

    # External block takes precedence: e-commerce anti-bot is a platform
    # policy, never auto-fixed (AGENTS.md: only alert, no code change).
    if any(re.search(p, text, re.I) for p in EXTERNAL_BLOCK_PATTERNS):
        return (
            "expected_external_block",
            "电商搜索页对匿名出口风控（JS 壳/登录墙/验证码），属平台策略，只告警不修码",
            False,
        )

    if any(re.search(p, text, re.I) for p in INFRA_TRANSIENT_PATTERNS):
        return (
            "expected_infra_transient",
            "GitHub Actions 基础设施瞬时故障（artifact/OIDC 网络抖动），重试即可，不改代码",
            False,
        )

    if any(re.search(p, text, re.I) for p in PROXY_DEGRADED_PATTERNS):
        return (
            "expected_proxy_degraded",
            "机场订阅/mihomo 代理链路降级或不可用；检查订阅有效性，不改代码",
            False,
        )

    if any(re.search(p, text, re.I) for p in STRUCTURAL_GATE_PATTERNS):
        return (
            "expected_gate_block",
            "结构/质量门如实失败（上游 blocked 时状态页展示真实失败），只告警不修码",
            False,
        )

    if any(re.search(p, text, re.I) for p in CI_BREAKAGE_PATTERNS):
        return (
            "ci_breakage",
            "CI/测试失败（pytest/compileall/导入错误），可自动修复",
            True,
        )

    if any(re.search(p, text, re.I) for p in SITE_BREAKAGE_PATTERNS):
        return (
            "site_breakage",
            "解析器/选择器或代码异常，可自动修复",
            True,
        )

    return "unknown", "未能归类失败原因，保守进入 AI 诊断（只诊断不改码）", True


def build_prompt(workflow_name: str, run_id: str, conclusion: str, text: str) -> str:
    log_excerpt = text[-120000:]
    return f"""你在 GitHub Actions 中作为羽毛球价格监控仓库的故障诊断代理运行。

## 仓库背景
- 仓库：Fatty911/shuttlecock_price_monitor，监控 31 个羽毛球型号 × 淘宝/京东/拼多多（93 任务）
- 链路：shuttle-monitor.yml（live 爬取 + 结构门 + 部署 + 产品门）→ monitor-blocked issue
- 真实性边界：仅详情复核 success 携带价格；验证码/登录/JS 壳一律 blocked；禁止伪造
- 信任根（自动修复不得修改）：AGENTS.md、.githooks/、scripts/codex_delivery_gate.py、
  .github/workflows/、阈值/超时/熔断参数、products.yaml 任务集合、requirements.txt 依赖、
  docs/schema.md
- 可修复范围：shuttle_monitor/monitor.py 的解析/选择器/匹配逻辑（parse_product_cards、
  classify_markup、verify_detail_candidate 等）、tests/ 测试修复
- 外部阻断（电商风控/代理失效/验证码/403/TLS）只告警，禁止尝试绕过或改门禁

## 本次运行
- Workflow: {workflow_name}
- Run ID: {run_id}
- Conclusion: {conclusion}

## 任务
判断失败根因属于哪一类，并给出可执行的修复建议：
1. 站点结构变化（选择器/页面结构失效）→ 给出需要更新的解析函数与定位思路
2. 代理/网络问题（订阅节点、mihomo）→ 说明检查方向，不改代码
3. 风控问题（搜索页 JS 壳/登录墙/验证码）→ 建议重试或调整，禁止绕过
4. 代码缺陷（异常堆栈）→ 给出最小修复思路
5. 临时故障（runner 抖动、超时）→ 建议重试，不改代码

## 输出格式（严格遵守）
第一行：根因分类: <类别编号和名称>
第二行：置信度: <0-1>
第三行起：修复建议（纯文本，不要编造文件内容，不要输出未经验证的代码）

## 日志摘录
```text
{log_excerpt}
```"""


def write_outputs(path: str, classification: str, reason: str, should_diagnose: bool) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"classification={classification}\n")
        handle.write(f"reason={reason}\n")
        handle.write(f"should_diagnose={'true' if should_diagnose else 'false'}\n")

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="*")
    parser.add_argument("--prompt-output", required=True)
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    args = parser.parse_args()

    workflow_name = os.environ.get("WORKFLOW_NAME", "")
    conclusion = os.environ.get("WORKFLOW_CONCLUSION", "")
    run_id = os.environ.get("WORKFLOW_RUN_ID", "")
    text = read_logs(args.logs)

    classification, reason, should_diagnose = classify(text, conclusion)
    print(f"classification={classification}")
    print(f"should_diagnose={'true' if should_diagnose else 'false'}")
    print(f"reason={reason}")
    write_outputs(args.github_output, classification, reason, should_diagnose)

    if should_diagnose:
        Path(args.prompt_output).write_text(
            build_prompt(workflow_name, run_id, conclusion, text), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
