---
name: cloudflare-deploy
description: 使用 WorkflowTools 和项目 Wrangler 配置部署 Cloudflare Workers 后端、Workers 静态资源或 Pages 前端，查询部署及健康状态；不执行数据库迁移、计费升级或交互式登录。
---

# Cloudflare 部署

从项目配置选取 target 和 environment，多目标/环境时不猜测生产目标。Cloudflare Account ID 默认读取本机 WorkflowTools 连接配置中的 `cloudflare.account_id`；项目显式填写时优先使用项目值。用 `workflow_doctor(capability="cloudflare_deploy", target, environment)` 检查本项依赖；无需 Apple 或 Google 凭据。

缺 Wrangler 时可在已授权的工具准备范围内运行 `./.workflow-tools/run setup --wrangler`。配置格式和权限见 [部署配置](references/deploy.md)。鉴权来自加密凭据引用，不执行 `wrangler login`，不把 token 写进 `.env` 或源码。

用户已授权具体目标部署时，调用 `cloudflare_deploy` 后跟进 `workflow_job_status`；用 `cloudflare_status` 回读部署标识、版本或 URL，并核对 healthcheck。健康检查未配置时明确未验证，不能以命令退出 0 代替服务可用。

未知结果、超时或中断时，只调用 `workflow_job_resume` 回读已保存意图。不要通过直接 Wrangler、删除任务状态或更换幂等键重发。同一配置记录的健康检查失败可以恢复重验，不重新部署。

首版只部署指定代码目标，不调用数据库迁移或套餐升级；绑定的存储资源需要预先存在。缺相关权限或服务需首次开通时，集中列出所需事项，不改成全局万能 token。

交付目标账户/环境、部署标识、访问 URL（有时）、健康结果、证据路径和未验证项。
