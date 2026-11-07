#!/usr/bin/env python3
"""Maintain the Mika Dev fork without executing source code in the signing job.

Only the workflow checked out from the triggering main commit runs this helper.
The build jobs receive neither a write token nor the signing environment secrets.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote


REPOSITORY = "EvanDbg/Mika"
UPSTREAM = "wangzan101/Mika"
PACKAGE = "com.wangzan101.mika.dev"
MIN_VERSION_CODE = 1_000_000
MAX_VERSION_CODE = 2_100_000_000
MAX_RELEASE_INFO_BYTES = 256 * 1024
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
TAG_RE = re.compile(r"v([0-9]+\.[0-9]+\.[0-9]+)-dev\.([1-9][0-9]*)\Z")
MARKER_RE = re.compile(
    r"<!-- mika-dev source=([0-9a-f]{40}) upstream=([0-9a-f]{40}) code=([1-9][0-9]*) -->"
)
KEYSTORE_SECRET = "MIKA_DEV_KEYSTORE_BASE64"
PASSWORD_SECRET = "MIKA_DEV_KEYSTORE_PASSWORD"
PLAN_KEYS = (
    "repository", "upstream_repository", "base_sha", "source_sha", "upstream_sha",
    "workflow_sha", "run_id", "run_attempt", "version_code", "version_name", "tag",
)


class ReleaseError(RuntimeError):
    """An invariant failed; do not advance main or publish an APK."""


class GitHubError(ReleaseError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    for key in (KEYSTORE_SECRET, PASSWORD_SECRET, "GH_TOKEN", "GITHUB_TOKEN"):
        secret = env.get(key, "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def command(args: list[str], *, env: Mapping[str, str] | None = None,
            label: str = "Command") -> str:
    """Never echo commands or signer stderr, which may contain credential details."""
    result = subprocess.run(args, text=True, capture_output=True, env=env)
    if result.returncode:
        raise ReleaseError(f"{label} failed (exit {result.returncode}).")
    return result.stdout


class GitHub:
    def api(self, path: str, *, method: str = "GET", payload: Any = None,
            paginate: bool = False) -> Any:
        args = [
            "gh", "api", "--hostname", "github.com", "--method", method,
            "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28", path,
        ]
        if paginate:
            args += ["--paginate", "--slurp"]
        if payload is not None:
            args += ["--input", "-"]
        result = subprocess.run(
            args, input=None if payload is None else json.dumps(payload),
            text=True, capture_output=True,
        )
        if result.returncode:
            status = re.search(r"\bHTTP ([0-9]{3})\b", result.stderr)
            # API requests contain no secrets in their URL or body. Authentication
            # stays in the runner environment; redact it even from error messages.
            detail = redact(result.stderr.strip())[:1000]
            raise GitHubError(
                f"GitHub {method} {path} failed: {detail}",
                int(status.group(1)) if status else None,
            )
        if not result.stdout.strip():
            return None  # Successful merges may return HTTP 204.
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"GitHub returned invalid JSON for {path}.") from exc
        if paginate:
            if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
                raise GitHubError("Unexpected paginated GitHub response.")
            return [item for page in data for item in page]
        return data

    def release_command(self, *args: str) -> str:
        return command(["gh", "release", *args, "--repo", REPOSITORY], label="GitHub release operation")

    def read_release_info(self, tag: str) -> bytes:
        # Download one small provenance file, never APKs, when deciding to skip.
        with tempfile.TemporaryDirectory(prefix="mika-release-info-") as directory:
            self.release_command("download", tag, "--pattern", "build-info.json", "--dir", directory)
            path = Path(directory) / "build-info.json"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RELEASE_INFO_BYTES:
                raise ReleaseError("Invalid downloaded release build information.")
            return path.read_bytes()


def require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ReleaseError(f"Invalid {label} commit SHA.")
    return value


def get_ref(client: GitHub, ref: str, repository: str = REPOSITORY) -> str:
    data = client.api(f"repos/{repository}/git/ref/{quote(ref, safe='/')}")
    if not isinstance(data, dict) or data.get("object", {}).get("type") != "commit":
        raise ReleaseError(f"Expected a direct commit ref for {repository}:{ref}.")
    return require_sha(data["object"].get("sha"), ref)


def optional_release(client: GitHub, tag: str) -> dict[str, Any] | None:
    try:
        return client.api(f"repos/{REPOSITORY}/releases/tags/{quote(tag, safe='')}")
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise


def emit_outputs(values: Mapping[str, Any], env: Mapping[str, str]) -> None:
    path = env.get("GITHUB_OUTPUT")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            text = str(value).lower() if isinstance(value, bool) else str(value)
            if "\n" in text or "\r" in text:
                raise ReleaseError("Invalid multiline workflow output.")
            stream.write(f"{key}={text}\n")


def summary(text: str, env: Mapping[str, str]) -> None:
    path = env.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")


def run_identity(env: Mapping[str, str]) -> tuple[str, str]:
    run_id, attempt = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", "")
    if (not isinstance(run_id, str) or not isinstance(attempt, str)
            or not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(r"[1-9][0-9]*", attempt)):
        raise ReleaseError("This operation requires a GitHub Actions run identity.")
    return run_id, attempt


def candidate_name(env: Mapping[str, str]) -> str:
    run_id, attempt = run_identity(env)
    return f"automation/candidate-{run_id}-{attempt}"


def apk_names(version_name: str) -> tuple[str, str]:
    return f"Mika-Dev-v{version_name}.apk", f"Mika-Dev-v{version_name}-arm64.apk"


def release_assets(version_name: str) -> set[str]:
    return {*apk_names(version_name), "SHA256SUMS", "build-info.json"}


def release_marker(release: Mapping[str, Any]) -> tuple[str, str, int] | None:
    matches = MARKER_RE.findall(release.get("body") or "")
    if len(matches) != 1:
        return None
    source, upstream, code = matches[0]
    return source, upstream, int(code)


def complete_release(release: Mapping[str, Any], source_sha: str) -> bool:
    """A marker alone is not proof of a complete, usable published version."""
    match = TAG_RE.fullmatch(release.get("tag_name", ""))
    if not match or release.get("draft") is not False or release.get("prerelease") is not True:
        return False
    marker = release_marker(release)
    if not marker or marker[0] != source_sha or marker[2] != int(match.group(2)):
        return False
    assets = release.get("assets")
    if not isinstance(assets, list):
        return False
    names = [asset.get("name") for asset in assets]
    expected = release_assets(release["tag_name"][1:])
    # An additional APK can change the app updater's selection. Exact contents
    # matter even when all four required names also happen to be present.
    if len(names) != len(expected) or set(names) != expected:
        return False
    for name in expected:
        if names.count(name) != 1:
            return False
        asset = assets[names.index(name)]
        size = asset.get("size")
        digest = asset.get("digest", "")
        if (asset.get("state") != "uploaded" or type(size) is not int or size <= 0
                or not isinstance(digest, str) or not digest.startswith("sha256:")
                or not DIGEST_RE.fullmatch(digest[7:])):
            return False
    return True


def verify_published_info(client: GitHub, item: Mapping[str, Any], source_sha: str) -> None:
    """Check the published provenance and APK digests before declaring a no-op."""
    if not complete_release(item, source_sha):
        raise ReleaseError("Published release has incomplete or unexpected assets.")
    assets = {asset["name"]: asset for asset in item["assets"]}
    info_asset = assets["build-info.json"]
    if info_asset["size"] > MAX_RELEASE_INFO_BYTES:
        raise ReleaseError("Published build-info.json is unexpectedly large.")
    raw = client.read_release_info(item["tag_name"])
    if (len(raw) != info_asset["size"]
            or "sha256:" + hashlib.sha256(raw).hexdigest() != info_asset["digest"]):
        raise ReleaseError("Downloaded build-info.json does not match its GitHub size and SHA-256.")
    try:
        info = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReleaseError("Published build-info.json is not valid JSON.") from exc
    marker = release_marker(item)
    expected = {
        "schema": 1, "repository": REPOSITORY, "upstream_repository": UPSTREAM,
        "source_sha": source_sha, "upstream_sha": marker[1], "version_code": marker[2],
        "tag": item["tag_name"], "version_name": item["tag_name"][1:],
        "package": PACKAGE, "application": "Mika Dev", "build_type": "release",
        "signing_certificate_sha256": certificate_digest(),
    }
    if (not isinstance(info, dict) or any(info.get(key) != value for key, value in expected.items())
            or info.get("debuggable") is not False):
        raise ReleaseError("Published build information disagrees with its source, version, or pinned signing identity.")
    require_sha(info.get("base_sha"), "published base")
    require_sha(info.get("workflow_sha"), "published workflow")
    run_identity({"GITHUB_RUN_ID": info.get("run_id", ""), "GITHUB_RUN_ATTEMPT": info.get("run_attempt", "")})
    apk_records = {
        name: {"size": assets[name]["size"], "sha256": assets[name]["digest"][7:]}
        for name in apk_names(info["version_name"])
    }
    if info.get("apks") != apk_records:
        raise ReleaseError("Published APK sizes or SHA-256 digests disagree with build-info.json.")


def next_version_code(releases: list[dict[str, Any]], tags: list[dict[str, Any]] | None = None) -> int:
    # Drafts reserve their numbers too. A failed upload can be retried with a new
    # version without overwriting either a published APK or an incomplete draft.
    codes = [MIN_VERSION_CODE]
    # A run may create its tag and then lose connectivity before creating a
    # draft. Those orphan tags also reserve the code, including after source moves.
    for value in [*releases, *(tags or [])]:
        match = TAG_RE.fullmatch(value.get("tag_name", value.get("name", "")))
        if match:
            codes.append(int(match.group(2)))
    result = max(codes) + 1
    if result > MAX_VERSION_CODE:
        raise ReleaseError("The Android versionCode range is exhausted.")
    return result


def upstream_version(client: GitHub, source_sha: str) -> str:
    data = client.api(f"repos/{REPOSITORY}/contents/app/build.gradle.kts?ref={source_sha}")
    if data.get("encoding") != "base64":
        raise ReleaseError("Unable to read the upstream app version from the source commit.")
    try:
        content = base64.b64decode(data["content"]).decode("utf-8")
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise ReleaseError("Invalid app build configuration response.") from exc
    matches = re.findall(r'^\s*versionName\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$', content, re.M)
    if len(matches) != 1:
        raise ReleaseError("Upstream versionName changed format; review the fork version configuration.")
    return matches[0]


def prepare(client: GitHub, env: Mapping[str, str], path: Path = Path("plan.json")) -> dict[str, Any]:
    run_id, attempt = run_identity(env)
    base_sha = get_ref(client, "heads/main")
    upstream_sha = get_ref(client, "heads/main", UPSTREAM)
    workflow_sha = require_sha(env.get("GITHUB_SHA"), "workflow")
    emit_outputs({"base_sha": base_sha, "upstream_sha": upstream_sha, "candidate_branch": ""}, env)
    comparison = client.api(f"repos/{REPOSITORY}/compare/{upstream_sha}...{base_sha}?per_page=1")
    status = comparison.get("status")
    if status not in {"identical", "ahead", "behind", "diverged"}:
        raise ReleaseError("Unable to determine whether main includes the upstream commit.")
    branch = ""
    source_sha = base_sha
    if status in {"behind", "diverged"}:
        branch = candidate_name(env)
        # Expose the intended ref first: a successful POST can lose its response.
        # cleanup treats a missing ref as a no-op and checks its SHA before deletion.
        emit_outputs({"candidate_branch": branch}, env)
        client.api(f"repos/{REPOSITORY}/git/refs", method="POST", payload={
            "ref": f"refs/heads/{branch}", "sha": base_sha,
        })
        try:
            client.api(f"repos/{REPOSITORY}/merges", method="POST", payload={
                "base": branch, "head": upstream_sha,
                "commit_message": f"Merge {UPSTREAM} main ({upstream_sha[:12]}) into Mika Dev",
            })
        except GitHubError as exc:
            if exc.status == 409:
                raise ReleaseError("Upstream merge conflicts require review; main and published APKs were not changed.") from exc
            raise
        source_sha = get_ref(client, f"heads/{branch}")
    emit_outputs({"source_sha": source_sha}, env)
    releases = client.api(f"repos/{REPOSITORY}/releases?per_page=100", paginate=True)
    if not isinstance(releases, list):
        raise ReleaseError("Unexpected releases response.")
    plan: dict[str, Any] = {
        "schema": 1, "repository": REPOSITORY, "upstream_repository": UPSTREAM,
        "base_sha": base_sha, "source_sha": source_sha, "upstream_sha": upstream_sha,
        "workflow_sha": workflow_sha, "run_id": run_id, "run_attempt": attempt,
        "candidate_branch": branch, "build": True,
    }
    for release in releases:
        marker = release_marker(release)
        if (TAG_RE.fullmatch(release.get("tag_name", "")) and release.get("draft") is False
                and release.get("prerelease") is True and marker and marker[0] == source_sha):
            if not complete_release(release, source_sha):
                raise ReleaseError("Published release for this source has incomplete or unexpected assets; review its integrity before retrying.")
            # Our release tags are deliberately lightweight refs at the tested SHA.
            if get_ref(client, f"tags/{release['tag_name']}") != source_sha:
                raise ReleaseError("Published release metadata and its source tag disagree.")
            verify_published_info(client, release, source_sha)
            plan.update(
                build=False, tag=release["tag_name"], version_name=release["tag_name"][1:],
                version_code=release_marker(release)[2], release_url=release["html_url"],
            )
            break
    if plan["build"]:
        tags = client.api(f"repos/{REPOSITORY}/tags?per_page=100", paginate=True)
        if not isinstance(tags, list):
            raise ReleaseError("Unexpected tags response.")
        code = next_version_code(releases, tags)
        version_name = f"{upstream_version(client, source_sha)}-dev.{code}"
        plan.update(version_code=code, version_name=version_name, tag=f"v{version_name}")
    validate_plan(plan)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    emit_outputs({key: plan[key] for key in (
        "build", "base_sha", "source_sha", "upstream_sha", "candidate_branch",
        "version_code", "version_name", "tag",
    )}, env)
    if plan["build"]:
        summary(f"## Mika Dev 构建计划\n\n- 上游：`{upstream_sha}`\n- 候选源码：`{source_sha}`\n- 版本：`{plan['version_name']}`\n\n验证通过后才推进 main 并发布。", env)
    else:
        summary(f"## 无需重复发布\n\n源码 `{source_sha}` 已有完整版本：[下载]({plan['release_url']})。", env)
    return plan


def validate_plan(plan: Mapping[str, Any], env: Mapping[str, str] | None = None) -> None:
    if (plan.get("schema") != 1 or plan.get("repository") != REPOSITORY
            or plan.get("upstream_repository") != UPSTREAM or type(plan.get("build")) is not bool):
        raise ReleaseError("Invalid release plan identity.")
    for field in ("base_sha", "source_sha", "upstream_sha", "workflow_sha"):
        require_sha(plan.get(field), field)
    code = plan.get("version_code")
    if type(code) is not int or not MIN_VERSION_CODE < code <= MAX_VERSION_CODE:
        raise ReleaseError("Invalid planned Android versionCode.")
    tag = plan.get("tag", "")
    match = TAG_RE.fullmatch(tag) if isinstance(tag, str) else None
    if not match or int(match.group(2)) != code or plan.get("version_name") != tag[1:]:
        raise ReleaseError("Release tag and APK version do not match.")
    run_env = {"GITHUB_RUN_ID": plan.get("run_id", ""), "GITHUB_RUN_ATTEMPT": plan.get("run_attempt", "")}
    run_identity(run_env)
    branch = plan.get("candidate_branch")
    if branch not in {"", candidate_name(run_env)}:
        raise ReleaseError("Invalid candidate branch in release plan.")
    if not branch and plan["base_sha"] != plan["source_sha"]:
        raise ReleaseError("A changed source commit requires a candidate branch.")
    if env is not None:
        if run_identity(env) != run_identity(run_env) or env.get("GITHUB_SHA") != plan["workflow_sha"]:
            raise ReleaseError("The plan belongs to a different workflow run, attempt, or commit; start a new run or rerun all jobs to regenerate it.")


def read_plan(path: Path, env: Mapping[str, str]) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError("Unable to read the release plan.") from exc
    if not isinstance(plan, dict):
        raise ReleaseError("Release plan must be an object.")
    validate_plan(plan, env)
    if not plan["build"]:
        raise ReleaseError("The release plan does not request a build.")
    return plan


def certificate_digest(path: Path | None = None) -> str:
    path = path or Path(__file__).with_name("signing-cert.sha256")
    digest = re.sub(r"[\s:]", "", path.read_text(encoding="ascii")).lower()
    if not DIGEST_RE.fullmatch(digest):
        raise ReleaseError("Invalid pinned signing certificate SHA-256.")
    return digest


def check_badging(text: str, plan: Mapping[str, Any]) -> None:
    package = re.search(r"^package: (.+)$", text, re.M)
    fields = dict(re.findall(r"([A-Za-z]+)='([^']*)'", package.group(1))) if package else {}
    if (fields.get("name") != PACKAGE or fields.get("versionCode") != str(plan["version_code"])
            or fields.get("versionName") != plan["version_name"]):
        raise ReleaseError("APK package or version does not match the release plan.")
    if not re.search(r"^application-label:'Mika Dev'$", text, re.M):
        raise ReleaseError("APK application label must be Mika Dev.")
    if re.search(r"^application-debuggable(?:\s|$)", text, re.M):
        raise ReleaseError("Refusing to publish a debuggable APK.")


def check_signature(text: str, expected: str) -> None:
    certificates = re.findall(r"^Signer #[0-9]+ certificate SHA-256 digest: ([0-9a-fA-F]+)$", text, re.M)
    if len(certificates) != 1 or certificates[0].lower() != expected:
        raise ReleaseError("APK signature does not match the pinned Mika Dev certificate.")


def file_record(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f"Expected a regular release asset: {path.name}.")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"sha256": digest, "size": path.stat().st_size}


def unique_input(directory: Path, filename: str) -> Path:
    paths = list(directory.rglob(filename))
    if len(paths) != 1 or paths[0].is_symlink() or not paths[0].is_file():
        raise ReleaseError(f"Expected exactly one unsigned APK named {filename}.")
    return paths[0]


def checksum_text(records: Mapping[str, Mapping[str, Any]]) -> str:
    return "".join(f"{records[name]['sha256']}  {name}\n" for name in sorted(records))


def sign(plan: Mapping[str, Any], inputs: Path, output: Path, env: Mapping[str, str]) -> None:
    expected_certificate = certificate_digest()
    tools = Path(env["ANDROID_HOME"]) / "build-tools" / "35.0.0"
    aapt, apksigner = str(tools / "aapt"), str(tools / "apksigner")
    unsigned = [unique_input(inputs, name) for name in (
        "app-universal-release-unsigned.apk", "app-arm64-v8a-release-unsigned.apk",
    )]
    clean_env = dict(env)
    clean_env.pop(KEYSTORE_SECRET, None)
    clean_env.pop(PASSWORD_SECRET, None)
    for apk in unsigned:
        check_badging(command([aapt, "dump", "badging", str(apk)], env=clean_env, label="APK identity check"), plan)
    encoded, password = env.get(KEYSTORE_SECRET, ""), env.get(PASSWORD_SECRET, "")
    if not encoded or not password:
        raise ReleaseError("The Mika Dev signing environment secrets are missing.")
    try:
        keystore = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ReleaseError("Invalid signing keystore encoding.") from exc
    if not keystore or len(keystore) > 48_000:
        raise ReleaseError("Invalid signing keystore size.")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ReleaseError("The release output directory must be empty.")
    names = apk_names(plan["version_name"])
    signer_env = dict(clean_env)
    signer_env[PASSWORD_SECRET] = password
    key_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="mika-dev-", suffix=".p12", dir=env["RUNNER_TEMP"], delete=False) as key:
            key_path = Path(key.name)
            os.chmod(key_path, 0o600)
            key.write(keystore)
        for original, name in zip(unsigned, names):
            apk = output / name
            command([
                apksigner, "sign", "--ks", str(key_path), "--ks-key-alias", "mika-dev",
                "--ks-pass", f"env:{PASSWORD_SECRET}", "--key-pass", f"env:{PASSWORD_SECRET}",
                "--v4-signing-enabled", "false",
                "--out", str(apk), str(original),
            ], env=signer_env, label="APK signing")
            # Verify the signed files, not merely the unsigned Gradle outputs.
            check_badging(command([aapt, "dump", "badging", str(apk)], env=clean_env, label="Signed APK identity check"), plan)
            signature = command([apksigner, "verify", "--verbose", "--print-certs", str(apk)], env=clean_env, label="APK signature verification")
            check_signature(signature, expected_certificate)
    finally:
        if key_path is not None:
            key_path.unlink(missing_ok=True)
    records = {name: file_record(output / name) for name in names}
    info = {
        "schema": 1, **{key: plan[key] for key in PLAN_KEYS},
        "application": "Mika Dev", "package": PACKAGE, "build_type": "release",
        "debuggable": False, "signing_certificate_sha256": expected_certificate,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "workflow_run_url": f"https://github.com/{REPOSITORY}/actions/runs/{plan['run_id']}",
        "validation": {
            "required_workflow_jobs": ["core", "cli", "relay", "app"],
            "android_device_tests": "compiled, not executed", "real_device_tests": False,
        },
        "apks": records,
    }
    (output / "build-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    records["build-info.json"] = file_record(output / "build-info.json")
    (output / "SHA256SUMS").write_text(checksum_text(records), encoding="ascii")
    print(f"Verified {len(names)} release APKs with certificate SHA-256 {expected_certificate}.")


def validate_distribution(plan: Mapping[str, Any], directory: Path) -> dict[str, dict[str, Any]]:
    expected = release_assets(plan["version_name"])
    if {path.name for path in directory.iterdir()} != expected:
        raise ReleaseError("The release distribution does not contain exactly the expected assets.")
    records = {name: file_record(directory / name) for name in expected}
    try:
        info = json.loads((directory / "build-info.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError("Invalid build-info.json.") from exc
    if not isinstance(info, dict) or any(info.get(key) != plan[key] for key in PLAN_KEYS):
        raise ReleaseError("Signed build information does not match the release plan.")
    if (info.get("schema") != 1 or info.get("package") != PACKAGE or info.get("application") != "Mika Dev"
            or info.get("build_type") != "release" or info.get("debuggable") is not False
            or info.get("signing_certificate_sha256") != certificate_digest()):
        raise ReleaseError("Unexpected APK identity or signing certificate in build information.")
    apk_records = {name: records[name] for name in apk_names(plan["version_name"])}
    if info.get("apks") != apk_records:
        raise ReleaseError("Signed APK hashes or sizes do not match build information.")
    checksum_records = {name: record for name, record in records.items() if name != "SHA256SUMS"}
    if (directory / "SHA256SUMS").read_text(encoding="ascii") != checksum_text(checksum_records):
        raise ReleaseError("SHA256SUMS does not match the signed release files.")
    return records


def verify_remote_assets(release: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]], *, complete: bool) -> list[str]:
    assets = release.get("assets", [])
    names = [asset.get("name") for asset in assets]
    if len(names) != len(set(names)) or any(name not in records for name in names):
        raise ReleaseError("Unexpected or duplicate assets in the draft release.")
    for asset in assets:
        record = records[asset["name"]]
        if (asset.get("state") != "uploaded" or asset.get("size") != record["size"]
                or asset.get("digest") != f"sha256:{record['sha256']}"):
            raise ReleaseError(f"GitHub asset verification failed for {asset['name']}.")
    missing = sorted(set(records) - set(names))
    if complete and missing:
        raise ReleaseError("The release is missing required uploaded assets.")
    return missing


def promote_main(client: GitHub, plan: Mapping[str, Any]) -> None:
    current = get_ref(client, "heads/main")
    if current not in {plan["base_sha"], plan["source_sha"]}:
        raise ReleaseError("main changed while the candidate was tested; this run will not overwrite it or publish stale APKs.")
    if current != plan["source_sha"]:
        # A non-fast-forward update is forbidden even if another writer races the
        # read above. No force push, reset, or automatic conflict resolution.
        client.api(f"repos/{REPOSITORY}/git/refs/heads/main", method="PATCH", payload={
            "sha": plan["source_sha"], "force": False,
        })
    if get_ref(client, "heads/main") != plan["source_sha"]:
        raise ReleaseError("main changed before publication; leaving the prior release available.")


def ensure_tag(client: GitHub, plan: Mapping[str, Any]) -> None:
    try:
        existing = get_ref(client, f"tags/{plan['tag']}")
    except GitHubError as exc:
        if exc.status != 404:
            raise
        client.api(f"repos/{REPOSITORY}/git/refs", method="POST", payload={
            "ref": f"refs/tags/{plan['tag']}", "sha": plan["source_sha"],
        })
    else:
        if existing != plan["source_sha"]:
            raise ReleaseError("The release tag already points to different source; it will not be replaced.")


def notes(plan: Mapping[str, Any]) -> str:
    marker = f"<!-- mika-dev source={plan['source_sha']} upstream={plan['upstream_sha']} code={plan['version_code']} -->"
    changes = ""
    if plan["base_sha"] != plan["source_sha"]:
        changes = f"\n- 本次同步差异：[查看代码变更](https://github.com/{REPOSITORY}/compare/{plan['base_sha']}...{plan['source_sha']})"
    return f"""## Mika Dev {plan['version_name']}

