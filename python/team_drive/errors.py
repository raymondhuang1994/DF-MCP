"""Path-free public errors shared by the storage boundary and protocol engine."""
class DriveError(Exception):
    def __init__(self, code, message, retryable=False,
                 next_action="Check the request and configuration."):
        super().__init__(message)
        self.code, self.message = code, message
        self.retryable, self.next_action = retryable, next_action


def is_runtime_error(error):
    """Shared storage/runtime failures cannot be attributed to a source file."""
    code = error.code if isinstance(error, DriveError) else error
    return isinstance(code, str) and code.startswith(("STATE_", "UNSAFE_STATE", "RUNTIME_"))


def is_transient_source_error(error):
    """These failures prove neither a source change nor permission revocation."""
    code = error.code if isinstance(error, DriveError) else error
    return code in {"SHARE_TIMEOUT", "GRAPH_TIMEOUT", "GRAPH_THROTTLED", "GRAPH_UNAVAILABLE"}
