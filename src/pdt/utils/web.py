"""One HTTP client for the apps and the CLI in this repo.

Every call to a web API goes through `Client`. It retries what can pass,
backs off between tries, and logs the whole response when it gives up.
An app never writes its own retry loop and never wraps a request in
try/except to print the error; the client does both.

The rules are the Meltano SDK's RESTStream rules:

- 429 and every 5xx are retriable. So are connection failures, timeouts,
  and a connection reset mid-response. Any other 4xx is fatal at once.
- A retriable failure waits 2, 4, 8, then 16 seconds (each plus up to a
  second of jitter) and gives up after 5 tries in all. A `Retry-After`
  header from the server replaces the computed wait, up to 5 minutes.
- Each retry logs a warning naming the try, the wait, and the status or
  error. Giving up logs an error with the method, the url, the status,
  and the first 2000 characters of the body. The url goes in the log as
  sent, so put a key in a header, never in the query string.
- Connecting waits 10 seconds; reading waits 60. Pass `timeout=` to a
  call, or to `Client`, to change that.

Usage in an app:

    from pdt.utils.web import Client

    ynab = Client("https://api.ynab.com/v1", exit_code=EXIT_YNAB,
                  headers={"Authorization": f"Bearer {key}"})
    budgets = ynab.get("/budgets").json()["data"]["budgets"]
    ynab.patch(f"/budgets/{budget_id}/transactions", json={"transactions": updates})

With `exit_code`, a request that fails for good logs the response and
exits the app with that code, the same way `pdt.utils.log.die` does.
Without it, the failed request raises `FatalAPIError` or
`RetriableAPIError` (both carry `.response`) or the httpx transport
error, after logging it.

`Client` is an `httpx.Client`, so `params=`, `json=`, `content=`,
`headers=`, `timeout=`, and the verb methods all work as in httpx.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Callable

import backoff
import httpx

from pdt import __version__
from pdt.utils.log import die, log

EXTRA_RETRY_STATUSES = (HTTPStatus.TOO_MANY_REQUESTS,)
MAX_TRIES = 5
BACKOFF_FACTOR = 2
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
BODY_LIMIT = 2000
MAX_RETRY_AFTER = 300.0
USER_AGENT = f"pdt/{__version__}"

Logger = Callable[..., None]


class APIError(Exception):
    """A request the client gave up on. `response` is the last one seen."""

    def __init__(self, message: str, response: httpx.Response | None = None):
        super().__init__(message)
        self.response = response


class FatalAPIError(APIError):
    """The server rejected the request; sending it again cannot help."""


class RetriableAPIError(APIError):
    """The server is busy or failing; sending the request again can work."""


# httpx groups its transport errors: TimeoutException covers connect,
# read, write, and pool timeouts; NetworkError covers a refused, reset, or
# dropped connection; ProtocolError covers a server that stops mid-reply.
# UnsupportedProtocol and InvalidURL are the caller's mistake and are not
# in this list, so they fail at once.
RETRIABLE_ERRORS = (
    RetriableAPIError,
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.ProtocolError,
    ConnectionResetError,
)


def response_error_message(response: httpx.Response) -> str:
    error_type = ("Client" if HTTPStatus.BAD_REQUEST <= response.status_code
                  < HTTPStatus.INTERNAL_SERVER_ERROR else "Server")
    return (f"{response.status_code} {error_type} Error: "
            f"{response.reason_phrase} for url: {response.url}")


def validate_response(response: httpx.Response) -> None:
    """Raise RetriableAPIError or FatalAPIError for an error status."""
    if (response.status_code in EXTRA_RETRY_STATUSES
            or response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR):
        raise RetriableAPIError(response_error_message(response), response)
    if HTTPStatus.BAD_REQUEST <= response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
        raise FatalAPIError(response_error_message(response), response)


def body_excerpt(response: httpx.Response) -> str:
    text = response.text
    if len(text) > BODY_LIMIT:
        return text[:BODY_LIMIT] + f"... ({len(text)} characters in all)"
    return text


def retry_after_seconds(error: BaseException) -> float | None:
    """The server's Retry-After as seconds, or None when it sent none."""
    response = getattr(error, "response", None)
    if response is None:
        return None
    value = response.headers.get("Retry-After", "").strip()
    if value == "":
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def retry_wait():
    """Exponential wait, replaced by Retry-After when the server sent one.

    backoff primes the generator, then sends it the exception before each
    wait, so the wait can read the failed response.
    """
    expo = backoff.expo(factor=BACKOFF_FACTOR)
    next(expo)  # backoff's own generators also start with a priming yield
    error = yield
    while True:
        wait = next(expo)
        retry_after = retry_after_seconds(error)
        error = yield wait if retry_after is None else retry_after


