# 独立云端健康监测启用证明

日期：2026-10-29，上海时区。用户已明确批准将现有专用健康机器人的 webhook URL 和签名密钥保存到 Cloudflare 内测 Worker 的加密 Secret，并启用每 30 分钟的检查。

## 配置与隔离

- 部署对象：`newsradar-preview`，版本 `36a86940-a536-4ffb-a384-c0a7ad7fe92f`。
- 每小时整点、半点检查（UTC 与上海分钟相同）。平台首次传播可能延迟；未声称本次手动验证等同已经观察到自动 Cron 事件。
- Secret 名称：`HEALTH_WEBHOOK_URL`、`HEALTH_WEBHOOK_SECRET`。2 项同步成功，未打印值、未写入源码、未纳入 Git。
- 状态空间：`HEALTH_MONITOR_STATE`；检测采集完成时间、采集成功标志、播客所属日期、生成时间与线上音频字节。
- 采集超过 120 分钟视为过期；北京时间 22:00 起要求当天播客，否则允许昨天播客。
- 正常静默；首次异常、新增/恶化问题、持续异常每 6 小时、恢复时发送签名卡片。首次人工启用验证额外发送一张正常卡片；重复启用不重复发送。
- 原有每天 22:00 健康日报保留，正常新闻推送机器人与群配置未改；正式网站及采集器未切换。

## 真实云端验证

通过受既有内测 Basic Auth 保护的 `POST /cloud-health/check` 调用同一 scheduled 处理函数；必须提供专用自定义请求头，拒绝匿名请求和普通跨站表单。此路径不接受任意目标 URL、卡片正文或外部凭据。

北京时间 15:19:59 左右的首轮：

1. 读出 `scheduler_enabled=true`，尚无历史检查状态。
2. Worker 读取部署资源，`status=healthy`，`issues={}`；最近采集完成 15:12:15，距今约 8 分钟。
3. 读取 2026-10-28 播客元数据与 32 个音频字节，符合 22:00 前日期规则。
4. 使用加密 Secret 在云端签名并发送卡片；飞书接口返回成功，Worker 才继续保存状态并返回 `success=true`。这是接口确认接收，不等同证明人已阅读。
5. `/cloud-health.json` 可读到已持久化的检查状态。
6. 再次触发成功且已有健康状态，按去重分支保持静默。

脱敏原始结果：[cloud-monitor-activation-proof.json](cloud-monitor-activation-proof.json)。

## 回归

- `node --test tests/*.mjs`：12 项全部通过，覆盖签名、告警、6 小时重复、恢复、失败不更新状态、鉴权与请求头、首次启用仅发一张卡片。
- Python 全套：74 项全部通过。
- `node --check workers/dashboard-monitor.mjs` 与 `git diff --check`：通过。
- 测试日志：`.deploy/cloud-activation-node-tests.log`、`.deploy/cloud-activation-python-tests.log`；部署日志 `.deploy/cloud-activation-deploy.log`。

## 边界

云端检查独立于 Mac mini，可以检测采集/发布长期停止；当前通过 Cloudflare ASSETS 读取，不覆盖公网 DNS 或 Cloudflare 整个平台故障。KV 并非严格串行锁，极端并发或发送成功后状态写入失败仍可能重复告警，不宣称消息 exactly-once。没有人为破坏线上数据制造故障；异常、恢复及错误分支通过离线回归验证。
