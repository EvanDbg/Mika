import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import quote

REPO = "EvanDbg/Mika"
ROOT = f"repos/{REPO}"


def call(path, *, method="GET", payload=None, file=None, tolerate=False):
    args = ["gh", "api", "--hostname", "github.com", "--method", method,
            "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28", path]
    if payload is not None:
        args += ["--input", "-"]
    if file is not None:
        args += ["--input", str(file), "-H", "Content-Type: application/octet-stream"]
    result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True)
    if result.returncode:
        error = result.stderr.strip().replace(os.environ["GITHUB_TOKEN"], "[REDACTED]")[:1000]
        print(json.dumps({"method": method, "path": path, "error": error}), flush=True)
        if not tolerate:
            raise RuntimeError("GitHub API probe failed")
        return None
    return json.loads(result.stdout) if result.stdout.strip() else None


assert os.environ["GITHUB_REPOSITORY"] == REPO
assert os.environ["GITHUB_REF"] == "refs/heads/automation/release-api-probe"
existing = call(f"{ROOT}/releases/437511134", tolerate=True)
print(json.dumps({"existing_draft_by_id": None if existing is None else
                  {key: existing.get(key) for key in ("id", "tag_name", "draft")}}), flush=True)
releases = call(f"{ROOT}/releases?per_page=100")
print(json.dumps({"visible_releases": [{key: item.get(key) for key in ("id", "tag_name", "draft")}
                                       for item in releases]}), flush=True)

tag = f"mika-dev-api-probe-{os.environ['GITHUB_RUN_ID']}"
draft = call(f"{ROOT}/releases", method="POST", payload={
    "tag_name": tag, "target_commitish": os.environ["GITHUB_SHA"],
    "name": "Mika Dev release API verification (temporary)",
    "body": "Temporary draft created and removed by the release API verification workflow.",
    "draft": True, "prerelease": True,
})
release_id = draft["id"]
assert type(release_id) is int and release_id > 0
endpoint = f"{ROOT}/releases/{release_id}"
print(json.dumps({"created_id": release_id, "tag_name": draft["tag_name"], "draft": draft["draft"]}), flush=True)
try:
    by_id = call(endpoint, tolerate=True)
    print(json.dumps({"immediate_id_read": by_id is not None}), flush=True)
    if by_id is None:
        time.sleep(3)
        by_id = call(endpoint)
    assert by_id["id"] == release_id and by_id["draft"] is True
    sample = Path(os.environ["RUNNER_TEMP"]) / "mika-release-api-probe.txt"
    sample.write_text("Mika release API verification\n", encoding="utf-8")
    data = sample.read_bytes()
    upload = f"https://uploads.github.com/{ROOT}/releases/{release_id}/assets?name={quote(sample.name, safe='')}"
    asset = call(upload, method="POST", file=sample)
    expected_digest = "sha256:" + hashlib.sha256(data).hexdigest()
    assert asset["state"] == "uploaded" and asset["size"] == len(data) and asset["digest"] == expected_digest
    print(json.dumps({"uploaded_asset_id": asset["id"], "size": asset["size"], "digest": asset["digest"]}), flush=True)
    updated = call(endpoint, method="PATCH", payload={"draft": True, "prerelease": True, "make_latest": "false"})
    assert updated["draft"] is True and updated["prerelease"] is True
    verified = call(endpoint)
    assert len(verified["assets"]) == 1 and verified["assets"][0]["digest"] == expected_digest
    print("VERIFIED: draft creation, stable-ID reads, binary upload, digest checks, and ID-based update.", flush=True)
finally:
    call(endpoint, method="DELETE")
    print(f"Removed only the temporary probe draft {release_id}.", flush=True)
