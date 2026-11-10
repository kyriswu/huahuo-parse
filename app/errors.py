class DownloadError(Exception):
    status_code = 502


class URLRejected(DownloadError):
    status_code = 400


class UnsupportedDocument(DownloadError):
    status_code = 415


class DownloadTooLarge(DownloadError):
    status_code = 413


class DownloadTimedOut(DownloadError):
    status_code = 504


class ModelUnavailable(DownloadError):
    status_code = 503
