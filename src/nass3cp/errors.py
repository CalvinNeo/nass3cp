class Nass3cpError(Exception):
    """Base exception for expected nass3cp failures."""


class ConfigError(Nass3cpError):
    """The configuration is invalid."""


class ProtocolError(Nass3cpError):
    """The peer returned an invalid response."""


class S3Error(Nass3cpError):
    """An S3-compatible endpoint request failed."""

