# 部署目标

cloudflare 段配置 api_token_ref 和 targets。`account_id` 可省略，此时使用本机 `~/.config/workflow-tools/bootstrap/connection.json` 中的 `cloudflare.account_id`；项目填写非空 `account_id` 时优先使用项目值。目标包含 kind、directory、name、environments、default_environment；环境必须显式列出。

```json
{
 "account_id":"","api_token_ref":"personal/cloudflare-deploy",
 "targets": {
  "backend":{"kind":"workers","directory":"backend","wrangler_config":"backend/wrangler.jsonc","environments":{"production":{"name":"my-api","wrangler_environment":""}},"default_environment":"production"},
  "frontend":{"kind":"pages","directory":"frontend","build_command":["npm","run","build"],"output_directory":"frontend/dist","environments":{"production":{"name":"my-web","branch":"main","healthcheck":{"url":"https://my-web.pages.dev","expected_status":200}}},"default_environment":"production"}
 }
}
```

Workers/Workers assets 配置的 account_id、解析后的名称、env 必须一致；workers-assets 需要 Wrangler assets。相对路径按项目根解析，构建命令以 directory 为 cwd。Pages 输出目录也按项目根解析。Worker 默认配置不传 --env；只有 wrangler_environment 非空才使用 Wrangler 命名环境。

使用目标账户/Worker 的 Editor 权限（创建新 Worker 需要产品级 Admin），Pages 使用对应 Pages 编辑权限；改变路由/域名才需要相应 Zone Workers Routes Write。绑定既有资源不需要为了部署申请所有资源管理权限。拒绝缺资源标识的自动配置；数据库迁移和套餐升级不属于本工具。

```sh
./.workflow-tools/run setup --wrangler
./.workflow-tools/run doctor --capability cloudflare_deploy --target backend --environment production
./.workflow-tools/run cloudflare deploy --target backend --environment production
./.workflow-tools/run job status <job_id>
./.workflow-tools/run cloudflare status --target backend --environment production
```

构建步骤不接收本工具注入的部署 token。部署步骤仅在子进程环境注入 token。首次 Pages 项目创建也落意图；不确定结果仅回读，不重复 POST。健康失败不会重部署。缺健康检查时结果明确 unverified。
