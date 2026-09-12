# nass3cp

`nass3cp` 通过一个短暂的 S3 兼容对象存储中转，把普通文件复制到 NAS，或从 NAS 复制到本机。NAS 服务只承担控制流量；文件数据由发送端上传至对象存储，再由接收端直接下载。当前提供 Cloudflare R2（推荐）和阿里云 OSS 配置示例。

```text
本机 ──HTTPS──► R2/S3 ──HTTPS──► NAS     # 上传到 NAS
NAS  ──HTTPS──► R2/S3 ──HTTPS──► 本机    # 从 NAS 下载
       本机 ◄──HTTPS 控制通道──► NAS
```

当前版本只支持单个普通文件，不递归复制目录，也不支持断点续传。运行时仅需 Python 3.8+ 标准库，没有第三方 Python 依赖。

## 安全模型

- NAS API 强制使用 TLS 1.2 或更高版本，并用 Bearer Token 认证。
- S3 Endpoint 和客户端拿到的预签名 URL 必须是 HTTPS。
- S3 Secret Access Key 只保存在 NAS 服务端；客户端只得到最长 1 小时、只允许操作单个分块的预签名 URL（URL 中的 Access Key ID 本身不是秘密）。
- 文件默认按 64 MiB 分块，落盘前校验端到端 SHA-256；目标文件通过同目录临时文件原子替换，失败时不会留下半个目标文件。
- 服务端只允许访问 `allowed_roots` 内的路径，默认拒绝覆盖已有文件。
- R2 会自动使用 AES-256 静态加密；OSS 示例则显式要求 AES-256 服务端加密。程序完成或失败后立即删除对象，仍建议给中转前缀配置 1 天生命周期规则，处理断电等极端情况。

这里的“传输加密”是 TLS：对象存储服务在接收请求时仍可看到明文内容。若威胁模型要求云厂商也无法看到内容，需要增加客户端侧 AEAD 加密；本项目没有用标准库自制密码算法。

## 安装

在 NAS 和客户端各执行一次：

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install /path/to/nass3cp
```

安装后有两个命令：客户端 `nass3cp`，NAS 服务端 `nass3cp-server`。

## Cloudflare R2 准备（推荐）

1. 开通 R2 并创建一个专用于 `nass3cp` 的私有 Standard Bucket，位置可选择 Asia-Pacific。
2. 为前缀 `nass3cp/` 增加“1 天后删除”的生命周期规则。
3. 创建仅限这个 Bucket 的 **Object Read & Write** R2 API Token，保存只显示一次的 Access Key ID 和 Secret Access Key。
4. 记录 Account ID；S3 Endpoint 是 `https://ACCOUNT_ID.r2.cloudflarestorage.com`，SigV4 Region 固定填写 `auto`。

Cloudflare R2 支持本程序使用的 SigV4 预签名 PUT、GET 和 DELETE。R2 示例会在签名中加入官方示例使用的 `X-Amz-Content-Sha256=UNSIGNED-PAYLOAD`。它不支持 OSS 示例中的 `x-amz-server-side-encryption` 请求头，因此 R2 配置的 `put_headers` 必须保持为空；R2 会自动加密所有静态对象和元数据。

完整的账号点击步骤、最小权限设置、生命周期规则和上线检查见 [`docs/cloudflare-r2.md`](docs/cloudflare-r2.md)。

官方文档：[R2 API Token](https://developers.cloudflare.com/r2/api/tokens/)、[预签名 URL](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)、[数据安全](https://developers.cloudflare.com/r2/reference/data-security/)。

## 阿里云 OSS 准备（可选）

1. 创建一个私有、标准存储（本地冗余）的 Bucket；地域尽量靠近 NAS 和客户端。
2. 在 Bucket 中启用 OSS 完全托管的 AES-256 默认加密。
3. 给前缀 `nass3cp/` 增加“1 天后删除”的生命周期规则。程序通常会立即删除对象，这条规则只是兜底。
4. 创建独立 RAM 用户，只授予该 Bucket 中转前缀的 `oss:GetObject`、`oss:PutObject`、`oss:DeleteObject` 权限，不要使用主账号 AccessKey。

最小 RAM 策略示例（替换 Bucket 名）：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["oss:GetObject", "oss:PutObject", "oss:DeleteObject"],
      "Resource": ["acs:oss:*:*:YOUR_BUCKET/nass3cp/*"]
    }
  ]
}
```

阿里云提供 S3 兼容 Endpoint，例如杭州为 `https://s3.oss-cn-hangzhou.aliyuncs.com`。本项目自己实现 SigV4，且发送固定 `Content-Length`，不使用 OSS 不支持的 `aws-chunked` 编码。

## 服务端配置

Cloudflare R2 推荐复制 [`examples/server.cloudflare-r2.json`](examples/server.cloudflare-r2.json)，将其中的 Account ID、Bucket 和 NAS 路径改成实际值，然后设置环境变量：

