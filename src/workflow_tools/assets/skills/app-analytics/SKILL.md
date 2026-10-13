---
name: app-analytics
description: 使用 WorkflowTools 获取 GA4、ASC 销售和已有 Apple Analytics 报告目录，生成按 App 版本隔离的 JSON/Markdown 分析；不适用于没有配置数据源的任意网页统计。
---

# App 数据分析

按需要调用 `workflow_doctor` 的 `analytics_ga4`、`analytics_asc_sales` 或 `analytics_apple_analytics`，不要为只读分析要求 Xcode 签名环境。用 `remote=true` 重验来源身份和读取权限。

使用 `analytics_fetch(sources, start_date, end_date)`，默认最近 28 天，明确要求刷新时传 refresh=true；轮询 `workflow_job_status`，读取 `dataset_id` 后调用 `analytics_report`。CLI 和口径配置见 [分析口径](references/metrics.md)。来源独立失败，检查 `source_status` 与实际可读日期；部分成功可以分析可用来源。

项目事件和漏斗来自配置，不推测其他 App 的事件含义。报告按 appVersion 分段。activeUsers 不跨日期或事件相加；金额分币种展示，成本未知则净利润未知。

比率仅在配置的事件 partition 被逐日、逐版本验证且达到样本下限时输出，并注明它是事件计数比，不能当作去重用户转化、留存或因果结果。数据受限、版本缺失、样本不足时呈现计数和原因。

Apple Analytics 的首版范围是读取已有 ONGOING 请求和报告目录；指标数据下载仍标未验证，不能把目录数量当作业务指标，也不能隐式创建持久报告请求。

交付日期覆盖、来源、版本分段计数、报告路径、不可用原因。数据不足只限制结论，不阻止其他已授权的无依赖工作。
