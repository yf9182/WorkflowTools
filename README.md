# WorkflowTools

WorkflowTools 为项目里的 Agent 提供 iOS 签名构建与 TestFlight、GA4/ASC 数据读取与分析，以及 Cloudflare Workers、静态资源和 Pages 部署能力。用户通过 Agent 安装和使用，不需要自己运行安装命令或手工复制 Skill 文件。

## 让 Agent 安装到当前项目

在 Codex 中打开要使用的项目，然后把下面这句话发给 Agent：

> 请从 https://github.com/yf9182/WorkflowTools 为当前项目安装 WorkflowTools。先阅读仓库 README 和接入说明，再按仓库提供的安装流程安装四个 Skills、登记项目级 MCP 并生成项目配置。保留已有 MCP、Skills 和审批设置，不改业务源码。完成后检查安装结果，并运行 workflow_doctor 报告当前项目各能力还缺什么。

Agent 会把 `workflow-credentials`、`ios-testflight`、`app-analytics`、`cloudflare-deploy` 安装到项目，并接入对应的本地 stdio MCP。重复安装会保留已有项目配置和 Agent 设置。若宿主要求信任项目或重新加载 MCP，请按 Codex 提示完成。

## 安装后这样使用

直接用自然语言描述目标，Agent 会先检查相关能力的门禁，只要求该任务真正需要的配置和授权：

- “检查 iOS TestFlight 构建还缺什么；齐备后构建 1.2.3（1230）。”
- “获取最近 28 天 GA4 和 ASC 销售数据，生成带来源和日期范围的报告。”
- “检查 Cloudflare 的 production 前端部署配置，然后部署 frontend。”
- “查看上一个任务的进度；如果中断，从最近检查点继续。”

Agent 可用工具包括 `workflow_doctor`、`ios_build`、`ios_upload_testflight`、`ios_testflight_status`、`analytics_fetch`、`analytics_report`、`cloudflare_deploy`、`cloudflare_status`、`workflow_job_status` 和 `workflow_job_resume`。长任务会返回 `job_id`，可以查询和恢复。iOS 上传会先记录意图并核验远端状态，结果未知时不会盲目重复上传。

## 授权与首次接入

凭据集中保存在机器本地 `~/.config/workflow-tools/`，不进入项目仓库。Agent 通过凭据引用调用工具，不会把明文密钥返回给对话。安装到同一台 Mac 的其他项目可复用现有凭据；换机器时需要导入受保护的连接授权。

已配置私有 Cloudflare R2，凭据对象上传前使用 AES-256-GCM 加密。运行端令牌只读，凭据上传使用另一个仅对专用桶有读写权限的令牌。日常任务不需要重复登录 Cloudflare、App Store Connect 或 Google；首次机器接入、令牌撤销、权限变化和证书到期仍需补充授权。

需要准备的材料、文件位置和权限见[集中接入清单](docs/SETUP.md)。本机当前接入状态见[凭据状态](docs/CREDENTIAL_STATUS.md)。项目配置只保存工程路径、目标身份、数据口径、部署目标和凭据引用，不保存密钥。

## 能力边界

- 门禁按能力独立。TestFlight 上传不依赖 GA4、商店文案、版本任务单或真机验收。
- iOS 使用临时工程副本和受限签名环境；各 App/扩展都需要匹配的 App Store 描述文件。构建、上传、Apple 处理和可供测试是不同状态。
- 分析报告注明来源和日期范围。GA4 指标、样本门槛和漏斗由项目配置提供；来源缺失或样本不足时会明确报告。
- Cloudflare 部署需在项目配置具体目标、环境、构建命令和 Wrangler 配置。部署后工具回读部署标识与已配置的健康检查。
- 不自动提审、发布、邀请测试者、购买服务或升级套餐。Agent 宿主的沙箱、项目授权和系统权限仍然生效。

## 开发维护

CLI 与 MCP 共享同一套 Python 运行库。安装器和工具源码位于 `src/workflow_tools`；本地测试和构建命令仅供维护者使用，项目使用者无需运行：

```sh
./scripts/bootstrap.sh
.venv/bin/python -m pytest -q
.venv/bin/python -m build --wheel
```

实现细节与验证限制见[验证报告](docs/VERIFICATION.md)。