自动跟进上游 `main`，保留微信任务独立唤醒与解锁授权；这是 fork 的开发版本。

- 应用：**Mika Dev**，包名 `{PACKAGE}`，可与官方 Mika 并排安装。
- `-arm64.apk` 适用于 Pixel 等 ARM64 设备；另一个 APK 是通用版。两者任选一个安装。
- 本渠道后续版本使用固定签名、递增版本码；应用内检查更新指向本仓库。
- 先前 `wechat-unlock-dev-3e24ca5` 临时调试 APK 的签名不同，首次切换需要备份配置后重新安装；不要卸载官方 Mika。
- 已通过工作流中的 core、CLI、relay、app 测试与构建门禁；设备测试仅编译，未进行真机微信解锁测试。
- `SHA256SUMS` 和 `build-info.json` 提供文件校验、版本、来源及签名证书信息。

### 来源

- 上游提交：[{plan['upstream_sha'][:12]}](https://github.com/{UPSTREAM}/commit/{plan['upstream_sha']})
- 实际构建源码：[{plan['source_sha'][:12]}](https://github.com/{REPOSITORY}/commit/{plan['source_sha']})
- 构建与验证：[Actions 运行](https://github.com/{REPOSITORY}/actions/runs/{plan['run_id']}){changes}

{marker}
"""


def publish(client: GitHub, plan: Mapping[str, Any], directory: Path, env: Mapping[str, str]) -> str:
    records = validate_distribution(plan, directory)
    release = optional_release(client, plan["tag"])
    if release is not None and not release.get("draft"):
        if (complete_release(release, plan["source_sha"])
                and release_marker(release) == (plan["source_sha"], plan["upstream_sha"], plan["version_code"])
                and get_ref(client, f"tags/{plan['tag']}") == plan["source_sha"]):
            # Manual reruns never replace an already published APK.
            verify_remote_assets(release, records, complete=True)
            url = release["html_url"]
            emit_outputs({"release_url": url}, env)
            summary(f"已存在完整发布，未覆盖任何附件：[下载]({url})。", env)
            return url
        raise ReleaseError("A published release already uses this tag; it will not be changed.")
    if release is not None and (release_marker(release) != (plan["source_sha"], plan["upstream_sha"], plan["version_code"])
                                or release.get("prerelease") is not True):
        raise ReleaseError("An existing draft does not match this release plan.")
    promote_main(client, plan)
    ensure_tag(client, plan)
    if release is None:
        # Creating a draft first keeps partially uploaded versions out of the app
        # updater and the public release feed. Never use --clobber.
        with tempfile.TemporaryDirectory(prefix="mika-release-notes-", dir=env["RUNNER_TEMP"]) as temp:
            note_path = Path(temp) / "notes.md"
            note_path.write_text(notes(plan), encoding="utf-8")
            client.release_command(
                "create", plan["tag"], "--target", plan["source_sha"], "--verify-tag",
                "--title", f"Mika Dev {plan['version_name']}", "--notes-file", str(note_path),
                "--draft", "--prerelease",
            )
        release = optional_release(client, plan["tag"])
        if release is None:
            raise ReleaseError("The draft release was not created.")
    missing = verify_remote_assets(release, records, complete=False)
    if missing:
        client.release_command("upload", plan["tag"], *(str(directory / name) for name in missing))
    release = optional_release(client, plan["tag"])
    if release is None:
        raise ReleaseError("The draft release disappeared during upload.")
    if release_marker(release) != (plan["source_sha"], plan["upstream_sha"], plan["version_code"]):
        raise ReleaseError("The draft release provenance changed during upload.")
    verify_remote_assets(release, records, complete=True)
    if get_ref(client, "heads/main") != plan["source_sha"]:
        raise ReleaseError("main changed during upload; the incomplete publication remains a draft.")
    client.release_command("edit", plan["tag"], "--draft=false", "--prerelease", "--latest=false")
    release = optional_release(client, plan["tag"])
    if (release is None or not complete_release(release, plan["source_sha"])
            or release_marker(release) != (plan["source_sha"], plan["upstream_sha"], plan["version_code"])):
        raise ReleaseError("Unable to verify the completed publication.")
    verify_remote_assets(release, records, complete=True)
    url = release["html_url"]
    emit_outputs({"release_url": url}, env)
    summary(f"## 已发布 Mika Dev\n\n[{plan['version_name']}]({url})\n\n- 源码：`{plan['source_sha']}`\n- 上游：`{plan['upstream_sha']}`\n- APK 使用固定签名，版本码 `{plan['version_code']}`。\n- 真机测试尚未执行。", env)
    print(url)
    return url


def cleanup(client: GitHub, env: Mapping[str, str]) -> None:
    branch = env.get("CANDIDATE_BRANCH", "")
    if not branch:
        return
    if branch != candidate_name(env):
        raise ReleaseError("Refusing to delete a candidate branch belonging to a different run.")
    expected = {value for value in (env.get("BASE_SHA"), env.get("SOURCE_SHA")) if value and SHA_RE.fullmatch(value)}
    if not expected:
        raise ReleaseError("No known candidate commit was provided for cleanup.")
    try:
        current = get_ref(client, f"heads/{branch}")
    except GitHubError as exc:
        if exc.status == 404:
            return
        raise
    if current not in expected:
        raise ReleaseError("Candidate branch changed unexpectedly; it was retained for inspection.")
    client.api(f"repos/{REPOSITORY}/git/refs/heads/{branch}", method="DELETE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("cleanup")
    signer = commands.add_parser("sign")
    signer.add_argument("--plan", required=True, type=Path)
    signer.add_argument("--input", required=True, type=Path)
    signer.add_argument("--output", required=True, type=Path)
    publisher = commands.add_parser("publish")
    publisher.add_argument("--plan", required=True, type=Path)
    publisher.add_argument("--dist", required=True, type=Path)
    args = parser.parse_args()
    env = os.environ
    try:
        if env.get("GITHUB_REPOSITORY") != REPOSITORY or env.get("GITHUB_REF") != "refs/heads/main":
            raise ReleaseError("This automation is restricted to EvanDbg/Mika main.")
        client = GitHub()
        if args.command == "prepare":
            prepare(client, env)
        elif args.command == "cleanup":
            cleanup(client, env)
        elif args.command == "sign":
            sign(read_plan(args.plan, env), args.input, args.output, env)
        elif args.command == "publish":
            publish(client, read_plan(args.plan, env), args.dist, env)
    except (ReleaseError, OSError, KeyError) as exc:
        # Do not print a traceback or a failed command containing sensitive data.
        message = redact(str(exc)).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
