"""How a command reaches `pdt service`: a WebSocket notice, and the safety read.

`Connection` holds one WebSocket connection on its own thread. It verifies nothing and reads no
frame content, because each frame means only that a check-in may find something. It pings each
PING_SECONDS, reconnects after no answer in PONG_SECONDS, sends a data message each
`ws_keepalive_seconds`, and replaces the connection at a random time from 1 h 45 min to 1 h 55 min.
After an unplanned close, it reconnects with a growing random wait, and it asks for the check-in
at a random time up to RECONNECT_SPREAD_SECONDS later, so an outage does not end in one burst.

`Schedule` holds each timer of the main loop, and it changes only on the main thread. The doorbell
read and the check-in always run, on the longer intervals when a connection is open. A doorbell
that changed with no frame since the previous read means that a proxy passes the upgrade and no
frames, so the short intervals apply for SILENT_SECONDS and the connection stays.

The two share only `Connection.frames`, `Connection.connected`, and `Connection.wanted_at`, which
one lock guards.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from pdt.utils import autoidm

PING_SECONDS = 60
PONG_SECONDS = 30
OPEN_TIMEOUT_SECONDS = 30
REPLACE_FIRST_SECONDS = 105 * 60
REPLACE_LAST_SECONDS = 115 * 60
REPLACE_RETRY_SECONDS = 60
RECONNECT_SPREAD_SECONDS = 60
BACKOFF_FIRST_SECONDS = 1.0
BACKOFF_MOST_SECONDS = 300.0
SILENT_SECONDS = 3600
RECEIVE_WAIT_SECONDS = 1.0
KEEPALIVE_MESSAGE = '{"action": "keepalive"}'


class Connection:

    def __init__(self, registration: autoidm.Registration, say: Callable[[str], None]) -> None:
        self.registration = registration
        self.say = say
        self.url: str | None = None
        self.keepalive_seconds = 540
        self.lock = threading.Lock()
        self.frames = 0
        self.connected = False
        self.wanted_at: float | None = None
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.said = ""

    def start(self, url: str) -> None:
        with self.lock:
            self.url = url
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True, name="pdt-websocket")
            self.thread.start()

    def stop(self) -> None:
        self.stopping.set()

    def read(self) -> tuple[bool, int, float | None]:
        with self.lock:
            return self.connected, self.frames, self.wanted_at

    def clear_wanted(self) -> None:
        with self.lock:
            self.wanted_at = None

    def set_connected(self, value: bool) -> None:
        with self.lock:
            self.connected = value

    def want_checkin(self, delay: float) -> None:
        with self.lock:
            self.wanted_at = time.time() + delay

    def frame(self) -> None:
        with self.lock:
            self.frames += 1

    def open(self):
        from websockets.sync.client import connect
        token = autoidm.websocket_token(self.registration)
        return connect(self.url, additional_headers={"Authorization": f"Bearer {token}"},
                       open_timeout=OPEN_TIMEOUT_SECONDS, ping_interval=PING_SECONDS,
                       ping_timeout=PONG_SECONDS, proxy=True)

    def say_once(self, message: str) -> None:
        if message != self.said:
            self.said = message
            self.say(message)

    def run(self) -> None:
        wait = BACKOFF_FIRST_SECONDS
        first = True
        while not self.stopping.is_set():
            try:
                connection = self.open()
            except Exception as e:
                self.set_connected(False)
                self.say_once(f"pdt could not open the connection to AutoIDM: {e}")
                self.stopping.wait(random.uniform(wait / 2, wait))
                wait = min(wait * 2, BACKOFF_MOST_SECONDS)
                continue
            self.say_once("The connection to AutoIDM is open.")
            wait = BACKOFF_FIRST_SECONDS
            self.set_connected(True)
            self.want_checkin(0 if first else random.uniform(0, RECONNECT_SPREAD_SECONDS))
            first = False
            self.serve(connection)
            self.set_connected(False)

    def serve(self, connection) -> None:
        from websockets.exceptions import ConnectionClosed
        replace_at = time.monotonic() + random.uniform(REPLACE_FIRST_SECONDS, REPLACE_LAST_SECONDS)
        keepalive_at = time.monotonic() + self.keepalive_seconds
        try:
            while not self.stopping.is_set():
                try:
                    connection.recv(timeout=RECEIVE_WAIT_SECONDS)
                    self.frame()
                except TimeoutError:
                    pass
                now = time.monotonic()
                if self.keepalive_seconds > 0 and now >= keepalive_at:
                    connection.send(KEEPALIVE_MESSAGE)
                    keepalive_at = now + self.keepalive_seconds
                if now >= replace_at:
                    try:
                        replacement = self.open()
                    except Exception as e:
                        self.say_once(f"pdt could not replace the connection to AutoIDM: {e}")
                        replace_at = now + REPLACE_RETRY_SECONDS
                        continue
                    self.want_checkin(0)
                    connection.close()
                    connection = replacement
                    replace_at = now + random.uniform(REPLACE_FIRST_SECONDS, REPLACE_LAST_SECONDS)
        except (ConnectionClosed, OSError) as e:
            self.say_once(f"The connection to AutoIDM closed: {e}")
            return
        connection.close()


@dataclass
class Schedule:
    poll_seconds: int = 30
    checkin_seconds: int = 900
    ws_poll_seconds: int = 300
    ws_checkin_seconds: int = 3600
    ws_keepalive_seconds: int = 540
    min_checkin_seconds: int = 10
    last_read: float = 0.0
    last_checkin: float = 0.0
    wanted: bool = True
    silent_until: float = 0.0
    frames_at_read: int = 0
    frames_handled: int = 0
    failures: int = 0
    retry_at: float = 0.0

    ANSWER_FIELDS = ("poll_seconds", "checkin_seconds", "ws_poll_seconds", "ws_checkin_seconds",
                     "ws_keepalive_seconds", "min_checkin_seconds")

    def apply(self, answer: dict) -> None:
        for name in self.ANSWER_FIELDS:
            value = answer.get(name)
            if type(value) is int and value >= 0:
                setattr(self, name, value)

    def usable(self, connected: bool, now: float) -> bool:
        return connected and now >= self.silent_until

    def poll_interval(self, connected: bool, now: float) -> int:
        return self.ws_poll_seconds if self.usable(connected, now) else self.poll_seconds

    def checkin_interval(self, connected: bool, now: float) -> int:
        return self.ws_checkin_seconds if self.usable(connected, now) else self.checkin_seconds

    def frames_seen(self, frames: int) -> None:
        if frames != self.frames_handled:
            self.frames_handled = frames
            self.wanted = True

    def doorbell_due(self, connected: bool, now: float) -> bool:
        return now >= self.last_read + self.poll_interval(connected, now)

    def doorbell_read(self, status: int, frames: int, connected: bool, known_etag: bool,
                      now: float) -> None:
        """A 200 or a 403 asks for a check-in. A 200 with no frame since the last read is the sign
        of a proxy that passes no frames."""
        if status == 200 or status == 403:
            self.wanted = True
        if status == 200 and known_etag and connected and frames == self.frames_at_read:
            self.silent_until = now + SILENT_SECONDS
        self.frames_at_read = frames
        self.last_read = now

    def checkin_due(self, connected: bool, now: float) -> bool:
        if now < self.retry_at or now < self.last_checkin + self.min_checkin_seconds:
            return False
        return self.wanted or now - self.last_checkin >= self.checkin_interval(connected, now)

    def checkin_done(self, now: float, more: bool) -> None:
        self.last_checkin = now
        self.wanted = more
        self.failures = 0
        self.retry_at = 0.0

    def checkin_failed(self, now: float) -> None:
        self.failures += 1
        most = min(BACKOFF_FIRST_SECONDS * 2 ** self.failures, BACKOFF_MOST_SECONDS)
        self.retry_at = now + random.uniform(most / 2, most)
