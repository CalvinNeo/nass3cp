# Cloudflare R2 配置指南

这份配置让 `nass3cp` 使用私有 Cloudflare R2 Bucket 作为临时中转。程序使用 R2 的 S3 兼容接口和 SigV4 预签名 URL；NAS 与客户端不需要安装 Cloudflare SDK。

## 1. 开通 R2

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)，进入 **R2 Object Storage**。
2. 按页面提示开通 R2。Cloudflare 要求先开通计费，即使实际用量完全落在免费额度内。
3. 记录页面上显示的 **Account ID**；它不是密钥。

R2 Standard 当前每月包含 10 GB-month 存储、100 万次 Class A 操作和 1000 万次 Class B 操作，直接从 R2 经 S3 API 流出不收公网流量费。免费额度只适用于 Standard，不要把这个临时 Bucket 设成 Infrequent Access。

## 2. 创建专用私有 Bucket

1. 在 R2 页面选择 **Create bucket**。
2. Bucket 名可使用 `nass3cp-relay-你的唯一后缀`；只能包含小写字母、数字和连字符。
3. 保持默认的 **Standard** 存储；如果创建页面没有存储类型选项，无需额外设置，本项目不会发送 Infrequent Access 请求头。
4. Location 建议选择 **Asia-Pacific (APAC)**。Location Hint 只是尽力靠近，并不保证具体国家或机房；也可以先保留 Automatic 后实测。
5. 创建后不要启用 Public Development URL，也不需要绑定自定义域名。R2 Bucket 默认是私有的。

Cloudflare 的 R2 令牌只能限制到 Bucket，不能进一步限制到 `nass3cp/` 前缀。因此应为本程序单独创建 Bucket，不要和备份或网站资源共用。

## 3. 配置一天生命周期兜底

程序在成功、失败或取消后都会立即删除分块。生命周期规则只处理 NAS 断电或进程被强制终止等情况遗留的对象。

1. 打开刚创建的 Bucket，进入 **Settings**。
2. 在 **Object Lifecycle Rules** 下选择 **Add rule**。
3. 新建启用状态的规则，例如 `delete-nass3cp-temp`。
4. Prefix 填 `nass3cp/`。
5. 操作选择在对象创建 **1 day** 后删除/过期。
6. 保存规则，不要增加向 Infrequent Access 转换的动作。

## 4. 创建最小权限 S3 凭证

1. 回到 R2 Overview。
2. 在 **Account Details** 中找到 **API Tokens**，选择 **Manage**。
3. 选择 **Create Account API token**。如果只有个人账号，也可以创建 User API token；Account token 更适合常驻 NAS 服务。
4. 权限选择 **Object Read & Write**。
5. Bucket 范围选择 **Apply to specific buckets only**，只选择刚创建的中转 Bucket。
6. 创建令牌，并立即保存这三个值：
   - **Access Key ID**
   - **Secret Access Key**（只显示一次）
   - **Account ID**

这里需要的是 R2 页面生成的 S3 Access Key，而不是 Cloudflare 其他页面使用的普通 API Token。

## 5. 配置 NAS

整个目录可以放在 NAS 任意位置，例如 `/volume1/apps/nass3cp`。不需要把程序安装到系统目录，也不需要创建 systemd 服务：

```text
nass3cp/
├── bin/
│   ├── nass3cp
│   └── nass3cp-server
├── config/
│   ├── server.json
│   ├── server.env          # 从示例复制，不提交 Git
│   └── client.env          # 可选；默认交互输入密码
├── src/
├── state/                  # 首次运行自动创建
└── run/                    # 使用 nohup 时存放日志和 PID
```

进入项目根目录并准备本地文件：

```bash
cd /volume1/apps/nass3cp
cp config/server.env.example config/server.env
chmod 700 bin/nass3cp bin/nass3cp-server
chmod 600 config/server.env
```

直接修改 [`config/server.json`](../config/server.json)。它默认使用密码认证且不启用应用层 TLS，不需要 CRT，并监听 `0.0.0.0:9443`。建议用 NAS 防火墙限制来源，或把 `listen` 改成 NAS 的覆盖网络虚拟 IP；仅供同机 FRP 使用时可以改成 `127.0.0.1`。

修改以下字段：

- `s3.endpoint`：替换成 `https://ACCOUNT_ID.r2.cloudflarestorage.com`
- `s3.bucket`：替换成第 2 步创建的 Bucket 名
- `allowed_roots`：改成 NAS 上允许访问的真实目录
- `state_dir`：默认是 `../state`，按配置文件所在目录解析，即项目根目录的 `state/`

以下 R2 参数应保持不变：

```json
{
  "region": "auto",
  "addressing_style": "virtual",
  "presign_unsigned_payload": true,
  "put_headers": {}
}
```

