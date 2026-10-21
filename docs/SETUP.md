# 集中授权接入清单

所有授权文件在仓库外 `~/.config/workflow-tools/`。原 CalorieSnapshot 已有 ASC `.p8` 与 Google 服务账号可以通过导入器读取并加密，不修改原配置、原密钥或业务代码。

本机已有材料已接入，见 [当前凭据状态与仅需补充的材料](CREDENTIAL_STATUS.md)。已导入的引用无需重复导入；以下命令用于首次接入或明确轮换。

## 1. 目录与需补的材料

```text
~/.config/workflow-tools/
├── bootstrap/connection.json        自动生成 AES 密钥；填写本机 Cloudflare 默认账号与 R2 访问字段
├── admin/r2-writer.json              独立 R2 上传管理访问凭据
├── incoming/
│   ├── apple/distribution.p12        目标团队 Apple Distribution 证书和私钥
│   ├── apple/p12-password.txt        .p12 密码，一行
│   ├── apple/profiles/*.mobileprovision  主 App 和各扩展的 App Store 描述文件
│   ├── google/analytics-reader.json  新增服务账号时放这里
│   └── cloudflare/deploy-token.txt   Cloudflare 部署 token，一行
├── vault/                           已导入加密对象，无秘密明文输出
├── run/                             活跃锁保护的临时签名文件
└── state/                           每个项目的任务、证据及外部写入意图
```

初始化：

```sh
./bin/workflow-tools credentials init
```

连接文件已生成的 `encryption_key` 必须保留，不要重新填写或发到聊天。填写 `r2.account_id`、`r2.bucket`、`r2.access_key_id`、`r2.secret_access_key`；特殊区域桶还需要 `r2.jurisdiction`（eu/us/fedramp）。如需让本机所有项目默认使用同一 Cloudflare 部署账户，可填写 `cloudflare.account_id`；项目 `.workflow-tools/project.json` 中的非空 `cloudflare.account_id` 会优先使用。Cloudflare Account ID 不是 API Token。R2 Object Read only 访问凭据限定在这个私有桶，不启用 r2.dev 或公共域名。

复制 `admin/r2-writer.example.json` 到 `admin/r2-writer.json`，填独立 Object Read & Write 的 `access_key_id` 与 `secret_access_key`。桶首次建立及可能的付款方式配置由账号持有人一次完成；工具不自动买服务或升级套餐。

免 Apple 网页登录的签名需要 `.p12`、密码和每个 Bundle ID 的 App Store profile；只导出 `.cer` 不含私钥，不能签名。通常无需手工下载 profile：若已有目标团队的 ASC Team API `.p8`（建议 Admin 角色/Provisioning 权限）、目标 Bundle ID 已登记且 `.p12` 有效，Agent 可调用 `ios_profiles_prepare` 自动创建或复用描述文件，验证后加密存入凭据库并更新项目引用。该 API 不会创建 Bundle ID、证书或 App ID capabilities；这些目标身份必须事先存在并具备正确能力配置。个人 API key 不支持 Provisioning API。仅当该自动化所需的团队 API key 缺失或权限不足时，才需补充正确授权。不要按 profile 文件名判断类型，工具会拒绝开发、Ad Hoc、企业或已过期文件；profile 必须引用同一 Distribution 证书。

Cloudflare 部署 token 按实际目标授予 Workers Editor（新 Worker 创建需产品级 Admin）或 Pages 编辑权限；改域名/路由才增加对应 Zone 权限。R2 S3 访问与 Cloudflare 部署 token 是不同用途的授权。

## 2. 导入与上传

部署前先检查本机凭据引用，避免重复索要已经导入的 token：

```sh
./bin/workflow-tools credentials list
./bin/workflow-tools credentials check --ref personal/cloudflare-deploy --provider r2
```

`list` 只返回引用名、类型和版本，不会读取或显示凭据内容。项目可将有效引用登记到 `credential_refs`，并在 `cloudflare.api_token_ref` 使用同一引用。若确实没有 API Token，再让用户将其保存到 `incoming/cloudflare/deploy-token.txt`，通过下面的命令导入和上传。

```sh
./bin/workflow-tools credentials import-legacy --source "$HOME/.config/calorie-ops" --prefix personal
./bin/workflow-tools credentials import --ref personal/distribution --kind pkcs12 --file "$HOME/.config/workflow-tools/incoming/apple/distribution.p12"
./bin/workflow-tools credentials import --ref personal/distribution-password --kind password --file "$HOME/.config/workflow-tools/incoming/apple/p12-password.txt"
./bin/workflow-tools credentials import --ref personal/calorie-profile --kind provisioning_profile --file "$HOME/.config/workflow-tools/incoming/apple/profiles/calorie.mobileprovision"
./bin/workflow-tools credentials import --ref personal/cloudflare-deploy --kind api_token --file "$HOME/.config/workflow-tools/incoming/cloudflare/deploy-token.txt"

# 各引用分别上传并回读验证；同版本不覆盖
./bin/workflow-tools credentials upload --ref personal/apple-p8
./bin/workflow-tools credentials upload --ref personal/google-service-account
./bin/workflow-tools credentials check --ref personal/apple-p8
```

维护者可手工创建 profile 时使用 `./bin/workflow-tools ios profiles-prepare --bundle-id <bundle-id>`；普通项目用户直接让 Agent 执行构建，Agent 会在 profile 缺失且授权足够时自动准备。

文件 0600，目录 0700。文件路径进入命令参数，秘密内容不进入参数；不要使用 `--token <明文>`、粘贴聊天或在项目 `.env` 存部署授权。导入只输出引用、类型和版本。

项目配置见 [CalorieSnapshot 示例](../examples/calorie-snapshot.project.json) 和 [Cloudflare 示例](../examples/cloudflare.project.json)。先安装再填写 `.workflow-tools/project.json`；示例身份和引用须与目标一致。运行默认 `credential_provider: "r2"`；云端尚未配置时，只读接入验证可显式用 `local`。

## 3. 轮换、备份与恢复

重复 import 自动增加版本，upload 新版本，再更新各项目引用 version。旧版本不被覆盖。文件补齐且项目配置没变时可 resume；配置变了创建新任务，外部写入仍受已有意图保护。

```sh
./bin/workflow-tools credentials backup --out /仓库外/connection-backup.enc --password-file /仓库外/backup-password.txt
./bin/workflow-tools credentials restore --file /仓库外/connection-backup.enc --password-file /仓库外/backup-password.txt
./bin/workflow-tools credentials sweep
```

备份口令文件 0600、至少 12 字节，与加密备份分开传递。备份采用 Scrypt + AES-GCM，含连接与已导入加密对象，不含 R2 写授权或任务账本。新机器先安装再恢复；已有连接不覆盖。解密密钥丢失后不能用 R2 token 解密，请保留受保护的备份。

工具不提供“导出明文到 Agent”接口。日常自动生成 ASC/Google 短 token；账号撤销、证书到期或未授权新 GA4 属性时需要补权限/文件，不能通过自动登录补救。
