"""Typed pipeline errors. `code` is stored on the run and shown in the UI."""


class MappingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class UploadError(Exception):
    """Raised for user-facing upload/parse problems (HTTP 400)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
