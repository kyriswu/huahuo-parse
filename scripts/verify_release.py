#!/usr/bin/env python3
"""Verify an anonymous Docker Hub pull and the remote linux/amd64 manifest."""
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from urllib3.util.retry import Retry


def imagetools(arguments, env):
    for attempt in range(4):
        try:
            result = subprocess.run(["docker", "buildx", "imagetools", "inspect", *arguments], env=env,
                                    text=True, capture_output=True, timeout=90)
            if result.returncode == 0:
                return result
        except subprocess.TimeoutExpired:
            pass
        if attempt < 3:
            time.sleep(2 ** attempt)
    raise RuntimeError("Anonymous docker buildx imagetools inspect failed after four attempts; check Docker Hub network access")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--output", default="var/release/verification.json")
    parser.add_argument("--build-metadata", help="Require the remote digest to match this buildx metadata file")
    args = parser.parse_args()
    repository, tag = args.image.rsplit(":", 1)
    repository = repository.removeprefix("docker.io/")
    namespace, name = repository.split("/", 1)
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=Retry(total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
    info = session.get(f"https://hub.docker.com/v2/repositories/{namespace}/{name}/", timeout=60)
    info.raise_for_status()
    assert info.json()["is_private"] is False, "Docker Hub repository is private"
    token = session.get("https://auth.docker.io/token", params={"service": "registry.docker.io", "scope": f"repository:{repository}:pull"}, timeout=60)
    token.raise_for_status()
    manifest = session.get(f"https://registry-1.docker.io/v2/{repository}/manifests/{tag}",
                           headers={"Authorization": "Bearer " + token.json()["token"], "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"}, timeout=60)
    manifest.raise_for_status()
    digest = manifest.headers["Docker-Content-Digest"]
    assert "sha256:" + hashlib.sha256(manifest.content).hexdigest() == digest, "remote manifest content digest differs"
    if args.build_metadata:
        built = json.loads(Path(args.build_metadata).read_text())
        assert built["containerimage.digest"] == digest, "remote digest differs from the actual buildx output"
    # Check the CLI pull anonymously too, independent of expiring login tokens.
    existing_config = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
    plugin_dirs = json.loads((existing_config / "config.json").read_text()).get("cliPluginsExtraDirs", []) if (existing_config / "config.json").exists() else []
    with tempfile.TemporaryDirectory(prefix="huahuo-anonymous-inspect-") as temporary:
        config = {"auths": {}, "cliPluginsExtraDirs": [*plugin_dirs, str(existing_config / "cli-plugins")]}
        Path(temporary, "config.json").write_text(json.dumps(config))
        env = {**os.environ, "DOCKER_CONFIG": temporary, "DOCKER_CONTEXT": "default"}
        inspect = imagetools([args.image], env)
        assert digest in inspect.stdout, "registry digest and imagetools digest differ"
        raw = imagetools(["--raw", args.image], env)
    data = json.loads(raw.stdout)
    platforms = [item.get("platform", {}) for item in data.get("manifests", [])]
    if not platforms and "config" in data:
        config = session.get(f"https://registry-1.docker.io/v2/{repository}/blobs/{data['config']['digest']}", headers={"Authorization": "Bearer " + token.json()["token"]}, timeout=60)
        config.raise_for_status()
        platforms = [{"os": config.json()["os"], "architecture": config.json()["architecture"]}]
    assert any(p.get("os") == "linux" and p.get("architecture") == "amd64" for p in platforms), "remote linux/amd64 manifest missing"
    headers = {"Authorization": "Bearer " + token.json()["token"], "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"}
    runtime = data
    if "manifests" in data:
        descriptor = next(item for item in data["manifests"] if item.get("platform", {}).get("os") == "linux" and item.get("platform", {}).get("architecture") == "amd64")
        child = session.get(f"https://registry-1.docker.io/v2/{repository}/manifests/{descriptor['digest']}", headers=headers, timeout=60)
        child.raise_for_status()
        assert "sha256:" + hashlib.sha256(child.content).hexdigest() == descriptor["digest"], "runtime manifest digest differs"
        runtime = child.json()
    config = session.get(f"https://registry-1.docker.io/v2/{repository}/blobs/{runtime['config']['digest']}", headers=headers, timeout=60)
    config.raise_for_status()
    assert "sha256:" + hashlib.sha256(config.content).hexdigest() == runtime["config"]["digest"], "runtime config digest differs"
    assert (config.json()["os"], config.json()["architecture"]) == ("linux", "amd64"), "actual image config is not linux/amd64"
    output = {"image": "docker.io/" + repository + ":" + tag, "digest": digest, "public": True, "anonymous_pull": True, "platforms": platforms,
              "runtime_config": {"os": config.json()["os"], "architecture": config.json()["architecture"]}, "buildx_digest_verified": bool(args.build_metadata)}
    path = Path(args.output); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2) + "\n")
    path.with_suffix(".inspect.txt").write_text(inspect.stdout)
    print(json.dumps(output))


if __name__ == "__main__":
    main()