`presign_unsigned_payload` 让预签名 URL 包含 R2 官方示例使用的 `X-Amz-Content-Sha256=UNSIGNED-PAYLOAD`。不要复制阿里云示例中的 `x-amz-server-side-encryption` 请求头；R2 的 S3 兼容接口不支持该 SSE-S3 请求头，但所有 R2 对象及元数据本身都会自动使用 AES-256 静态加密。

编辑 [`config/server.env.example`](../config/server.env.example) 的副本 `config/server.env`，不要把实际秘密写进 JSON：

```text
NASS3CP_PASSWORD=替换为高强度随机密码
CLOUDFLARE_R2_ACCESS_KEY_ID=替换为Access-Key-ID
CLOUDFLARE_R2_SECRET_ACCESS_KEY=替换为Secret-Access-Key
```

可以使用 Python 标准库生成密码：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

`bin/nass3cp-server` 会自动读取项目内的 `config/server.env`；未指定 `--config` 时使用 `config/server.json`。环境文件按 `KEY=VALUE` 解析，不作为 shell 脚本执行，不支持变量展开或命令替换；进程外部已经设置的环境变量优先。

也可以把密码直接写成 `"auth": {"password": "..."}`，但项目内的 `server.env` 更不容易被误提交。旧版的 `auth.token`/`NASS3CP_TOKEN` 仍然兼容。

## 6. 上线前验证

在项目根目录这样验证配置和 R2：

```bash
./bin/nass3cp-server --check-config
./bin/nass3cp-server --check-s3
```

`--check-s3` 只写入几十字节，读回校验后立即删除。

成功输出：

```text
S3 relay check passed (PUT, GET, DELETE)
```

前台启动服务：

```bash
./bin/nass3cp-server
```

需要退出 SSH 后继续运行时，可以使用 Linux 自带的 `nohup`，仍然不依赖 systemd：

```bash
mkdir -p run
nohup ./bin/nass3cp-server >run/nass3cp.log 2>&1 &
echo $! >run/nass3cp.pid
```

检查进程和日志：

```bash
ps -p "$(cat run/nass3cp.pid)" -f
tail -f run/nass3cp.log
```

确认 `ps` 显示的是本项目的 `bin/nass3cp-server` 后，可以正常停止；服务会处理 `SIGTERM` 并关闭监听端口：

```bash
kill "$(cat run/nass3cp.pid)"
```

客户端机器也可以直接保留一份项目目录，不需要安装。覆盖网络模式测试小文件时加 `--no-tls`，程序会在终端提示密码且输入不会回显：

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 --port 9443 \
  ./small-test.bin nas:/volume1/share/small-test.bin
NAS password:
```

将 `10.10.10.2` 换成 NAS 的覆盖网络 IP。无人值守场景可使用 `--password-file`，或复制 `config/client.env.example` 为 `config/client.env` 并设置 `NASS3CP_PASSWORD`。

Windows PowerShell 使用 `.cmd` 启动器，它会自动寻找 Python 3.8+：

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 --port 9443 `
  nas:/volume1/share/small-test.bin .
```

若 Python 不在 `PATH`，先设置 `$env:NASS3CP_PYTHON` 为 `python.exe` 的完整路径。

## 安全和费用说明

- R2 数据通道始终使用 HTTPS；程序拒绝 HTTP Endpoint 和 HTTP 预签名 URL。
- 无应用层 TLS 时，NAS 控制通道是 HTTP，密码也在这条通道内传输。只有在底层覆盖网络或隧道已经提供加密和身份认证时才可使用；内网地址本身不是安全边界。
- 无 TLS 服务端允许监听 `0.0.0.0` 或 `::`，但这会在所有网卡上开放控制端口；应通过防火墙限制来源，或优先绑定覆盖网络 IP。
- R2 自动进行 AES-256 静态加密，但 Cloudflare 在服务端解密时仍能看到文件明文。若要求云厂商不能读取内容，需要另加客户端侧 AEAD 加密。
- 预签名 URL 是短时 Bearer 凭证，应像临时密码一样处理；默认 15 分钟过期。
- 在上述免费额度内，偶尔传输 1 GiB 的 R2 费用通常为 $0。重试不会产生 R2 公网下行费，但仍会计入操作次数。
- 中国大陆到 R2 的速度和稳定性取决于运营商及跨境链路，建议在 NAS 所在网络和常用客户端网络分别实测。

官方参考：[创建 Bucket](https://developers.cloudflare.com/r2/buckets/create-buckets/)、[创建 R2 API Token](https://developers.cloudflare.com/r2/api/tokens/)、[预签名 URL](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)、[生命周期规则](https://developers.cloudflare.com/r2/buckets/object-lifecycles/)、[数据安全](https://developers.cloudflare.com/r2/reference/data-security/)、[R2 定价](https://developers.cloudflare.com/r2/pricing/)。
