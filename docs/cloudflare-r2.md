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

把 [`examples/server.cloudflare-r2.json`](../examples/server.cloudflare-r2.json) 复制为 `/etc/nass3cp/server.json`，然后修改：

- `s3.endpoint`：替换成 `https://ACCOUNT_ID.r2.cloudflarestorage.com`
- `s3.bucket`：替换成第 2 步创建的 Bucket 名
- `allowed_roots`：改成 NAS 上允许访问的真实目录
- `tls.cert_file`、`tls.key_file` 和 `state_dir`：按实际安装路径调整

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

可以从 [`examples/nass3cp.cloudflare-r2.env.example`](../examples/nass3cp.cloudflare-r2.env.example) 复制环境文件，将秘密放入 `/etc/nass3cp/nass3cp.env`，不要直接写进 JSON：

```text
NASS3CP_TOKEN=替换为随机控制通道令牌
CLOUDFLARE_R2_ACCESS_KEY_ID=替换为Access-Key-ID
CLOUDFLARE_R2_SECRET_ACCESS_KEY=替换为Secret-Access-Key
```

可以使用 Python 标准库生成控制通道令牌：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

保护配置和环境文件：

```bash
sudo chown root:nass3cp /etc/nass3cp/server.json
sudo chmod 640 /etc/nass3cp/server.json
sudo chown root:root /etc/nass3cp/nass3cp.env
sudo chmod 600 /etc/nass3cp/nass3cp.env
```

JSON 不含实际密钥，但运行服务的 `nass3cp` 用户必须能够读取它。环境文件则由 systemd 在降权前读取，因此可以保持仅 root 可读。仓库中的 [`examples/nass3cp-server.service`](../examples/nass3cp-server.service) 已通过 `EnvironmentFile=/etc/nass3cp/nass3cp.env` 加载这些变量。

## 6. 上线前验证

先在一个仅 root 可读的 shell 中加载环境变量：

```bash
set -a
. /etc/nass3cp/nass3cp.env
set +a
```

验证本地 TLS、目录和配置：

```bash
/opt/nass3cp/.venv/bin/nass3cp-server \
  --config /etc/nass3cp/server.json --check-config
```

再验证 R2 的 PUT、GET 和 DELETE。该命令只写入几十字节，读回校验后立即删除：

```bash
/opt/nass3cp/.venv/bin/nass3cp-server \
  --config /etc/nass3cp/server.json --check-s3
```

成功输出：

```text
S3 relay check passed (PUT, GET, DELETE)
```

最后启动服务并进行一次小文件测试，再测试 1 GiB 文件：

```bash
sudo systemctl enable --now nass3cp-server

nass3cp --host nas.example --port 9443 --ca-file nas-ca.crt \
  ./small-test.bin nas:/volume1/share/small-test.bin
```

## 安全和费用说明

- NAS 控制通道和 R2 数据通道都使用 HTTPS/TLS；程序拒绝 HTTP Endpoint 和 HTTP 预签名 URL。
- R2 自动进行 AES-256 静态加密，但 Cloudflare 在服务端解密时仍能看到文件明文。若要求云厂商不能读取内容，需要另加客户端侧 AEAD 加密。
- 预签名 URL 是短时 Bearer 凭证，应像临时密码一样处理；默认 15 分钟过期。
- 在上述免费额度内，偶尔传输 1 GiB 的 R2 费用通常为 $0。重试不会产生 R2 公网下行费，但仍会计入操作次数。
- 中国大陆到 R2 的速度和稳定性取决于运营商及跨境链路，建议在 NAS 所在网络和常用客户端网络分别实测。

官方参考：[创建 Bucket](https://developers.cloudflare.com/r2/buckets/create-buckets/)、[创建 R2 API Token](https://developers.cloudflare.com/r2/api/tokens/)、[预签名 URL](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)、[生命周期规则](https://developers.cloudflare.com/r2/buckets/object-lifecycles/)、[数据安全](https://developers.cloudflare.com/r2/reference/data-security/)、[R2 定价](https://developers.cloudflare.com/r2/pricing/)。
