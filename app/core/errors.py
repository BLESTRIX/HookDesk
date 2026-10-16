class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


class PermanentFailure(Exception):
    def __init__(self, code: str, summary: str):
        self.code = code
        self.summary = summary
        super().__init__(summary)


class TemporaryFailure(Exception):
    pass


class LostLease(Exception):
    pass
