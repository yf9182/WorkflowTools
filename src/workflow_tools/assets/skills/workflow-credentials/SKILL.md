---
name: workflow-credentials
description: 初始化、导入、轮换和检查 WorkflowTools 的本地或 Cloudflare R2 加密凭据；用于门禁报告缺文件、权限或凭据失效时。
---

# 集中凭据与门禁

项目能力由 `workflow_doctor` 返回。指定需要的 capability，使用 `remote=true` 检查真实服务权限；本地 ready 不能当作远端已验证。只处理相关能力的 blocker，继续不依赖它的工作。

凭据取用在工具内部完成。不要读取秘密文件并贴入提示、调用参数或回复；不提供返回秘密内容的工具。日常使用不运行浏览器、`wrangler login`、Google 人工 OAuth 或 Apple ID 登录。

首次接入、轮换和机器迁移读 [接入与恢复](references/setup.md)。管理操作使用项目根目录的 `./.workflow-tools/run credentials ...`；授权根目录默认 `~/.config/workflow-tools`，可用 `WORKFLOW_TOOLS_HOME` 或安装器的 `--home` 指定。配置只存凭据引用。

Apple 重签名构建若缺少某个已登记 Bundle ID 的 App Store 描述文件，不要要求用户手工下载。先确认项目已有有效 Distribution `.p12` 和 ASC team API `.p8`，再按 `ios-testflight` skill 的流程调用 `ios_profiles_prepare`，由工具从 ASC 创建/复用、下载、加密并登记 profile。个人 API key 或缺少 Provisioning 权限时说明需要团队 API key 的 Admin/Provisioning 权限；不能向 Agent 暴露 `.p8` 明文。

任务已有授权时，不在构建、取数、上传或部署的阶段之间重复询问。缺失、撤销或到期时，集中说明要补的文件和权限；不要扩大权限或改宿主审批配置。补齐后使用 `workflow_job_resume`，不手改意图记录。
