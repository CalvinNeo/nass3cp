import hashlib
import hmac
import time
from datetime import datetime, timezone
from http.client import HTTPException
from typing import BinaryIO, Dict, List, Mapping, NamedTuple, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request

from .config import S3Config
from .errors import S3Error
from .net import secure_opener


class PresignedRequest(NamedTuple):
    url: str
    headers: Dict[str, str]


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _canonical_query(parameters: Mapping[str, str]) -> str:
    encoded = []
    for name, value in parameters.items():
        encoded.append(
            (
                quote(str(name), safe="-_.~"),
                quote(str(value), safe="-_.~"),
            )
        )
    encoded.sort()
    return "&".join("%s=%s" % pair for pair in encoded)


def _normalize_header(value: str) -> str:
    return " ".join(value.strip().split())


class S3Relay:
    """Small, dependency-free S3 SigV4 client and URL signer.

    All data-plane calls use short-lived presigned HTTPS URLs. This works with
    Amazon S3 and S3-compatible endpoints such as Cloudflare R2 and Alibaba
    Cloud OSS.
    """

    def __init__(self, config: S3Config):
        self.config = config
        self._endpoint = urlsplit(config.endpoint)
        self._opener = secure_opener()

    def object_key(self, transfer_id: str, index: int) -> str:
        return "%s/%s/%08d" % (self.config.prefix, transfer_id, index)

    def _target(self, key: str) -> tuple:
        endpoint_path = self._endpoint.path.rstrip("/")
        if self.config.addressing_style == "virtual":
            hostname = self._endpoint.hostname
            if not hostname:
                raise S3Error("S3 endpoint has no hostname")
            host = "%s.%s" % (self.config.bucket, hostname)
            if self._endpoint.port is not None:
                host = "%s:%d" % (host, self._endpoint.port)
            path = "%s/%s" % (endpoint_path, key)
        else:
            host = self._endpoint.netloc
            path = "%s/%s/%s" % (endpoint_path, self.config.bucket, key)
        canonical_uri = quote(path or "/", safe="/-_.~")
        return host, canonical_uri

    def presign(
        self,
        method: str,
        key: str,
        now: Optional[datetime] = None,
        expires: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> PresignedRequest:
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        scope = "%s/%s/s3/aws4_request" % (date_stamp, self.config.region)
        host, canonical_uri = self._target(key)

        headers: Dict[str, str] = {"host": host}
        if extra_headers:
            for name, value in extra_headers.items():
                lowered = name.strip().lower()
                if lowered == "host":
                    raise S3Error("host cannot be overridden while presigning")
                headers[lowered] = _normalize_header(value)
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(
            "%s:%s\n" % (name, _normalize_header(headers[name]))
            for name in sorted(headers)
        )

        params = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": "%s/%s" % (self.config.access_key_id, scope),
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires or self.config.url_ttl_seconds),
            "X-Amz-SignedHeaders": signed_headers,
        }
        if self.config.presign_unsigned_payload:
            params["X-Amz-Content-Sha256"] = "UNSIGNED-PAYLOAD"
        if self.config.session_token:
            params["X-Amz-Security-Token"] = self.config.session_token
        query = _canonical_query(params)
        canonical_request = "\n".join(
            [
                method.upper(),
                canonical_uri,
                query,
                canonical_headers,
                signed_headers,
                "UNSIGNED-PAYLOAD",
            ]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                _sha256_hex(canonical_request.encode("utf-8")),
            ]
        )
        date_key = _hmac(("AWS4" + self.config.secret_access_key).encode("utf-8"), date_stamp)
        region_key = _hmac(date_key, self.config.region)
        service_key = _hmac(region_key, "s3")
        signing_key = _hmac(service_key, "aws4_request")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        final_query = "%s&X-Amz-Signature=%s" % (query, signature)
        url = urlunsplit(
            (
                self._endpoint.scheme,
                host,
                canonical_uri,
                final_query,
                "",
            )
        )
        returned_headers = {name: value for name, value in headers.items() if name != "host"}
        return PresignedRequest(url=url, headers=returned_headers)

    def presign_chunk(
        self,
        method: str,
        transfer_id: str,
        index: int,
        content_length: Optional[int] = None,
    ) -> PresignedRequest:
        headers = None
        if method.upper() == "PUT":
            if content_length is None or content_length < 0:
                raise S3Error("content length is required when presigning a PUT")
            headers = dict(self.config.put_headers)
            headers["content-length"] = str(content_length)
            headers["content-type"] = "application/octet-stream"
        return self.presign(method, self.object_key(transfer_id, index), extra_headers=headers)

    def request(
        self,
        method: str,
        transfer_id: str,
        index: int,
        data: Optional[bytes] = None,
        timeout: int = 300,
    ) -> BinaryIO:
        signed = self.presign_chunk(
            method,
            transfer_id,
            index,
            content_length=len(data) if method.upper() == "PUT" and data is not None else None,
        )
        headers = dict(signed.headers)
        request = Request(signed.url, data=data, headers=headers, method=method.upper())
        try:
            return self._opener.open(request, timeout=timeout)
        except HTTPError as exc:
            try:
                detail = exc.read(4096).decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            message = "S3 %s failed with HTTP %d" % (method.upper(), exc.code)
            if detail:
                message += ": " + detail
            raise S3Error(message) from exc
        except (URLError, OSError, HTTPException) as exc:
            raise S3Error("S3 %s failed: %s" % (method.upper(), exc.reason if isinstance(exc, URLError) else exc)) from exc

    def put_chunk(self, transfer_id: str, index: int, data: bytes) -> None:
        response = self.request("PUT", transfer_id, index, data=data)
        try:
            response.read()
        finally:
            response.close()

    def get_chunk(self, transfer_id: str, index: int) -> BinaryIO:
        return self.request("GET", transfer_id, index)

    def delete_chunk(self, transfer_id: str, index: int) -> None:
        response = self.request("DELETE", transfer_id, index)
        try:
            response.read()
        finally:
            response.close()

    def cleanup(self, transfer_id: str, chunks: int) -> List[str]:
        errors: List[str] = []
        for index in range(chunks):
            try:
                self.delete_chunk(transfer_id, index)
            except S3Error as exc:
                errors.append(str(exc))
            if index and index % 100 == 0:
                time.sleep(0.01)
        return errors
