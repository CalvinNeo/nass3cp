class Nass3cpError(Exception):
    """Base exception for expected nass3cp failures."""


class ConfigError(Nass3cpError):
    """The configuration is invalid."""


class ProtocolError(Nass3cpError):
    """The peer returned an invalid response."""


class AuthenticationError(ProtocolError):
    """The NAS rejected the supplied password."""


class DownloadCancelled(Nass3cpError):
    """A caller cancelled an in-progress download."""


class S3Error(Nass3cpError):
    """An S3-compatible endpoint request failed."""
