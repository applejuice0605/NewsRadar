# 第一批优化自测证明

日期：2026-10-29。范围：待办 01～06、15～18。06 的云端凭据授权受阻，因此第一批不能标记为全部运行完成。

## 改造前基线与原因

- 基线：`b1aa892`。源码快照在 `.deploy/snapshots/pre-first-batch-20261029/source.tar`，不含密钥、数据库或音频。
- 原分析提示词要求通用舆情、强制反直觉和投资者/品牌策略；实际输入主要是标题、没有来源编号。因此结论可能超出材料，页面也没有程序保证的依据链接。
- 原播客标签把日期和音色放在一起，缺少新闻更新时间与音频生成时间的区分。
- 原 RSS 临时错误不重试；ISO 负时区/小数秒解析存在截断风险；远程存储把检查失败当不存在。这三项为 v6.11.0 的选定修复，不是整体升级。

## 自动化结果

命令（从项目根目录执行）：

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=TrendRadar TrendRadar/.venv/bin/python -m unittest discover -s tests -v
node --test tests/*.mjs
LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=TrendRadar TrendRadar/.venv/bin/python scripts/evaluate_news_quality.py
node --check docs/assets/report-speech.js
node --check workers/preview-auth.mjs
git diff --check
```

- Python：74 项通过，0 失败。原始日志 `.deploy/first-batch-python-tests.log`。
- Node：11 项通过，0 失败。原始日志 `.deploy/first-batch-node-tests.log`。
- 40 条编辑种子样本：周边排除规则 40/40 通过，AI 准确率未测，不以规则通过率替代。
- JS 语法与差异空白检查通过。

覆盖：

| 测试 | 可复核的检查内容 |
|---|---|
| `tests/test_analysis_evidence.py` | 编造来源、混入未知引用、伪造数字摘录、缺少依据不能发布；真实摘录生成程序控制的网页/飞书链接；XSS 转义；旧结构仍可读取但不能绕过新分析门禁 |
| `tests/test_quality_evaluation.py` | 区分规则回归与 AI 准确率，分别计算误收与漏收，拒绝不完整预测集 |
| `tests/test_upstream_reliability.py` | 20 项定向回归：RSS 重试矩阵、负时区与小数秒/新鲜度边界、S3 权限/限流/网络失败不创建空库 |
| `tests/test_podcast_dates.mjs` | 实际执行播放器 JS：三个日期标签分离、UTC→上海、旧音频不冒充今天、历史音频匹配、无效时间未知 |
| `tests/test_cloud_monitor.mjs` | 不依赖 Mac 的 ASSETS/KV 流程；过期/缺失/未来元数据、音频类型与字节；签名、去重、6 小时重复、恢复、发送/KV 失败不假报成功 |
| `tests/test_preview_auth.mjs` | 保留 Basic Auth、无配置拒绝和音频 Range 行为 |

## 一次真实模型验证

执行 `scripts/verify_analysis_live.py`，只读生产原始数据库，选取 2026-10-29 当天 12 条已匹配新闻，单次调用现有模型，不发送飞书、不重做历史报告/播客、不更新原始数据库。

结果：模型返回 8 条分析，8 条通过已知来源编号与逐字摘录校验，0 条因引用失败被拦截。输出区分来源陈述、条件性 AI 判断、证据局限。私有原始产物：`.deploy/first-batch/live-analysis.json`。

局限：这证明模型能遵循新协议，不是筛选准确率或事实真实性证明。来源只有标题时，页面明确未独立核验；AI 解读仍需要持续人工抽检。

## 发布与运行状态

- 正式采集器和正式网站未切换；现有新闻推送群保持原有配置。
- 独立云端监测实现与离线自测完成，KV 已建立；Secret 同步被自动审批拒绝，Cron 留空，尚不能在 Mac 离线时实际告警。需要明确批准专用健康机器人凭据存入 Cloudflare 加密 Secret。
- 已部署内测，正式站点首页 SHA-256 前后相同，所有内测功能开关保持开启。
- 线上 `/analysis-preview.html`：HTTP 200，8 条依据链接；`/assets/report-speech.js`：三个时间标签齐全；`/status`：HTTP 200，无 ISO 时间戳直出。
- 桌面及 390×844 小屏验证历史播客页面：播报日期、新闻更新、音频生成时间各自清楚，小屏文档宽度 390px，无横向溢出；历史 10 月 28 日播客显示云扬男声与上海生成时间，当天没有音频时不会冒用昨天音频。
- 真实模型结果视觉证明：`analysis-evidence-desktop.png`；播放器证明：`podcast-dates-desktop.png`、`podcast-dates-mobile.png`。浏览器验收使用与部署相同的本地静态构建；线上另以已认证 HTTP 验证，未把密码写入页面或截图。
- 独立复核补充 4 项回归：模型自由 Markdown 链接不能绕过来源目录；剔除不支持摘录的附带来源；畸形 section 单条拒绝不拖垮有效结果；RSS 禁用后不能被依据重建重新打开。
- 云端实测发现本地 Node 不暴露的兼容问题：Workers 不支持 `redirect: "error"`，导致读取元数据之前即失败。已改为 `manual` 并由响应状态拒绝重定向，测试显式检查该选项。

## 回退

本轮改动保留在 staging，正式运行目录未替换。若需要回退，应先另存后续改动，再按基线提交选择性恢复对应源码；不要直接覆盖当前目录，更不要覆盖原始数据库或本地密钥。
