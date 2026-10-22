"""Fixed messages only: do not expose targets, headers, filenames or transport errors."""


class ProtocolError(Exception):
    def __init__(self):
        super().__init__('Gateway protocol rejected request')


class AuthenticationError(ProtocolError):
    pass


class ReplayUnavailable(AuthenticationError):
    pass


class AdmissionClosed(ProtocolError):
    pass
