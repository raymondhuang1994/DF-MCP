"""Path-free public errors shared by the storage boundary and protocol engine."""
class DriveError(Exception):
    def __init__(self, code, message, retryable=False,
                 next_action="Check the request and configuration."):
        super().__init__(message)
        self.code, self.message = code, message
        self.retryable, self.next_action = retryable, next_action
