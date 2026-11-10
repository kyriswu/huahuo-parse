#!/usr/bin/env python3
"""Resume an exact buildx OCI image upload when a proxy rejects large PUTs.

Uses the existing Docker Hub CLI login, never prints credentials/upload URLs,
and obtains immutable content from an isolated Colima containerd image store.
Upload state lives only in the ignored release directory.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import requests


REGISTRY = "https://registry-1.docker.io"


def checked_digest(value):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Expected a sha256 content digest")
    return value


class Hub:
    def __init__(self, repository):
        self.repository = repository
        config_dir = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
        config = json.loads((config_dir / "config.json").read_text())
        entry = config.get("auths", {}).get("https://index.docker.io/v1/", {})
        if "auth" not in entry:
            raise RuntimeError("Docker Hub CLI login with an auth entry is required")
        self.credentials = tuple(base64.b64decode(entry["auth"]).decode().split(":", 1))
        self.session = requests.Session()
        # A macOS system HTTP proxy can reject PATCH uploads even when GET works.
        # Explicitly opt in below when that proxy is required on the build host.
        self.session.trust_env = False
        self.token = None
        self.expires = 0

    def request(self, method, url, **kwargs):
        target = urlsplit(url)
        if target.scheme != "https" or target.netloc != "registry-1.docker.io":
            raise RuntimeError("Refusing an unexpected registry upload host")
        for attempt in range(2):
            if time.monotonic() >= self.expires:
                for token_attempt in range(4):
                    try:
                        auth = self.session.get(
                            "https://auth.docker.io/token",
                            params={"service": "registry.docker.io", "scope": f"repository:{self.repository}:pull,push"},
                            auth=self.credentials, timeout=(20, 60),
                        )
                        break
                    except requests.RequestException:
                        if token_attempt == 3:
                            raise
                        time.sleep(token_attempt + 1)
                if auth.status_code != 200:
                    raise RuntimeError(f"Docker Hub token HTTP {auth.status_code}")
                data = auth.json()
                self.token = data["token"]
                self.expires = time.monotonic() + max(30, data.get("expires_in", 300) - 60)
            headers = dict(kwargs.get("headers", {}))
            headers["Authorization"] = "Bearer " + self.token
            response = self.session.request(method, url, **{**kwargs, "headers": headers},
                                            timeout=(120, 180), allow_redirects=False)
            if response.status_code != 401 or attempt:
                return response
            self.expires = 0
        raise RuntimeError("Docker Hub authentication failed")

    def endpoint(self, suffix):
        return f"{REGISTRY}/v2/{self.repository}/{suffix}"


def require(response, *statuses):
    if response.status_code not in statuses:
        # Exception messages from requests may contain signed registry queries.
        raise RuntimeError(f"Registry HTTP {response.status_code}")


def save_state(path, value):
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(value, stream)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Docker Hub namespace/repository:unique-tag")
    parser.add_argument("--digest", required=True, type=checked_digest)
    parser.add_argument("--colima-profile", required=True)
    parser.add_argument("--directory", default="var/release/oci")
    parser.add_argument("--chunk-mib", type=int, default=32)
    parser.add_argument("--system-proxy", action="store_true")
    parser.add_argument("--only-blob", type=checked_digest, help="Upload one layer without publishing manifests")
    args = parser.parse_args()
    repository, tag = args.image.removeprefix("docker.io/").rsplit(":", 1)
    if not re.fullmatch(r"[a-z0-9._-]+/[a-z0-9._-]+", repository):
        raise ValueError("Expected Docker Hub namespace/repository")
    if not re.fullmatch(r"[\w][\w.-]{0,127}", tag) or not 5 <= args.chunk_mib <= 128:
        raise ValueError("Invalid tag or chunk size")
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    hub = Hub(repository)
    hub.session.trust_env = args.system_proxy
    manifests = []
    blobs = {}

    def content(digest, suffix):
        checked_digest(digest)
        path = directory / (digest.split(":")[1] + suffix)
        if not path.exists():
            temporary = path.with_suffix(".exporting")
            print(f"Exporting {digest}", flush=True)
            with temporary.open("wb") as stream:
                subprocess.run(["colima", "--profile", args.colima_profile, "ssh", "--", "sudo", "ctr",
                                "--address", "/run/containerd/containerd.sock", "--namespace", "moby",
                                "content", "get", digest], stdout=stream, check=True)
            temporary.replace(path)
        with path.open("rb") as stream:
            actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise RuntimeError(f"Exported content hash mismatch: {digest}")
        return path

    def visit(digest):
        path = content(digest, ".json")
        data = json.loads(path.read_bytes())
        for item in data.get("manifests", []):
            visit(item["digest"])
        for item in ([data["config"]] if "config" in data else []) + data.get("layers", []):
            blobs[item["digest"]] = item
        manifests.append((digest, path, data["mediaType"]))

    visit(args.digest)
    if args.only_blob:
        blobs = {args.only_blob: blobs[args.only_blob]}
    for digest, description in blobs.items():
        check = hub.request("HEAD", hub.endpoint(f"blobs/{digest}"))
        require(check, 200, 404)
        if check.status_code == 200:
            print(f"Exists {digest}", flush=True)
            continue
        path = content(digest, ".blob")
        size = path.stat().st_size
        if size != description["size"]:
            raise RuntimeError("Blob size differs from immutable manifest")
        state_path = path.with_suffix(".upload.json")
        state = json.loads(state_path.read_text()) if state_path.exists() else None
        if state and state.get("repository") != repository:
            raise RuntimeError("Upload state belongs to a different repository")
        if state:
            response = hub.request("GET", state["url"])
            if response.status_code in (204, 202):
                state["url"] = urljoin(REGISTRY, response.headers["Location"])
                received = response.headers["Range"]
                state["offset"] = 0 if state["offset"] == 0 and received == "0-0" else int(received.rsplit("-", 1)[1]) + 1
            elif response.status_code == 404:
                state = None
            else:
                require(response, 204)
        if state is None:
            response = hub.request("POST", hub.endpoint("blobs/uploads/"), data=b"", headers={"Content-Length": "0"})
            require(response, 202)
            state = {"repository": repository, "url": urljoin(REGISTRY, response.headers["Location"]), "offset": 0}
        save_state(state_path, state)
        started = time.monotonic()
        failures = 0
        with path.open("rb") as stream:
            while state["offset"] < size:
                offset = state["offset"]
                stream.seek(offset)
                chunk = stream.read(args.chunk_mib * 1024 * 1024)
                try:
                    response = hub.request("PATCH", state["url"], data=chunk, headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"{offset}-{offset + len(chunk) - 1}",
                    })
                    require(response, 202)
                    state["url"] = urljoin(REGISTRY, response.headers["Location"])
                    state["offset"] = int(response.headers["Range"].rsplit("-", 1)[1]) + 1
                    if state["offset"] != offset + len(chunk):
                        raise RuntimeError("Registry acknowledged an unexpected byte range")
                    failures = 0
                except (requests.RequestException, RuntimeError) as error:
                    failures += 1
                    reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                    print(f"Retry {digest[:19]} offset={offset} cause={reason}", flush=True)
                    if failures > 10:
                        raise RuntimeError("Repeated chunk upload failures; rerun to resume") from None
                    time.sleep(min(30, failures * 3))
                    status = hub.request("GET", state["url"])
                    require(status, 204, 202)
                    state["url"] = urljoin(REGISTRY, status.headers["Location"])
                    received = status.headers["Range"]
                    state["offset"] = 0 if offset == 0 and received == "0-0" else int(received.rsplit("-", 1)[1]) + 1
                save_state(state_path, state)
                print(f"Uploaded {digest[:19]} {state['offset']}/{size} bytes elapsed={time.monotonic() - started:.1f}s", flush=True)
        final_url = state["url"] + ("&" if "?" in state["url"] else "?") + urlencode({"digest": digest})
        response = hub.request("PUT", final_url, data=b"", headers={"Content-Length": "0"})
        require(response, 201)
        if response.headers.get("Docker-Content-Digest") != digest:
            raise RuntimeError("Committed blob digest mismatch")
        state_path.unlink()
        print(f"Committed {digest}", flush=True)

    if args.only_blob:
        return
    for digest, path, media_type in manifests:
        reference = tag if digest == args.digest else digest
        response = hub.request("PUT", hub.endpoint(f"manifests/{reference}"), data=path.read_bytes(),
                               headers={"Content-Type": media_type})
        require(response, 201)
        if response.headers.get("Docker-Content-Digest") != digest:
            raise RuntimeError("Committed manifest digest mismatch")
        print(f"Published {repository}:{reference} {digest}", flush=True)
    print(json.dumps({"image": "docker.io/" + args.image.removeprefix("docker.io/"), "digest": args.digest}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as error:
        raise SystemExit(f"Registry transport failure ({type(error).__name__}); rerun to resume") from None