class Client(httpx.Client):
    """An httpx.Client that retries, backs off, and logs failures.

    `exit_code`: exit the process with this code after logging a failure,
    instead of raising. `log`: where retry warnings and the failure go;
    the default is `pdt.utils.log.log`, the CLI passes its own.
    """

    def __init__(self, base_url: str = "", *, exit_code: int | None = None,
                 max_tries: int = MAX_TRIES, log: Logger = log,
                 timeout=None, headers=None, **kwargs):
        if timeout is None:
            timeout = httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)
        merged = {"User-Agent": USER_AGENT}
        merged.update(headers or {})
        super().__init__(base_url=base_url, timeout=timeout, headers=merged, **kwargs)
        self.exit_code = exit_code
        self.max_tries = max_tries
        self.log = log
        self._send_with_retries = backoff.on_exception(
            retry_wait,
            RETRIABLE_ERRORS,
            max_tries=max_tries,
            jitter=backoff.random_jitter,
            on_backoff=self._on_backoff,
            logger=None,
        )(self._send_once)

    def request(self, method: str, url, **kwargs) -> httpx.Response:
        try:
            return self._send_with_retries(method, url, kwargs)
        except (APIError, *RETRIABLE_ERRORS) as error:
            self._report(method, url, kwargs, error)
            if self.exit_code is None:
                raise
            sys.exit(self.exit_code)

    def _send_once(self, method: str, url, kwargs: dict) -> httpx.Response:
        response = httpx.Client.request(self, method, url, **kwargs)
        validate_response(response)
        return response

    def _on_backoff(self, details) -> None:
        error = details["exception"]
        method, url, kwargs = details["args"]
        fields = {"method": method.upper(), "url": self._full_url(method, url, kwargs),
                  "try": details["tries"], "of": self.max_tries,
                  "wait": round(details["wait"], 1)}
        response = getattr(error, "response", None)
        if response is not None:
            fields["status"] = response.status_code
        else:
            fields["error"] = f"{type(error).__name__}: {error}"
        self.log("warning", "http request failed; retrying", **fields)

    def _report(self, method: str, url, kwargs: dict, error: BaseException) -> None:
        response = getattr(error, "response", None)
        fields = {"method": method.upper(), "url": self._full_url(method, url, kwargs)}
        if response is not None:
            fields["status"] = response.status_code
            body = body_excerpt(response)
            if body != "":
                fields["body"] = body
        else:
            fields["error"] = f"{type(error).__name__}: {error}"
        self.log("error", "http request failed", **fields)

    def _full_url(self, method: str, url, kwargs: dict) -> str:
        return str(self.build_request(method, url, params=kwargs.get("params")).url)


def http_json(url: str, exit_code: int, method: str = "GET",
              headers: dict | None = None, data: bytes | None = None) -> dict:
    """Request url and return the decoded json body; die with exit_code on failure.

    A one-call convenience over `Client`. An app that makes more than one
    call to the same API should hold a `Client` instead.
    """
    with Client(exit_code=exit_code) as client:
        response = client.request(method, url, headers=headers, content=data)
    if response.content == b"":
        return {}
    try:
        return response.json()
    except json.JSONDecodeError:
        die(exit_code, "http response was not json", url=url)
