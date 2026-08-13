import logging
import sys

_REDACTED_FIELDS = {"client_secret", "access_token", "redis_password", "authorization"}


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for field in _REDACTED_FIELDS:
            if field in record.getMessage().lower():
                record.msg = "[redacted: log message contained a sensitive field name]"
                record.args = ()
                break
        return True


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
        )
    )
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler]
