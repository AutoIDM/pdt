"""What the service tests share: a command key, signed tokens, and a local fake of AutoIDM."""

import io
import json
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat)

from pdt import service_commands, snapshots
from pdt.utils import autoidm

CUSTOMER = "cus_7a1c09e4b2"
INSTALLATION = "ins_4f8a1c2e9b"
KID = "key-1"
SIGNING_KEY = generate_private_key(SECP256R1())
PRIVATE_PEM = SIGNING_KEY.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
PUBLIC_PEM = SIGNING_KEY.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
KEYS = [{"kid": KID, "pem": PUBLIC_PEM}]
COMMAND = "cmd_20261103T120000Z_0123abcd"
AUTHOR_KEY = Ed25519PrivateKey.generate()


def claims(operation="list", args=None, command_id=COMMAND, **changes) -> dict:
    now = int(time.time())
    found = {"aud": INSTALLATION, "iss": service_commands.ISSUER, "sub": command_id,
             "customer_id": CUSTOMER, "operation": operation,
             "args": {"project": "example"} if args is None else args,
             "requested_by": "support@example.com", "iat": now, "exp": now + 600,
             "limit_seconds": 300}
    return {**found, **changes}


def publication(identity, parent=None, project="example", purpose="publish", key=None, ref=None,
                label=None) -> dict:
    """A publication of a snapshot, signed as `/v1/snapshots/start` takes it."""
    key = key or AUTHOR_KEY
    return {"schema": 1, "installation_id": INSTALLATION, "public_key": autoidm.public_key(key),
            "ref": ref, "label": label, "parent": parent, "purpose": purpose,
            "signature": snapshots.sign_publication(key, CUSTOMER, project, identity, purpose, ref,
                                                    label, parent),
            "at": "2026-11-04T12:00:00Z"}


def sign(payload: dict, key=PRIVATE_PEM, kid=KID, alg="ES256") -> str:
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})


def token(operation="list", args=None, command_id=COMMAND, **changes) -> str:
    return sign(claims(operation, args, command_id, **changes))


def snapshot_of(files: dict[str, bytes]) -> tuple[str, dict, bytes]:
    """The ID, manifest, and zip of a snapshot that holds `files`."""
    entries = sorted(({"path": path, "sha256": snapshots.content_hash(data), "size": len(data)}
                      for path, data in files.items()), key=lambda entry: entry["path"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path, data in files.items():
            zipped.writestr(path, data)
    return snapshots.snapshot_id(entries), {"files": entries}, buffer.getvalue()


class FakePortal:
    """Answers each POST from `routes`, by path, and records it. GET /doorbell follows ETags,
    and GET /zip/<id> serves `zips`. POST /bucket takes a presigned upload."""

    def __init__(self):
        self.seen, self.routes, self.zips, self.uploads = [], {}, {}, []
        self.doorbell_status = None
        self.etag = '"one"'
        self.doorbell_gets = []
        self.ranges, self.zip_cut_after = [], []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self, status, body=b"", headers=None):
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/doorbell":
                    sent = self.headers.get("If-None-Match")
                    owner.doorbell_gets.append(sent)
                    if owner.doorbell_status is not None:
                        return self.answer(owner.doorbell_status)
                    if sent == owner.etag:
                        return self.answer(304)
                    return self.answer(200, b"{}", {"ETag": owner.etag})
                if self.path.startswith("/zip/"):
                    data = owner.zips.get(self.path[len("/zip/"):])
                    sent = self.headers.get("Range")
                    owner.ranges.append(sent)
                    if data is None:
                        return self.answer(500)
                    if sent:
                        return self.answer(206, data[int(sent[6:-1]):])
                    if owner.zip_cut_after:
                        cut = owner.zip_cut_after.pop(0)
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data[:cut])
                        self.close_connection = True
                        return None
                    return self.answer(200, data)
                return self.answer(404)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/bucket":
                    owner.uploads.append(raw)
                    return self.answer(204)
                body = json.loads(raw)
                owner.seen.append((self.path, body, self.headers.get("Authorization")))
                route = owner.routes.get(self.path)
                status, answer = route(body) if route else (200, {})
                return self.answer(status, json.dumps(answer).encode())

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def bodies(self, path: str) -> list[dict]:
        return [body for seen, body, _auth in self.seen if seen == path]

    def facts(self, command_id: str = COMMAND) -> list[str]:
        return [body["fact"] for body in self.bodies("/v1/commands/report")
                if body["command_id"] == command_id]

    def result(self, command_id: str = COMMAND) -> dict:
        return next(body for body in self.bodies("/v1/commands/report")
                    if body["command_id"] == command_id and body["fact"] == "result")

    def output(self, command_id: str = COMMAND) -> str:
        return "".join(body["text"] for body in self.bodies("/v1/commands/report")
                       if body["command_id"] == command_id and body["fact"] == "output")

    def serve_snapshot(self, identity: str, manifest: dict, data: bytes,
                       publications: list | None = None) -> None:
        self.zips[identity] = data
        self.routes["/v1/snapshots/get"] = lambda _body: (
            200, {"snapshot_id": identity, "url": f"{self.url}/zip/{identity}",
                  "manifest": manifest, "publications": publications or []})
        self.routes["/v1/snapshots/heads"] = lambda _body: (
            200, {"heads": [{"installation_id": INSTALLATION, "snapshot_id": identity,
                             "at": "2026-11-04T12:00:00Z"}]})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
