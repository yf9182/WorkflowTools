---
name: cloudflare-deploy
description: 使用 WorkflowTools 和项目 Wrangler 配置部署 Cloudflare Workers 后端、Workers 静态资源或 Pages 前端，查询部署及健康状态；不执行数据库迁移、计费升级或交互式登录。
---

# Cloudflare 部署

从项目配置选取 target 和 environment，多目标/环境时不猜测生产目标。Cloudflare Account ID 默认读取本机 WorkflowTools 连接配置中的 `cloudflare.account_id`；项目显式填写时优先使用项目值。用 `workflow_doctor(capability="cloudflare_deploy", target, environment)` 检查本项依赖；无需 Apple 或 Google 凭据。

配置凭据前，先运行 `./.workflow-tools/run credentials list` 检查本机已登记的凭据引用。找到 `kind=api_token` 的引用后，运行 `./.workflow-tools/run credentials check --ref <ref> --provider r2` 验证它可用；项目使用 `credential_provider: "local"` 时改用 `--provider local`。可用时把返回的同一 `ref`、`kind`、`version` 登记到项目 `credential_refs`，并设置 `cloudflare.api_token_ref`。不要因项目还没有引用就认定本机没有 token，也不要要求用户把 token 发到聊天。

如果本机没有可用的 `api_token` 引用，先说明需要用户在 Cloudflare Dashboard 创建具备目标所需权限的 API Token。请用户把 token 保存到本机 `~/.config/workflow-tools/incoming/cloudflare/deploy-token.txt`（不要读取或回显内容），然后运行 `./.workflow-tools/run credentials import --ref <ref> --kind api_token --file "$HOME/.config/workflow-tools/incoming/cloudflare/deploy-token.txt"`；若项目使用 R2 凭据提供者，再运行 `./.workflow-tools/run credentials upload --ref <ref>`。随后用 `credentials check` 验证并将引用登记到项目配置。配置格式和权限见 [部署配置](references/deploy.md)。不执行 `wrangler login`，不把 token 写进 `.env` 或源码。

缺 Wrangler 时可在已授权的工具准备范围内运行 `./.workflow-tools/run setup --wrangler`。

用户已授权具体目标部署时，调用 `cloudflare_deploy` 后跟进 `workflow_job_status`；用 `cloudflare_status` 回读部署标识、版本或 URL，并核对 healthcheck。健康检查未配置时明确未验证，不能以命令退出 0 代替服务可用。

未知结果、超时或中断时，只调用 `workflow_job_resume` 回读已保存意图。不要通过直接 Wrangler、删除任务状态或更换幂等键重发。同一配置记录的健康检查失败可以恢复重验，不重新部署。

首版只部署指定代码目标，不调用数据库迁移或套餐升级；绑定的存储资源需要预先存在。缺相关权限或服务需首次开通时，集中列出所需事项，不改成全局万能 token。

交付目标账户/环境、部署标识、访问 URL（有时）、健康结果、证据路径和未验证项。
