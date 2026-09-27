# Repository Guide for Agents

## Language policy

- Maintain `README.md` in English and `README.zh-CN.md` in Simplified Chinese, with reciprocal language links.
- Keep both README versions aligned when changing behavior, configuration, commands, examples, or limitations.
- Write all other documentation in English, including this guide and files under `docs/`.
- Write code comments, docstrings, and comments in configuration files and examples in English.
- Preserve non-English literals when they are functional data, such as Unicode filenames, search patterns, or test fixtures. Do not translate these values merely to satisfy the documentation policy.
- Use the user's preferred language in conversation; the English requirement applies to repository comments and documentation.

## Project overview

`nass3cp` copies regular files between a client and a NAS through a temporary S3-compatible relay. The NAS API handles authentication and transfer coordination; file data travels through object storage. The project also supports recursive directory copies, directory browsing, and filename searches.

- Support Python 3.8 and later on Windows, Linux, and macOS.
- Keep runtime code limited to the Python standard library; `pyproject.toml` declares no runtime dependencies.
- Preserve compatibility with Cloudflare R2 and Alibaba Cloud OSS configurations.

## Repository layout

| Path | Purpose |
|---|---|
| `src/nass3cp/cli.py` | Command-line parsing, authentication setup, and command dispatch |
| `src/nass3cp/client.py` | NAS control API client and file transfers |
| `src/nass3cp/server.py` | NAS HTTP service, path validation, and transfer lifecycle |
| `src/nass3cp/s3.py` | S3 requests and SigV4 URL signing |
| `src/nass3cp/config.py` | Server configuration and environment-file loading |
| `src/nass3cp/credentials.py` | Windows Credential Manager integration |
| `src/nass3cp/recursive.py`, `compression.py` | Directory-copy planning and optional gzip compression |
| `src/nass3cp/browse.py`, `browse.html`, `browse.css`, `browse.js` | Local browser service and UI |
| `src/nass3cp/downloads.py`, `downloads.js` | Browser download queue and client UI |
| `src/nass3cp/search.py` | Bounded filename searches in a separate worker process |
| `tests/` | Standard-library `unittest` tests |
| `bin/` | Launchers that run directly from the source tree |
| `config/`, `examples/` | Project defaults and provider/deployment examples |
| `docs/cloudflare-r2.md` | Detailed Cloudflare R2 setup instructions |

## Development and validation

Run commands from the repository root. Installation is optional: the launchers load `src/` directly.

Run the test suite on Linux or macOS:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Run the test suite in Windows PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

Use `python bin/nass3cp --help` and `python bin/nass3cp-server --help` to inspect available options. On Windows, `bin\nass3cp.cmd` is the client launcher and supports `NASS3CP_PYTHON` when Python is not on `PATH`.

For behavior changes, run the relevant tests and add coverage for meaningful regressions. For documentation-only changes, check local links, code examples, README parity, and the language policy; a full runtime test run is not required.

`--check-config` validates a prepared server configuration. `--check-s3` makes real PUT, GET, and DELETE requests against the configured bucket; it is an integration check, not an offline test.

## Implementation constraints

- Inspect the working tree before editing and preserve unrelated changes and local artifacts.
- Follow the existing code style and keep syntax and standard-library APIs compatible with Python 3.8.
- Keep NAS paths within `allowed_roots` and retain path, symlink, and regular-file validation.
- Keep S3 endpoints and presigned URLs on HTTPS. HTTP control traffic requires explicit `--no-tls` and an encrypted, authenticated network or tunnel.
- Keep S3 secret keys on the NAS. Do not expose NAS passwords to browser code or log passwords, credentials, or presigned URLs.
- Preserve SHA-256 verification, temporary-file handling, atomic destination replacement, and transfer cleanup/resume behavior.
- Preserve bounded concurrency and memory use, cancellation behavior, and search resource limits.
- Preserve Unicode path support and Windows-specific path and credential behavior.
- Never commit real credentials, local environment files, TLS private keys, or runtime state. Use placeholders in examples and keep local secrets in the ignored configuration files.
- Update both READMEs and any affected English documentation when user-visible behavior changes.
