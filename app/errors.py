"""Business errors raised by services. main.py turns them into HTTP responses.

Keeping these framework-free means the GMP rules in services/ can be read and tested
without knowing anything about FastAPI.
"""


class EbrError(Exception):
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(EbrError):
    status_code = 404


class Forbidden(EbrError):      # role / permission problem
    status_code = 403


class Blocked(EbrError):        # a GMP rule stopped the action (wrong sequence, quarantine lot, ...)
    status_code = 409


class Invalid(EbrError):        # malformed input (missing reason, non-numeric value, ...)
    status_code = 422


class AuthFailed(EbrError):     # wrong user ID / password
    status_code = 401


class SessionInvalid(AuthFailed):  # not logged in / token expired -> the UI sends the user to the login page
    status_code = 401
