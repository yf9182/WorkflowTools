---
name: ios-testflight
description: 在安装 WorkflowTools 的 macOS 项目中构建手动分发签名 IPA、上传 TestFlight 并查询处理状态；不执行商店提审、发布或邀请测试者。
---

# iOS 构建与 TestFlight

使用项目绑定的 MCP 工具；CLI 对应关系见 [调用与签名](references/signing.md)。

1. 按请求调用 `workflow_doctor(capability="ios_build")` 或 `workflow_doctor(capability="ios_upload_testflight", remote=true)`。只上传已有制品时不要求新的 `.p12`，构建才需要证书、密码及各 target 的 App Store 描述文件。
2. 请求上传时，用 `ios_testflight_status` 核对目标版本/构建号。保留用户指定版本；构建号冲突时选取该版本未使用的正整数，不重复使用另一制品的编号。
3. 调用 `ios_build(version, build_number)`；轮询 `workflow_job_status(job_id)` 到完成，再使用结果的 `artifact_id` 调用 `ios_upload_testflight`。只要求本项实际依赖，不引入 GA4、商店文案、版本任务单、独立评审或真机验收门禁。
4. 持续查询上传任务和 `ios_testflight_status`。分别说明上传成功、Apple 处理中、处理完成；没有测试组可用证据时，不声称用户已经可以测试。

长任务排队、运行或处理中时继续跟进。未知上传结果只走 `workflow_job_resume` 的回读路径，不能直接 altool、删除意图、换构建号重复上传或用文件存在冒充签名通过。工具只修改临时工程副本，原工程保持现状。

交付版本/构建号、IPA 摘要、任务编号、真实上传及处理状态、日志/产物路径和未验证项。构建成功不能替代真实业务验收。
