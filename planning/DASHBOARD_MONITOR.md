# Dashboard monitoring

The macOS user LaunchAgent `com.bruce.newsradar.dashboard` runs
`scripts/run_dashboard_monitor.sh` every 3600 seconds and at 22:00 local time.
The machine uses Asia/Shanghai time. launchd coalesces a calendar event missed
during sleep when the machine wakes; a powered-off machine cannot send alerts.

Each run deploys staging presentation assets with current production reports
and audio to the password-protected preview Worker. It then authenticates to
the deployed health JSON, checks collection freshness (under 120 minutes),
and verifies the latest podcast metadata and audio HTTP response online.
The runtime state is `.deploy/dashboard-health.json`; logs are
`.deploy/dashboard-monitor.log` and `.deploy/dashboard-monitor.err.log`.

After 22:00, one successful Feishu health message is sent per calendar day.
Delivery failures leave the daily marker unset for retry on the next run.
The report includes collection, preview deployment, website freshness,
podcast generation and online availability, catch-up flag, and the latest
Feishu news delivery status. `--notify` sends a manual verification report.
Before 22:00, that verification does not suppress the nightly report.

Preview credentials are read from `.preview-auth.local`.
Health cards use a dedicated group webhook with HMAC-SHA256 signature
verification. Set `NEWSRADAR_HEALTH_WEBHOOK_URL` and
`NEWSRADAR_HEALTH_WEBHOOK_SECRET` in `.health.env.local` (mode 600, Git ignored).
The production Feishu news webhook is not read or changed by the monitor.
No credentials are copied into static assets or logs. Webhooks have no API
idempotency guarantee; the local daily delivery marker prevents routine
duplicates, but a crash after delivery and before saving may duplicate a card.
Podcast expectation: yesterday is valid before 22:00; today's episode is
required after 22:00. News pushes skipped for no matches are not failures.

Root cause of the October 19-23 preview outage: collection and production
publishing were scheduled, but preview deployment was manual only. A Git push
success was incorrectly treated as evidence of preview website freshness.
