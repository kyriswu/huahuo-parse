#!/usr/bin/env python3
"""Download only public, revision-pinned Apache-2.0 model artifacts."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

MODELS = {
    "PP-DocLayoutV3": ("PaddlePaddle/PP-DocLayoutV3", "241f8bdfc77a7c7bee915a5057aaee58c235a8d3"),
    "PaddleOCR-VL-1.6": ("PaddlePaddle/PaddleOCR-VL-1.6", "132c45f52c0a26ed5a6b2f6bd8e3985794cc27dd"),
}


def main():
    from huggingface_hub import snapshot_download
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="var/models")
    parser.add_argument("--license-file", default=str(Path(__file__).resolve().parent.parent / "licenses" / "Apache-2.0.txt"))
    args = parser.parse_args()
    root = Path(args.output)
    manifest = {}
    for name, (repo, revision) in MODELS.items():
        target = root / name
        snapshot_download(repo, revision=revision, local_dir=target, max_workers=4,
                          allow_patterns=["*.json", "*.model", "*.safetensors", "*.yml", "*.jinja", "*.pdiparams", "*.py", "LICENSE", "README.md"])
        shutil.rmtree(target / ".cache", ignore_errors=True)
        if not (target / "LICENSE").exists():
            # PP-DocLayoutV3's model card declares Apache-2.0; retain the license text.
            shutil.copyfile(args.license_file, target / "LICENSE")
        files = {}
        for file in sorted(target.iterdir()):
            if file.is_file():
                digest = hashlib.sha256()
                with file.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                files[file.name] = digest.hexdigest()
        manifest[name] = {"repository": repo, "revision": revision, "license": "Apache-2.0", "sha256": files}
    root.joinpath("manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Pinned model artifacts ready:", root)


if __name__ == "__main__":
    main()
