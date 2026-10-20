# 首次接入、轮换与恢复

1. `./.workflow-tools/run credentials init` 创建仓库外授权根目录。把连接文件、上传管理文件和密码文件保持 0600；目录 0700。秘密不要发到聊天或 Git。
2. 在 `bootstrap/connection.json` 填写 R2 account_id、bucket、access_key_id、secret_access_key；保留工具生成的 encryption_key。需要设置本机 Cloudflare 部署默认账户时填写 `cloudflare.account_id`，项目级 `cloudflare.account_id` 可以覆盖它。桶不启用 r2.dev 或公共域名。客户端 AES-GCM 加密与云端静态加密同时存在。
3. 管理上传需要独立桶级 Object Read & Write 凭据，放入 `admin/r2-writer.json`。运行连接使用桶级 Object Read only 凭据；这些 Object 权限用于 S3 API。
4. 放入 `incoming/apple/distribution.p12`、`incoming/apple/p12-password.txt`、每个 Bundle ID 的 `incoming/apple/profiles/*.mobileprovision`、`incoming/cloudflare/deploy-token.txt`。Apple `.p8` 和 Google JSON 可从旧授权根目录导入。

```sh
./.workflow-tools/run credentials import-legacy --source "$HOME/.config/calorie-ops" --prefix personal
./.workflow-tools/run credentials import --ref personal/distribution --kind pkcs12 --file "$HOME/.config/workflow-tools/incoming/apple/distribution.p12"
./.workflow-tools/run credentials import --ref personal/distribution-password --kind password --file "$HOME/.config/workflow-tools/incoming/apple/p12-password.txt"
./.workflow-tools/run credentials import --ref personal/app-profile --kind provisioning_profile --file "$HOME/.config/workflow-tools/incoming/apple/profiles/app.mobileprovision"
./.workflow-tools/run credentials upload --ref personal/apple-p8
./.workflow-tools/run credentials check --ref personal/apple-p8
```

导入输出包含非秘密 `credential_ref`，将其加入项目 JSON 的 `credential_refs`；引用位置使用同一 ref。每个引用必须指定 kind 和 version。其他凭据同样先 import 再 upload。团队级凭据可以被多个已登记项目引用。

轮换重复 import，自动增加版本；上传后更新受影响项目的引用版本。旧版本不被覆盖或隐式删除。工具启动新的 Runtime 后读取新版本。变更操作配置会拒绝恢复旧任务；创建同操作的新任务仍受既有外部意图约束。

本地诊断可显式使用 `credential_provider: "local"`，仍读取本地加密对象；云端故障不会自动降级到本地旧凭据。

```sh
./.workflow-tools/run credentials backup --out /仓库外/connection-backup.enc --password-file /仓库外/backup-password.txt
./.workflow-tools/run credentials restore --file /仓库外/connection-backup.enc --password-file /仓库外/backup-password.txt
./.workflow-tools/run credentials sweep
```

备份采用 Scrypt + AES-GCM，包含连接授权和已导入加密对象，不包含 R2 写管理凭据或任务账本。口令从 0600 文件读取，不进 argv；口令文件与备份分开传递。新机器先安装套件再恢复，已有连接不会覆盖。运行状态不随连接迁移，不要在多台机器同时发布同一目标。新机器遇到已有远端构建而没有本机绑定时会拒绝重复上传。

第一次项目注册及系统签名权限由宿主控制；本工具不改信任/审批配置。证书到期、账号权限被撤销仍需补文件或权限，不能通过反复登录解决。