```bash
export NASS3CP_TOKEN='用密码管理器生成的高强度随机值'
export CLOUDFLARE_R2_ACCESS_KEY_ID='...'
export CLOUDFLARE_R2_SECRET_ACCESS_KEY='...'
```

如果继续使用阿里云，则复制 [`examples/server.aliyun.json`](examples/server.aliyun.json)，并设置：

```bash
export NASS3CP_TOKEN='用密码管理器生成的高强度随机值'
export ALIBABA_CLOUD_ACCESS_KEY_ID='LTAI...'
export ALIBABA_CLOUD_ACCESS_KEY_SECRET='...'
```

配置里的 `${NAME}` 只有在整个 JSON 字符串恰好是该占位符时才会展开。生产环境建议通过 systemd 的权限受限 `EnvironmentFile` 提供密钥。

控制通道必须有证书。使用公网域名时可直接使用受信任 CA 的证书；内网域名/IP 可建立自己的 CA，或仅用于试验时生成自签名证书。证书的 SAN 必须包含客户端传给 `--host` 的域名或 IP。

启动并先检查配置：

```bash
nass3cp-server --config /etc/nass3cp/server.json --check-config
nass3cp-server --config /etc/nass3cp/server.json --check-s3
nass3cp-server --config /etc/nass3cp/server.json
```

`--check-s3` 会通过配置的中转服务 PUT 一个几十字节的临时对象、GET 校验并 DELETE；它用于确认 Endpoint、Bucket、凭证和权限确实可用。

默认监听配置文件中的 `0.0.0.0:9443`。只应通过 ZeroTier、受控 FRP 入口或防火墙白名单暴露此端口。

仓库还提供了可按 NAS 路径调整的 [`examples/nass3cp-server.service`](examples/nass3cp-server.service)。SigV4 对时钟敏感，请确保 NAS 已启用 NTP/systemd-timesyncd。

## 使用

令客户端可读取 Token。环境变量不会像命令行参数那样直接出现在进程列表中：

```bash
export NASS3CP_TOKEN='与服务端相同的值'
```

本机上传到 NAS：

```bash
nass3cp --host nas.example --port 9443 --ca-file nas-ca.crt \
  ./movie.mkv nas:/volume1/share/movie.mkv
```

从 NAS 下载到本机：

```bash
nass3cp --host nas.example --port 9443 --ca-file nas-ca.crt \
  nas:/volume1/share/movie.mkv ./movie.mkv
```

恰好一个参数必须以 `nas:` 开头。NAS 相对路径以 `allowed_roots` 的第一项为基准；绝对路径也必须位于某个允许根目录中。已有目标默认报错，明确传 `--overwrite` 才会替换。并发数由 `--jobs` 控制，默认 2，范围 1–16。

`--insecure` 仍会加密控制流量，但不验证 NAS 身份，可能遭受中间人攻击，仅用于临时排障。

## 约 1 GiB 的 Cloudflare R2 费用

R2 Standard 当前每月免费额度包括 10 GB-month 存储、100 万次 Class A 操作和 1000 万次 Class B 操作，并且从 R2 直接流出公网免费。默认 64 MiB 分块的一次 1 GiB 复制约产生 16 次 PUT、16 次 GET 和 16 次免费 DELETE；如果账号当月仍在免费额度内，总成本通常为 **$0**。

免费额度只适用于 Standard。不要为临时中转使用 Infrequent Access，因为它没有这份免费额度，还存在数据读取费和 30 天最短存储期。价格会调整，最终以 [Cloudflare R2 定价](https://developers.cloudflare.com/r2/pricing/) 为准。

## 约 1 GB 的阿里云费用

以下按 2026-09-12 中国内地公共云、标准本地冗余、按量付费、普通公网 Endpoint、没有资源包或免费额度估算：

| 项目 | 1 GB 单次复制 |
|---|---:|
| 上传到 OSS | ¥0（流入免费） |
| 从 OSS 公网下载，00:00–08:00 | 约 ¥0.25 |
| 从 OSS 公网下载，08:00–24:00 | 约 ¥0.50 |
| 临时存储 1 小时 | 约 ¥0.000167 |
| 16 PUT + 16 GET + 约 16 DELETE | 免费额度外也仅约 ¥0.000048 |
| 合计 | 闲时约 ¥0.2502；忙时约 ¥0.5002 |

上传到 NAS 和从 NAS 下载的价格基本相同：两者都是一次免费流入 OSS，再有一次 OSS 公网流出。重试会重复产生流出流量；使用传输加速 Endpoint 还会叠加传输加速费，所以示例没有开启。标准存储当前每月每地域前 500 万次 PUT、前 2000 万次 GET 免费，实际小规模使用的请求费通常为零。

价格会调整，最终以阿里云账单为准：[OSS 价格详情](https://cn.aliyun.com/price/detail/oss)、[流量费用说明](https://help.aliyun.com/zh/oss/traffic-fees)、[存储费用说明](https://help.aliyun.com/zh/oss/storage-fees)、[使用 AWS SDK/S3 API 访问 OSS](https://help.aliyun.com/zh/oss/developer-reference/use-aws-sdks-to-access-oss)。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
