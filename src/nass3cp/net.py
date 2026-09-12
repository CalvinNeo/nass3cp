import ssl
from typing import Optional
from urllib.request import HTTPRedirectHandler, HTTPSHandler, OpenerDirector, build_opener


class NoRedirectHandler(HTTPRedirectHandler):
    """Never forward credentials or signed URLs through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def secure_opener(context: Optional[ssl.SSLContext] = None) -> OpenerDirector:
    if context is None:
        context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return build_opener(HTTPSHandler(context=context), NoRedirectHandler())

