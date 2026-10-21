"""Preserve wire targets while matching Go URL.Path and Starlette's decoded path."""
from dataclasses import dataclass
import re
from urllib.parse import unquote

from .errors import ProtocolError


@dataclass(frozen=True)
class RequestTarget:
    method: str
    raw_path: str
    raw_query: str = ''

    def __post_init__(self):
        method = self.method.upper()
        if method not in {'GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE'}:
            raise ProtocolError()
        object.__setattr__(self, 'method', method)
        # Reject ambiguous routing encodings rather than silently normalize them.
        # All current worker route names/identifiers fit this conservative subset.
        if (len(self.raw_path) + len(self.raw_query) > 8192
                or not re.fullmatch(r'/[A-Za-z0-9_./%~\-]*', self.raw_path)
                or '//' in self.raw_path
                or re.search(r'%(?![0-9A-Fa-f]{2})', self.raw_path + self.raw_query)
                or any(ord(c) < 33 or ord(c) > 126 or c == '#' for c in self.raw_query)):
            raise ProtocolError()
        try:
            decoded = unquote(self.raw_path, errors='strict')
        except UnicodeDecodeError:
            raise ProtocolError() from None
        if (not re.fullmatch(r'/[A-Za-z0-9_./~\-]*', decoded)
                or any(part in {'.', '..'} for part in decoded.split('/'))
                or decoded.count('/') != self.raw_path.count('/')):
            raise ProtocolError()

    @property
    def path(self) -> str:
        return unquote(self.raw_path)

    @property
    def raw_target(self) -> str:
        return self.raw_path + ('?' + self.raw_query if self.raw_query else '')

    def canonical(self, timestamp: str, nonce: str, body_hash: str = '') -> bytes:
        # This is precisely the existing worker format, NOT a body digest.
        fields = [self.method, self.path + ('?' + self.raw_query if self.raw_query else ''), timestamp, nonce]
        if body_hash:
            fields.append(body_hash)
        if any('\n' in item or '\r' in item for item in fields):
            raise ProtocolError()
        return '\n'.join(fields).encode('utf-8')
