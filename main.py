import logging
import os
import sys


def _setup_logging():
    """Configure root logging for the application.

    Logs to both stderr and a rotating-style file in the user's home dir.
    Level can be overridden with the DOCREADER_LOGLEVEL env var.
    """
    level_name = os.environ.get("DOCREADER_LOGLEVEL", "DEBUG").upper()
    level = getattr(logging, level_name, logging.DEBUG)

    log_path = os.path.join(os.path.expanduser("~"), "documentreader.log")
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError:
        pass

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(threadName)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger(__name__).info("Logging initialized at level %s -> %s", level_name, log_path)


_setup_logging()

from src.app import DocumentReaderApp


def main():
    app = DocumentReaderApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
