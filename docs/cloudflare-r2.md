# Cloudflare R2 Setup Guide

This setup uses a private Cloudflare R2 bucket as a temporary relay for `nass3cp`. The program uses R2's S3-compatible API and SigV4 presigned URLs. Neither the NAS nor the client needs the Cloudflare SDK.

## 1. Enable R2

1. Sign in to the [Cloudflare Dashboard](https://dash.cloudflare.com/) and open **R2 Object Storage**.
2. Follow the prompts to enable R2. Cloudflare requires billing to be enabled even if your usage stays entirely within the free tier.
3. Record the **Account ID** displayed on the page; it is not a secret.

R2 Standard's monthly free tier includes 10 GB-month of storage, one million Class A operations, and ten million Class B operations. Direct egress from R2 through the S3 API has no internet bandwidth charge. The free tier applies only to Standard; do not use Infrequent Access for this temporary bucket.

The client defaults to a bounded pipeline with `--mode pipeline --inflight 3`. The sender keeps at most three unacknowledged chunks in R2. It continues as the receiver verifies each chunk, writes it to disk, and successfully deletes the R2 object. With the default 64 MiB chunks, one normal transfer peaks at about 192 MiB of cloud storage. Multiple copy processes each have their own window, so their usage adds up. Adjust `--inflight` in the client command: lowering it reduces peak storage but may lower throughput; increasing it has the opposite effect.

For single-file transfers with `--mode parallel`, all unfinished chunks are scheduled independently. `--jobs` still limits simultaneous client S3 requests, while `--inflight` does not apply. Completed objects remain until the entire file succeeds or `transfer_ttl_seconds` expires, allowing the next run to upload or download only missing chunks. Peak cloud storage can therefore approach the full file size. Resume state is retained for 24 hours by default; do not set the bucket lifecycle shorter than your intended resume window. `--resume` is a compatibility alias for this mode.

Recursive copies create a separate pipeline for each file, so peak cloud storage remains bounded by `--inflight`. Each nonempty file still generates at least one PUT, GET, and DELETE. For directories with hundreds of thousands or millions of small files, watch R2 Class A/Class B operation counts as well as total bytes. The default `--rpolicy=auto` first gzips files with suitable extensions, reducing relay bytes and chunk requests when compression actually makes the file smaller. Use `--dry` to inspect original size totals and planned file counts without creating R2 objects.

## 2. Create a dedicated private bucket

1. Select **Create bucket** on the R2 page.
2. Choose a name such as `nass3cp-relay-your-unique-suffix`, using lowercase letters, digits, and hyphens.
3. Keep **Standard** storage. If the creation page has no storage-class option, no extra setting is needed; this project does not send an Infrequent Access request header.
4. **Asia-Pacific (APAC)** is a suggested location. A Location Hint is best-effort and does not guarantee a particular country or data center. You can also start with Automatic and measure performance.
5. Leave the Public Development URL disabled. No custom domain is needed. R2 buckets are private by default.

R2 tokens can be restricted to a bucket, but not further to the `nass3cp/` prefix. Use a dedicated bucket for this program instead of sharing one with backups or website assets.

## 3. Configure a one-day lifecycle fallback

Normal pipeline transfers delete chunks immediately after success, failure, or cancellation. `--mode parallel` deliberately retains chunks after interruption, deletes them after success, and relies on server cleanup after expiry. Lifecycle rules also remove objects left behind by events such as NAS power loss or forced process termination.

1. Open the new bucket and go to **Settings**.
2. Under **Object Lifecycle Rules**, select **Add rule**.
3. Create an enabled rule, for example `delete-nass3cp-temp`.
4. Set the prefix to `nass3cp/`.
5. Choose deletion/expiration **1 day** after object creation.
6. Save the rule without adding an Infrequent Access transition.

## 4. Create S3 credentials with minimum permissions

1. Return to R2 Overview.
2. Under **Account Details**, find **API Tokens** and select **Manage**.
3. Select **Create Account API token**. A personal account can also use a User API token; an Account token is better suited to a persistent NAS service.
4. Select **Object Read & Write** permissions.
5. Choose **Apply to specific buckets only** and select only the relay bucket you just created.
6. Create the token and immediately save these three values:
   - **Access Key ID**
   - **Secret Access Key** (shown only once)
   - **Account ID**

Use the S3 access keys generated on the R2 page, not a general Cloudflare API token from another page.

## 5. Configure the NAS

Place the complete project directory anywhere on the NAS, such as `/volume1/apps/nass3cp`. No system-wide installation or systemd service is required:

```text
nass3cp/
├── bin/
│   ├── nass3cp
│   └── nass3cp-server
├── config/
│   ├── server.json
│   ├── server.env          # Copy from the example; do not commit to Git
│   └── client.env          # Optional; the default is an interactive password prompt
├── src/
├── state/                  # Created automatically on first run
└── run/                    # Logs and PID when using nohup
```

Enter the project root and prepare the local files:

```bash
cd /volume1/apps/nass3cp
cp config/server.env.example config/server.env
chmod 700 bin/nass3cp bin/nass3cp-server
chmod 600 config/server.env
```

Edit [`config/server.json`](../config/server.json) directly. It uses password authentication without application-layer TLS by default, requires no certificate, and listens on `0.0.0.0:9443`. Restrict incoming connections with the NAS firewall or set `listen` to the NAS overlay IP. Use `127.0.0.1` when only a local FRP process needs access.

Update these fields:

- `s3.endpoint`: set it to `https://ACCOUNT_ID.r2.cloudflarestorage.com`.
- `s3.bucket`: use the bucket name from step 2.
- `allowed_roots`: use the actual NAS directories that may be accessed.
- `state_dir`: the default is `../state`, resolved relative to the configuration file, which places it at `state/` in the project root.

Keep these R2 settings unchanged:

```json
{
  "region": "auto",
  "addressing_style": "virtual",
  "presign_unsigned_payload": true,
  "put_headers": {}
}
```

`presign_unsigned_payload` adds `X-Amz-Content-Sha256=UNSIGNED-PAYLOAD` to presigned URLs, as in R2's official examples. Do not copy the `x-amz-server-side-encryption` header from the Alibaba Cloud example. R2's S3-compatible API does not support that SSE-S3 header, but all R2 objects and metadata are automatically encrypted at rest with AES-256.

Edit `config/server.env`, copied from [`config/server.env.example`](../config/server.env.example), instead of placing actual secrets in JSON:

```text
NASS3CP_PASSWORD=replace-with-a-strong-random-password
CLOUDFLARE_R2_ACCESS_KEY_ID=replace-with-access-key-id
CLOUDFLARE_R2_SECRET_ACCESS_KEY=replace-with-secret-access-key
```

Generate a password with the Python standard library:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

`bin/nass3cp-server` automatically reads the project's `config/server.env` and uses `config/server.json` when `--config` is omitted. Environment files are parsed as `KEY=VALUE` data, not executed as shell scripts. Variable expansion and command substitution are not supported. Existing process environment variables take precedence.

A literal `"auth": {"password": "..."}` also works, but using the local `server.env` reduces the risk of accidentally committing a password. Legacy `auth.token` / `NASS3CP_TOKEN` settings remain supported.

## 6. Validate before deployment

From the project root, validate the configuration and R2 access:

```bash
./bin/nass3cp-server --check-config
./bin/nass3cp-server --check-s3
```

`--check-s3` writes only a few dozen bytes, reads them back for verification, and immediately deletes the object.

Successful output:

```text
S3 relay check passed (PUT, GET, DELETE)
```

Start the service in the foreground:

```bash
./bin/nass3cp-server
```

To keep it running after disconnecting SSH, use the Linux `nohup` command without systemd:

```bash
mkdir -p run
nohup ./bin/nass3cp-server >run/nass3cp.log 2>&1 &
echo $! >run/nass3cp.pid
```

Inspect the process and logs:

```bash
ps -p "$(cat run/nass3cp.pid)" -f
tail -f run/nass3cp.log
```

After verifying that `ps` shows this project's `bin/nass3cp-server`, stop it gracefully. The service handles `SIGTERM` and closes the listening port:

```bash
kill "$(cat run/nass3cp.pid)"
```

The client machine can also use a complete project directory without installation. Add `--no-tls` when testing a small file over an encrypted overlay network. The password prompt does not echo input:

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 --port 9443 \
  ./small-test.bin nas:/volume1/share/small-test.bin
NAS password:
```

Replace `10.10.10.2` with the NAS overlay IP. Interactive Windows clients can add `--remember-password` on the first successful authentication to save the password in Windows Credential Manager for the current user. Later connections to the same endpoint and security mode read it automatically. For unattended use, specify `--password-file`, or copy `config/client.env.example` to `config/client.env` and set `NASS3CP_PASSWORD`.

In Windows PowerShell, use the `.cmd` launcher, which locates Python 3.8+ automatically:

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 --port 9443 `
  nas:/volume1/share/small-test.bin .
```

If Python is not on `PATH`, first set `$env:NASS3CP_PYTHON` to the full path of `python.exe`.

## Security and cost notes

- The R2 data channel always uses HTTPS. The program rejects HTTP endpoints and HTTP presigned URLs.
- Without application-layer TLS, the NAS control channel uses HTTP, including password transmission. Use this mode only when the underlying overlay network or tunnel already provides encryption and authentication. A private address alone is not a security boundary.
- A no-TLS server may listen on `0.0.0.0` or `::`, but doing so opens the control port on every interface. Restrict incoming connections with a firewall or preferably bind to the overlay IP.
- R2 automatically uses AES-256 encryption at rest, but Cloudflare can still see plaintext when decrypting on the server. Add client-side AEAD encryption if the provider must not be able to read content.
- Presigned URLs are short-lived Bearer credentials and should be treated like temporary passwords. They expire after 15 minutes by default.
- Within the free tier described above, occasional 1 GiB transfers usually cost $0 in R2 charges. Retries incur no R2 internet egress fees but still count as operations.
- Speed and reliability between mainland China and R2 depend on the ISP and cross-border links. Measure performance on both the NAS network and the networks commonly used by clients.

Official references: [create buckets](https://developers.cloudflare.com/r2/buckets/create-buckets/), [create R2 API tokens](https://developers.cloudflare.com/r2/api/tokens/), [presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/), [lifecycle rules](https://developers.cloudflare.com/r2/buckets/object-lifecycles/), [data security](https://developers.cloudflare.com/r2/reference/data-security/), and [R2 pricing](https://developers.cloudflare.com/r2/pricing/).
