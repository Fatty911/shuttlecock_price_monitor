# 数据契约（schema v4）

Pages 只把 `schema_version: 4` 且 `mode: live` 的批次当作当前价格。旧 schema 只能显示为“历史待迁移”，不能变成新鲜价格。

## Envelope

`site/data/batch.json` 包含：

- `batch_id`: `shuttlecock_price_monitor:<run_id>:<run_attempt>`
- `run_id`、`run_attempt`、`source_sha`、`config_sha256`
- `started_at`、`finished_at`、`mode`、`baseline_batch_id`
- `expected_tasks: 93`
- `statuses`、`prices`、守恒 `summary`
- `evidence_sha256`、`audit_status`

`source_sha` 必须为 40 位小写十六进制，配置和证据摘要必须为 64 位 SHA-256。`success + blocked + rejected + error + out_of_stock` 必须精确等于 93。

## Status

每条状态必须有 `task_id`、`outcome`、`attempts`、`started_at`、`finished_at`、`source_url`、`final_url`、`rejection_reason`、`evidence_hash`、`parser_version`。任务 ID 包含平台、品牌型号和目标速度。

`success` 还必须有平台、价格、官方 `product_url`、`native_product_id`、`detail_verified: true` 和 `mode: live`。非成功状态的价格和商品 URL 必须为 `null`。

常见拒绝原因包括 `challenge`、`no_card`、`title_mismatch`、`accessory`、`price_ambiguous`、`detail_unverified`、`url_domain_mismatch` 和 `sold_out`。

## 历史与 manifest

历史是带 `event_id`、`observed_at` 的追加事件，稳定键去重，保留 180 天。旧记录不计入当前成功数。

`manifest.json` 绑定 schema、batch、run、attempt、source/config SHA、mode、audit 状态和公开文件的 SHA-256/大小。部署后核验必须通过正常 TLS 重新获取 manifest 和每个列出的文件。
