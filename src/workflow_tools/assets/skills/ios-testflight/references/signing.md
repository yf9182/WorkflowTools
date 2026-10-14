# 文件签名与调用

项目 JSON 的 ios 段需要 enabled、project 或 workspace（相对项目根）、共享 scheme、bundle_id、team_id、app_store_id。apple 段配置 key_id、issuer_id、p8_ref；signing 段配置 p12_ref、password_ref、profiles（Bundle ID → provisioning_profile 引用）。所有引用在 credential_refs 登记 kind/version。

使用目标团队的 Apple Distribution .p12；必须有匹配私钥且未到期。各 App/扩展 profile 的 Team、Bundle、证书指纹和分发类型都必须匹配。开发、Ad Hoc、企业描述文件拒绝。签名助手使用受控钥匙串及 stdin 密码，禁止 GUI 提示；结束后移除本次钥匙串和本次安装的 profile，活跃子进程持有继承的文件锁。

缺少 profile 时先从工程 target/build settings 获取所有实际签名 Bundle ID，然后调用 MCP `ios_profiles_prepare(bundle_ids=[...])`，或由维护者运行 `./.workflow-tools/run ios profiles-prepare --bundle-id <bundle-id>`（多个 Bundle ID 重复该参数）。该工具只为已在目标团队登记的 iOS Bundle ID 创建/复用 App Store profile，使用现有 Distribution 证书，通过 ASC API 下载 profile 内容，验证 CMS、团队、Bundle ID 和证书指纹后加密托管并写入当前项目 profile 引用。它不创建或撤销证书、不登记 Bundle ID、不修改 App ID capabilities，也不修改 Xcode 源码。Team API key 需要 Provisioning API 权限；个人 API key 不支持 Provisioning 接口。权限不足时返回可识别 blocker，不能退回手工 profile 下载流程。

```sh
./.workflow-tools/run doctor --capability ios_build
./.workflow-tools/run ios build --version 1.2.3 --build-number 1230
./.workflow-tools/run job status <job_id>
./.workflow-tools/run ios upload --artifact-id <artifact_id>
./.workflow-tools/run ios status --version 1.2.3 --build-number 1230
./.workflow-tools/run job resume <job_id>
```

省略版本/构建号时使用工程 Release 设置，不自动改仓库。构建复制工程后逐 target 注入签名和版本，日志写在受限 artifacts/build-<job_id>。构建需要现有工程依赖可获取；不会偷偷改变 SDK 版本或系统 Xcode 设置。

上传门禁只需要 ASC 凭据、本机上传工具和已验证 IPA。上传成功以 Delivery UUID 为证据，Apple 的 processingState 为独立证据。测试组关联、外部测试审查、商店资料和正式提审不在本工具中。
