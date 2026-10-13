# 数据源与口径

analytics.sources 可选 ga4、asc_sales、apple_analytics。GA4 使用 google.property_id、google.service_account_ref；只读账号需要属性查看权限，Admin/Data API 已启用，iOS stream 的 Bundle ID 与 ios.bundle_id 匹配。ASC 销售需要 ios.apple.vendor_number；product_ids 仅用于过滤属于目标 App 的既有订阅产品。

默认 window_days=28、sample_floor=30。funnels 示例：

```json
{"name":"识别完成事件比","numerator":"recognize_success","denominator":"recognize_started","partition_complements":["recognize_failed"]}
```

此例只应放在使用这些事件含义的项目里。工具逐日、逐版本检查分子与 complements 的和是否等于分母；不能混入可能重叠的第二类失败。未配置证明、空样本、数据受限、版本未知时不给数值。即使 partition 通过也只称事件计数比，不作去重用户转化或因果结论。

```sh
./.workflow-tools/run analytics fetch --sources ga4 asc_sales --start-date 2026-09-01 --end-date 2026-09-28
./.workflow-tools/run job status <job_id>
./.workflow-tools/run analytics report --dataset-id <dataset_id>
```

部分来源不可用时保留各来源 status、缺日期和原因；同一参数重复采集返回已有任务。需要在同一日期范围重新获取时用 analytics_fetch(refresh=true) 或 CLI --refresh；完成任务不会被 resume 重采集。

Apple Analytics 当前只采已有请求/报告目录，metric_rows=unverified；不隐式创建 ONGOING，也不把报告目录当用户或销售数据。留存/cohort、用户去重分析需要额外真实查询证据，本工具不通过 daily activeUsers 相加推导。
