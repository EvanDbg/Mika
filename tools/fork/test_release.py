"""Release safety tests; all GitHub and Android tool operations stay offline."""

import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import release


BASE, UPSTREAM, SOURCE, WORKFLOW, OTHER = (char * 40 for char in "abcde")
CERTIFICATE = "f" * 64


def environment(directory):
    return {
        "GITHUB_REPOSITORY": release.REPOSITORY, "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": WORKFLOW,
        "GITHUB_OUTPUT": str(directory / "outputs"), "GITHUB_STEP_SUMMARY": str(directory / "summary"),
        "RUNNER_TEMP": str(directory), "ANDROID_HOME": "/android",
    }


def plan(source=BASE, code=1_000_001):
    version = f"0.1.38-dev.{code}"
    return {
        "schema": 1, "repository": release.REPOSITORY, "upstream_repository": release.UPSTREAM,
        "base_sha": BASE, "source_sha": source, "upstream_sha": UPSTREAM, "workflow_sha": WORKFLOW,
        "run_id": "12345", "run_attempt": "1", "version_code": code, "version_name": version,
        "tag": "v" + version, "candidate_branch": "" if source == BASE else "automation/candidate-12345-1",
        "build": True,
    }


def published(plan_value):
    item = {
        "tag_name": plan_value["tag"], "draft": False, "prerelease": True,
        "body": release.notes(plan_value),
        "html_url": f"https://github.com/{release.REPOSITORY}/releases/tag/{plan_value['tag']}",
        "assets": [
            {"name": name, "state": "uploaded", "size": 42, "digest": "sha256:" + "0" * 64}
            for name in sorted(release.release_assets(plan_value["version_name"]))
        ],
    }
    info = {
        "schema": 1, **{key: plan_value[key] for key in release.PLAN_KEYS},
        "package": release.PACKAGE, "application": "Mika Dev", "build_type": "release",
        "debuggable": False, "signing_certificate_sha256": CERTIFICATE,
        "apks": {name: {"size": 42, "sha256": "0" * 64}
                 for name in release.apk_names(plan_value["version_name"])},
    }
    set_published_info(item, info)
    return item


def set_published_info(item, info):
    raw = json.dumps(info).encode()
    item["_info_bytes"] = raw
    asset = next(asset for asset in item["assets"] if asset["name"] == "build-info.json")
    asset.update(size=len(raw), digest="sha256:" + hashlib.sha256(raw).hexdigest())


class FakeGitHub:
    def __init__(self, *, status="ahead", releases=(), conflict=False):
        self.refs = {
            (release.REPOSITORY, "heads/main"): BASE,
            (release.UPSTREAM, "heads/main"): UPSTREAM,
        }
        self.status = status
        self.releases = deepcopy(list(releases))
        for index, item in enumerate(self.releases, start=100):
            item.setdefault("id", index)
        self.info_files = {item["tag_name"]: item.pop("_info_bytes")
                           for item in self.releases if "_info_bytes" in item}
        self.info_reads = []
        self.conflict = conflict
        self.calls = []
        self.commands = []
        self.merges = []
        self.on_upload = None

    def api(self, path, *, method="GET", payload=None, paginate=False):
        self.calls.append((method, path, deepcopy(payload)))
        for repository in (release.REPOSITORY, release.UPSTREAM):
            prefix = f"repos/{repository}/git/ref/"
            if path.startswith(prefix) and method == "GET":
                key = (repository, path[len(prefix):])
                if key not in self.refs:
                    raise release.GitHubError("Not Found (HTTP 404)", 404)
                return {"object": {"type": "commit", "sha": self.refs[key]}}
        root = f"repos/{release.REPOSITORY}"
        if path.startswith(root + "/compare/"):
            return {"status": self.status}
        if path == root + "/git/refs" and method == "POST":
            key = (release.REPOSITORY, payload["ref"].removeprefix("refs/"))
            if key in self.refs:
                raise release.GitHubError("Reference already exists (HTTP 422)", 422)
            self.refs[key] = payload["sha"]
            return {"object": {"type": "commit", "sha": payload["sha"]}}
        if path == root + "/releases?per_page=100" and paginate:
            return deepcopy(self.releases)
        if path == root + "/tags?per_page=100" and paginate:
            return [{"name": ref.removeprefix("tags/")} for (repo, ref) in self.refs
                    if repo == release.REPOSITORY and ref.startswith("tags/")]
        if path.startswith(root + "/contents/app/build.gradle.kts"):
            return {"encoding": "base64", "content": base64.b64encode(b'versionName = "0.1.38"\n').decode()}
        if path.startswith(root + "/git/refs/"):
            key = (release.REPOSITORY, path[len(root + "/git/refs/"):])
            if method == "PATCH":
                if payload.get("force") is not False:
                    raise AssertionError("A force update must never be requested")
                self.refs[key] = payload["sha"]
                return {"object": {"type": "commit", "sha": payload["sha"]}}
            if method == "DELETE":
                del self.refs[key]
                return None
        if path.startswith(root + "/releases/tags/"):
            tag = path[len(root + "/releases/tags/"):]
            for item in self.releases:
                # The real tag endpoint returns published releases only.
                if item["tag_name"] == tag and not item["draft"]:
                    return deepcopy(item)
            raise release.GitHubError("Not Found (HTTP 404)", 404)
        if path.startswith(root + "/releases/"):
            release_id = path[len(root + "/releases/"):]
            for item in self.releases:
                if str(item["id"]) == release_id:
                    return deepcopy(item)
            raise release.GitHubError("Not Found (HTTP 404)", 404)
        raise AssertionError(f"Unexpected API operation: {method} {path}")

    def read_release_info(self, tag):
        self.info_reads.append(tag)
        return self.info_files[tag]

    def merge_candidate(self, branch, base_sha, upstream_sha):
        self.merges.append((branch, base_sha, upstream_sha))
        if self.conflict:
            raise release.ReleaseError("Upstream merge conflicts require review; main and published APKs were not changed.")
        self.refs[(release.REPOSITORY, "heads/" + branch)] = SOURCE
        return SOURCE

    def release_command(self, *args):
        self.commands.append(args)
        operation, tag = args[:2]
        if operation == "create":
            body = Path(args[args.index("--notes-file") + 1]).read_text()
            release_id = max((item["id"] for item in self.releases), default=99) + 1
            self.releases.append({
                "id": release_id,
                "tag_name": tag, "body": body, "draft": True, "prerelease": True, "assets": [],
                "html_url": f"https://github.com/{release.REPOSITORY}/releases/tag/untagged-test-{release_id}",
            })
            return ""
        item = next(item for item in self.releases if item["tag_name"] == tag)
        if operation == "upload":
            for name in args[2:]:
                path = Path(name)
                record = release.file_record(path)
                item["assets"].append({
                    "name": path.name, "state": "uploaded", "size": record["size"],
                    "digest": "sha256:" + record["sha256"],
                })
                if path.name == "build-info.json":
                    self.info_files[tag] = path.read_bytes()
            if self.on_upload:
                self.on_upload(item)
        elif operation == "edit":
            self.assert_complete_before_publish(item)
            item["draft"] = False
            item["html_url"] = f"https://github.com/{release.REPOSITORY}/releases/tag/{tag}"
        else:
            raise AssertionError(f"Unexpected release operation {operation}")
        return ""

    @staticmethod
    def assert_complete_before_publish(item):
        if len(item["assets"]) != 4:
            raise AssertionError("A release must not be public before every asset is uploaded")


class ReleaseMetadataTest(unittest.TestCase):
    def test_draft_lookup_falls_back_to_authenticated_list_and_id(self):
        item = published(plan())
        item["draft"] = True
        client = FakeGitHub(releases=[item])
        found = release.optional_release(client, item["tag_name"])
        self.assertTrue(found["draft"])
        self.assertEqual(found["tag_name"], item["tag_name"])
        self.assertTrue(any(path.endswith("/releases?per_page=100") for _, path, _ in client.calls))
        self.assertTrue(any(path.endswith("/releases/100") for _, path, _ in client.calls))

    def test_draft_lookup_matches_exact_tag_and_rejects_duplicates(self):
        item = published(plan(code=1_000_010))
        item["draft"] = True
        client = FakeGitHub(releases=[item])
        self.assertIsNone(release.optional_release(client, plan()["tag"]))
        client = FakeGitHub(releases=[item, item])
        with self.assertRaisesRegex(release.ReleaseError, "ambiguous draft lookup"):
            release.optional_release(client, item["tag_name"])

    def test_draft_lookup_does_not_hide_authorization_errors(self):
        client = FakeGitHub()
        with patch.object(client, "api", side_effect=release.GitHubError("Forbidden", 403)) as api:
            with self.assertRaises(release.GitHubError) as caught:
                release.optional_release(client, plan()["tag"])
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(api.call_count, 1)

    def test_version_code_advances_past_drafts_and_ignores_other_channels(self):
        values = [
            {"tag_name": "wechat-unlock-dev-3e24ca5"},
            {"tag_name": "v0.1.38"},
            {"tag_name": "v0.1.38-dev.1000008", "draft": True},
            {"tag_name": "v0.1.38-dev.1000002", "draft": False},
        ]
        self.assertEqual(release.next_version_code(values), 1_000_009)
        self.assertEqual(release.next_version_code(values, [{"name": "v0.1.38-dev.1000011"}]), 1_000_012)
        self.assertEqual(release.next_version_code([]), 1_000_001)
        with self.assertRaises(release.ReleaseError):
            release.next_version_code([{"tag_name": "v0.1.38-dev.2100000000"}])

    def test_noop_requires_published_channel_marker_and_complete_assets(self):
        item = published(plan())
        self.assertTrue(release.complete_release(item, BASE))
        for mutate in (
            lambda value: value.update(draft=True),
            lambda value: value.update(prerelease=False),
            lambda value: value.update(tag_name="v0.1.38"),
            lambda value: value.update(body=value["body"] + value["body"]),
            lambda value: value["assets"].pop(),
            lambda value: value["assets"][0].update(state="new"),
            lambda value: value["assets"][0].update(size=0),
            lambda value: value["assets"][0].update(digest=None),
            lambda value: value["assets"].append({
                "name": "unexpected.apk", "size": 42, "state": "uploaded", "digest": "sha256:" + "0" * 64,
            }),
        ):
            with self.subTest(mutate=mutate):
                changed = deepcopy(item)
                mutate(changed)
                self.assertFalse(release.complete_release(changed, BASE))
        self.assertFalse(release.complete_release(item, SOURCE))

    def test_plan_rejects_cross_run_inputs_version_mismatch_and_foreign_branches(self):
        with tempfile.TemporaryDirectory() as temp:
            env = environment(Path(temp))
            release.validate_plan(plan(), env)
            for key, value in (
                ("run_id", "98765"), ("run_attempt", "2"), ("workflow_sha", OTHER),
                ("version_name", "0.1.38"), ("version_code", True),
                ("source_sha", SOURCE), ("candidate_branch", "main"),
                ("tag", "v0.1.38-dev.1000002"), ("repository", release.UPSTREAM),
            ):
                with self.subTest(field=key):
                    changed = plan()
                    changed[key] = value
                    with self.assertRaises(release.ReleaseError):
                        release.validate_plan(changed, env)

    def test_apk_checks_reject_official_package_wrong_version_debug_and_wrong_signer(self):
        valid = (f"package: name='{release.PACKAGE}' versionCode='1000001' "
                 "versionName='0.1.38-dev.1000001' platformBuildVersionName='15'\n"
                 "application-label:'Mika Dev'\n")
        release.check_badging(valid, plan())
        for badging in (
            valid.replace(release.PACKAGE, "com.wangzan101.mika"),
            valid.replace("versionCode='1000001'", "versionCode='39'"),
            valid.replace("application-label:'Mika Dev'", "application-label:'Mika'"),
            valid + "application-debuggable\n",
        ):
            with self.assertRaises(release.ReleaseError):
                release.check_badging(badging, plan())
        signature = f"Signer #1 certificate SHA-256 digest: {CERTIFICATE}\n"
        release.check_signature(signature, CERTIFICATE)
        for text in ("", signature.replace(CERTIFICATE, "0" * 64), signature + signature.replace("#1", "#2")):
            with self.assertRaises(release.ReleaseError):
                release.check_signature(text, CERTIFICATE)


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.certificate_patch = patch.object(release, "certificate_digest", return_value=CERTIFICATE)
        self.certificate_patch.start()

    def tearDown(self):
        self.certificate_patch.stop()

    def test_ahead_and_identical_both_keep_current_main_without_a_candidate(self):
        for status in ("ahead", "identical"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                client = FakeGitHub(status=status)
                if status == "identical":
                    client.refs[(release.UPSTREAM, "heads/main")] = BASE
                directory = Path(temp)
                value = release.prepare(client, environment(directory), directory / "plan.json")
                self.assertEqual(value["source_sha"], BASE)
                self.assertEqual(value["candidate_branch"], "")
                self.assertTrue(all(method == "GET" for method, _, _ in client.calls))
                self.assertEqual(client.merges, [])

    def test_published_current_source_skips_without_creating_a_candidate(self):
        item = published(plan())
        client = FakeGitHub(releases=[item])
        client.refs[(release.REPOSITORY, "tags/" + item["tag_name"])] = BASE
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            value = release.prepare(client, environment(directory), directory / "plan.json")
            self.assertFalse(value["build"])
            self.assertIn("build=false", (directory / "outputs").read_text())
            self.assertEqual(json.loads((directory / "plan.json").read_text()), value)
        self.assertTrue(all(method == "GET" for method, _, _ in client.calls))
        self.assertEqual(client.info_reads, [item["tag_name"]])

    def test_extra_published_apk_is_an_integrity_error_instead_of_a_successful_noop(self):
        item = published(plan())
        item["assets"].append({
            "name": "unexpected-arm64.apk", "size": 42, "state": "uploaded", "digest": "sha256:" + "0" * 64,
        })
        client = FakeGitHub(releases=[item])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(release.ReleaseError, "unexpected assets"):
                release.prepare(client, environment(Path(temp)), Path(temp) / "plan.json")
        self.assertEqual(client.info_reads, [])

    def test_noop_verifies_downloaded_info_digest(self):
        item = published(plan())
        client = FakeGitHub(releases=[item])
        client.refs[(release.REPOSITORY, "tags/" + item["tag_name"])] = BASE
        client.info_files[item["tag_name"]] += b"changed"
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(release.ReleaseError, "GitHub size and SHA-256"):
                release.prepare(client, environment(Path(temp)), Path(temp) / "plan.json")

    def test_noop_refuses_a_remote_apk_digest_that_disagrees_with_provenance(self):
        item = published(plan())
        apk_asset = next(asset for asset in item["assets"] if asset["name"].endswith(".apk"))
        apk_asset["digest"] = "sha256:" + "1" * 64
        client = FakeGitHub(releases=[item])
        client.refs[(release.REPOSITORY, "tags/" + item["tag_name"])] = BASE
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(release.ReleaseError, "digests disagree"):
                release.prepare(client, environment(Path(temp)), Path(temp) / "plan.json")

    def test_noop_checks_provenance_source_version_package_and_certificate(self):
        for key, bad_value in (
            ("source_sha", OTHER), ("upstream_sha", OTHER), ("tag", "v0.1.38-dev.1000009"),
            ("version_code", 1_000_009), ("version_name", "0.1.38-dev.1000009"),
            ("package", "com.wangzan101.mika"), ("build_type", "debug"), ("debuggable", True),
            ("signing_certificate_sha256", "0" * 64),
        ):
            with self.subTest(field=key), tempfile.TemporaryDirectory() as temp:
                item = published(plan())
                info = json.loads(item["_info_bytes"])
                info[key] = bad_value
                set_published_info(item, info)
                client = FakeGitHub(releases=[item])
                client.refs[(release.REPOSITORY, "tags/" + item["tag_name"])] = BASE
                with self.assertRaisesRegex(release.ReleaseError, "pinned signing identity"):
                    release.prepare(client, environment(Path(temp)), Path(temp) / "plan.json")

    def test_noop_rejects_large_metadata_before_downloading(self):
        item = published(plan())
        next(asset for asset in item["assets"] if asset["name"] == "build-info.json")["size"] = release.MAX_RELEASE_INFO_BYTES + 1
        client = FakeGitHub(releases=[item])
        client.refs[(release.REPOSITORY, "tags/" + item["tag_name"])] = BASE
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(release.ReleaseError, "unexpectedly large"):
                release.prepare(client, environment(Path(temp)), Path(temp) / "plan.json")
        self.assertEqual(client.info_reads, [])

    def test_failed_prior_publication_is_retried_even_without_new_upstream_commits(self):
        unfinished = published(plan(code=1_000_004))
        unfinished["draft"] = True
        client = FakeGitHub(releases=[unfinished])
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            value = release.prepare(client, environment(directory), directory / "plan.json")
        self.assertTrue(value["build"])
        self.assertEqual(value["source_sha"], BASE)
        self.assertEqual(value["version_code"], 1_000_005)
        self.assertEqual(value["candidate_branch"], "")

    def test_new_upstream_is_merged_only_into_the_candidate(self):
        client = FakeGitHub(status="diverged")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            value = release.prepare(client, environment(directory), directory / "plan.json")
        self.assertEqual(value["source_sha"], SOURCE)
        self.assertEqual(value["candidate_branch"], "automation/candidate-12345-1")
        self.assertEqual(client.refs[(release.REPOSITORY, "heads/main")], BASE)
        self.assertFalse(any(method in {"PATCH", "DELETE"} for method, _, _ in client.calls))
        self.assertEqual(client.merges, [(value["candidate_branch"], BASE, UPSTREAM)])
        self.assertIn(f"/compare/{BASE}...{SOURCE}", release.notes(value))

    def test_remote_candidate_must_match_the_locally_merged_commit(self):
        client = FakeGitHub(status="diverged")

        def changed_candidate(branch, _base, _upstream):
            client.refs[(release.REPOSITORY, "heads/" + branch)] = OTHER
            return SOURCE

        client.merge_candidate = changed_candidate
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with self.assertRaisesRegex(release.ReleaseError, "does not match the local merge"):
                release.prepare(client, environment(directory), directory / "plan.json")
            self.assertIn("source_sha=" + SOURCE, (directory / "outputs").read_text())
        self.assertEqual(client.refs[(release.REPOSITORY, "heads/main")], BASE)

    def test_orphan_tag_reserves_version_even_after_the_source_has_changed(self):
        client = FakeGitHub(status="diverged")
        client.refs[(release.REPOSITORY, "tags/v0.1.38-dev.1000009")] = OTHER
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            value = release.prepare(client, environment(directory), directory / "plan.json")
        self.assertEqual(value["source_sha"], SOURCE)
        self.assertEqual(value["version_code"], 1_000_010)
        self.assertEqual(value["tag"], "v0.1.38-dev.1000010")

    def test_conflicts_leave_main_unchanged_and_expose_candidate_for_cleanup(self):
        client = FakeGitHub(status="diverged", conflict=True)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            env = environment(directory)
            with self.assertRaisesRegex(release.ReleaseError, "conflicts require review"):
                release.prepare(client, env, directory / "plan.json")
            outputs = (directory / "outputs").read_text()
            self.assertIn("candidate_branch=automation/candidate-12345-1", outputs)
            self.assertIn("base_sha=" + BASE, outputs)
            release.cleanup(client, {**env, "CANDIDATE_BRANCH": "automation/candidate-12345-1", "BASE_SHA": BASE})
        self.assertEqual(client.refs[(release.REPOSITORY, "heads/main")], BASE)
        self.assertNotIn((release.REPOSITORY, "heads/automation/candidate-12345-1"), client.refs)

    def test_moved_published_tag_fails_instead_of_claiming_noop(self):
        item = published(plan())
        client = FakeGitHub(releases=[item])
        client.refs[(release.REPOSITORY, "tags/" + item["tag_name"])] = OTHER
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(release.ReleaseError, "source tag disagree"):
                release.prepare(client, environment(Path(temp)), Path(temp) / "plan.json")


class SigningAndPublishingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.env = environment(self.directory)
        self.env[release.KEYSTORE_SECRET] = base64.b64encode(b"test-keystore-only").decode()
        self.env[release.PASSWORD_SECRET] = "test-password-only"
        self.inputs = self.directory / "incoming"
        self.inputs.mkdir()
        for name in ("app-universal-release-unsigned.apk", "app-arm64-v8a-release-unsigned.apk"):
            (self.inputs / name).write_bytes(b"test-unsigned-apk")
        self.output = self.directory / "dist"
        self.value = plan(source=SOURCE)
        self.command_calls = []
        self.certificate_patch = patch.object(release, "certificate_digest", return_value=CERTIFICATE)
        self.certificate_patch.start()
        self.command_patch = patch.object(release, "command", side_effect=self.android_tool)
        self.command_patch.start()

    def tearDown(self):
        self.command_patch.stop()
        self.certificate_patch.stop()
        self.temporary.cleanup()

    def android_tool(self, args, *, env=None, label=None):
        self.command_calls.append((args, env, label))
        if Path(args[0]).name == "aapt":
            return (f"package: name='{release.PACKAGE}' versionCode='{self.value['version_code']}' "
                    f"versionName='{self.value['version_name']}'\napplication-label:'Mika Dev'\n")
        if args[1] == "sign":
            key = Path(args[args.index("--ks") + 1])
            self.assertTrue(key.exists())
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(release.KEYSTORE_SECRET, env)
            self.assertEqual(env[release.PASSWORD_SECRET], "test-password-only")
            self.assertNotIn("test-password-only", args)
            Path(args[args.index("--out") + 1]).write_bytes(b"test-signed-apk")
            return ""
        if args[1] == "verify":
            self.assertNotIn(release.PASSWORD_SECRET, env)
            return f"Verifies\nSigner #1 certificate SHA-256 digest: {CERTIFICATE}\n"
        raise AssertionError(f"Unexpected Android command {args}")

    def make_distribution(self):
        with patch("sys.stdout", new_callable=io.StringIO):
            release.sign(self.value, self.inputs, self.output, self.env)

    def test_signing_separates_keys_and_validates_complete_distribution(self):
        self.make_distribution()
        records = release.validate_distribution(self.value, self.output)
        self.assertEqual(set(records), release.release_assets(self.value["version_name"]))
        self.assertFalse(list(self.directory.glob("mika-dev-*.p12")))
        info = json.loads((self.output / "build-info.json").read_text())
        self.assertEqual(info["signing_certificate_sha256"], CERTIFICATE)
        self.assertFalse(info["validation"]["real_device_tests"])
        self.assertEqual(info["source_sha"], SOURCE)
        for value in (self.env[release.KEYSTORE_SECRET], self.env[release.PASSWORD_SECRET]):
            self.assertNotIn(value, (self.output / "build-info.json").read_text())
        for args, _, _ in self.command_calls:
            if args[1] == "sign":
                self.assertEqual(args[args.index("--v4-signing-enabled") + 1], "false")

    def test_failed_signing_also_removes_the_temporary_key(self):
        original_tool = self.android_tool

        def fail_signer(args, **kwargs):
            if args[1] == "sign":
                self.assertTrue(Path(args[args.index("--ks") + 1]).exists())
                raise release.ReleaseError("APK signing failed (exit 1).")
            return original_tool(args, **kwargs)

        with patch.object(release, "command", side_effect=fail_signer):
            with self.assertRaisesRegex(release.ReleaseError, "APK signing failed"):
                release.sign(self.value, self.inputs, self.output, self.env)
        self.assertFalse(list(self.directory.glob("mika-dev-*.p12")))

    def test_tampered_apk_and_signing_metadata_are_rejected(self):
        self.make_distribution()
        apk = self.output / release.apk_names(self.value["version_name"])[0]
        original = apk.read_bytes()
        apk.write_bytes(original + b"changed")
        with self.assertRaisesRegex(release.ReleaseError, "hashes or sizes"):
            release.validate_distribution(self.value, self.output)
        apk.write_bytes(original)
        info_path = self.output / "build-info.json"
        info = json.loads(info_path.read_text())
        info["signing_certificate_sha256"] = "0" * 64
        info_path.write_text(json.dumps(info))
        with self.assertRaisesRegex(release.ReleaseError, "signing certificate"):
            release.validate_distribution(self.value, self.output)

    def test_publish_promotes_nonforce_then_uploads_and_checks_before_publication(self):
        self.make_distribution()
        client = FakeGitHub()
        with patch("sys.stdout", new_callable=io.StringIO):
            url = release.publish(client, self.value, self.output, self.env)
        self.assertEqual(client.refs[(release.REPOSITORY, "heads/main")], SOURCE)
        self.assertEqual(client.refs[(release.REPOSITORY, "tags/" + self.value["tag"])], SOURCE)
        self.assertEqual([args[0] for args in client.commands], ["create", "upload", "edit"])
        self.assertIn("--draft", client.commands[0])
        self.assertIn("--draft=false", client.commands[-1])
        self.assertIn("--latest=false", client.commands[-1])
        self.assertTrue(release.complete_release(client.releases[0], SOURCE))
        self.assertTrue(any(path.endswith("/releases/100") for _, path, _ in client.calls))
        self.assertTrue(url.endswith(self.value["tag"]))
        previous_commands = list(client.commands)
        self.assertEqual(release.publish(client, self.value, self.output, self.env), url)
        self.assertEqual(client.commands, previous_commands, "Published assets must not be overwritten on rerun")

    def test_existing_matching_draft_can_finish_without_creating_a_second_release(self):
        self.make_distribution()
        item = published(self.value)
        item.update(draft=True, assets=[])
        client = FakeGitHub(releases=[item])
        client.refs[(release.REPOSITORY, "tags/" + self.value["tag"])] = SOURCE
        with patch("sys.stdout", new_callable=io.StringIO):
            release.publish(client, self.value, self.output, self.env)
        self.assertEqual([args[0] for args in client.commands], ["upload", "edit"])
        self.assertEqual(len(client.releases), 1)
        self.assertTrue(release.complete_release(client.releases[0], SOURCE))

    def test_existing_published_release_does_not_ignore_changed_remote_digest(self):
        self.make_distribution()
        client = FakeGitHub()
        with patch("sys.stdout", new_callable=io.StringIO):
            release.publish(client, self.value, self.output, self.env)
        client.releases[0]["assets"][0]["digest"] = "sha256:" + "0" * 64
        previous_commands = list(client.commands)
        with self.assertRaisesRegex(release.ReleaseError, "asset verification failed"):
            release.publish(client, self.value, self.output, self.env)
        self.assertEqual(client.commands, previous_commands)

    def test_existing_published_release_checks_full_marker_and_extra_apks(self):
        self.make_distribution()
        for mutation in ("marker", "extra_apk"):
            with self.subTest(mutation=mutation):
                client = FakeGitHub()
                with patch("sys.stdout", new_callable=io.StringIO):
                    release.publish(client, self.value, self.output, self.env)
                item = client.releases[0]
                if mutation == "marker":
                    item["body"] = item["body"].replace("upstream=" + UPSTREAM, "upstream=" + OTHER)
                else:
                    item["assets"].append({
                        "name": "unexpected-arm64.apk", "size": 42, "state": "uploaded", "digest": "sha256:" + "0" * 64,
                    })
                previous_commands = list(client.commands)
                with self.assertRaisesRegex(release.ReleaseError, "published release already"):
                    release.publish(client, self.value, self.output, self.env)
                self.assertEqual(client.commands, previous_commands)

    def test_changed_main_is_never_overwritten_and_nothing_is_published(self):
        self.make_distribution()
        client = FakeGitHub()
        client.refs[(release.REPOSITORY, "heads/main")] = OTHER
        with self.assertRaisesRegex(release.ReleaseError, "main changed while"):
            release.publish(client, self.value, self.output, self.env)
        self.assertTrue(all(method == "GET" for method, _, _ in client.calls))
        self.assertEqual(client.commands, [])

    def test_main_change_during_upload_leaves_a_draft(self):
        self.make_distribution()
        client = FakeGitHub()
        client.on_upload = lambda _item: client.refs.update({(release.REPOSITORY, "heads/main"): OTHER})
        with self.assertRaisesRegex(release.ReleaseError, "main changed during upload"):
            release.publish(client, self.value, self.output, self.env)
        self.assertTrue(client.releases[0]["draft"])
        self.assertNotIn("edit", [args[0] for args in client.commands])
        self.assertEqual(client.refs[(release.REPOSITORY, "heads/main")], OTHER)

    def test_wrong_remote_digest_keeps_release_private(self):
        self.make_distribution()
        client = FakeGitHub()
        client.on_upload = lambda item: item["assets"][0].update(digest="sha256:" + "0" * 64)
        with self.assertRaisesRegex(release.ReleaseError, "asset verification failed"):
            release.publish(client, self.value, self.output, self.env)
        self.assertTrue(client.releases[0]["draft"])
        self.assertNotIn("edit", [args[0] for args in client.commands])

    def test_missing_remote_asset_is_not_a_complete_release(self):
        self.make_distribution()
        records = release.validate_distribution(self.value, self.output)
        self.assertEqual(release.verify_remote_assets({"assets": []}, records, complete=False), sorted(records))
        with self.assertRaisesRegex(release.ReleaseError, "missing required"):
            release.verify_remote_assets({"assets": []}, records, complete=True)


class CleanupAndLoggingTest(unittest.TestCase):
    def test_cleanup_preserves_foreign_or_changed_branches(self):
        client = FakeGitHub()
        with tempfile.TemporaryDirectory() as temp:
            env = {**environment(Path(temp)), "BASE_SHA": BASE, "SOURCE_SHA": SOURCE}
            with self.assertRaises(release.ReleaseError):
                release.cleanup(client, {**env, "CANDIDATE_BRANCH": "main"})
            branch = "automation/candidate-12345-1"
            client.refs[(release.REPOSITORY, "heads/" + branch)] = OTHER
            with self.assertRaisesRegex(release.ReleaseError, "retained for inspection"):
                release.cleanup(client, {**env, "CANDIDATE_BRANCH": branch})
        self.assertFalse(any(method == "DELETE" for method, _, _ in client.calls))

    def test_gitHub_errors_keep_status_but_redact_token_and_signing_values(self):
        private = {
            "GITHUB_TOKEN": "test-github-token", release.KEYSTORE_SECRET: "test-keystore-base64",
            release.PASSWORD_SECRET: "test-store-password",
        }
        error = "gh: Forbidden (HTTP 403) " + " ".join(private.values())
        completed = subprocess.CompletedProcess([], 1, "", error)
        with patch.dict(os.environ, private), patch("subprocess.run", return_value=completed):
            with self.assertRaises(release.GitHubError) as caught:
                release.GitHub().api("repos/EvanDbg/Mika/merges", method="POST", payload={})
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("Forbidden", str(caught.exception))
        for value in private.values():
            self.assertNotIn(value, str(caught.exception))

    def test_signer_error_does_not_log_command_or_secret_stderr(self):
        completed = subprocess.CompletedProcess([], 1, "", "password=private-value")
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(release.ReleaseError) as caught:
                release.command(["apksigner", "sign", "private-value"], label="APK signing")
        self.assertEqual(str(caught.exception), "APK signing failed (exit 1).")

    def test_git_failure_details_are_redacted_and_timeouts_are_bounded(self):
        secret_env = {"GITHUB_TOKEN": "test-token-only"}
        failed = subprocess.CompletedProcess([], 128, "merge output", "fatal: denied test-token-only")
        with patch("subprocess.run", return_value=failed):
            with self.assertRaises(release.ReleaseError) as caught:
                release.git_command(["push", "origin"], env=secret_env)
        self.assertIn("fatal: denied", str(caught.exception))
        self.assertNotIn("test-token-only", str(caught.exception))
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["git"], 120)):
            with self.assertRaisesRegex(release.ReleaseError, "timed out after 120 seconds"):
                release.git_command(["fetch", "origin"], env={})

    def test_candidate_push_uses_temporary_askpass_and_no_force(self):
        for fail_push in (False, True):
            with self.subTest(fail_push=fail_push):
                askpass_paths = []

                def fake_git(args, *, env, timeout=120):
                    self.assertNotIn("test-token-only", args)
                    if args[0] == "clone":
                        Path(args[-1]).mkdir()
                    if "push" in args:
                        self.assertNotIn("--force", args)
                        self.assertNotIn("--force-with-lease", args)
                        self.assertEqual(args[-1], "HEAD:refs/heads/automation/candidate-12345-1")
                        self.assertEqual(env["GITHUB_TOKEN"], "test-token-only")
                        askpass = Path(env["GIT_ASKPASS"])
                        askpass_paths.append(askpass)
                        self.assertEqual(askpass.stat().st_mode & 0o777, 0o700)
                        text = askpass.read_text()
                        self.assertIn("$GITHUB_TOKEN", text)
                        self.assertNotIn("test-token-only", text)
                        if fail_push:
                            raise release.ReleaseError("Git operation failed (exit 128): rejected")
                    else:
                        self.assertNotIn("GITHUB_TOKEN", env)
                        self.assertNotIn("GH_TOKEN", env)
                    return ""

                with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token-only", "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "1"}), \
                     patch.object(release, "git_command", side_effect=fake_git), \
                     patch.object(release, "merge_local", return_value=SOURCE), \
                     patch.object(release, "emit_outputs") as outputs:
                    if fail_push:
                        with self.assertRaisesRegex(release.ReleaseError, "rejected"):
                            release.GitHub().merge_candidate("automation/candidate-12345-1", BASE, UPSTREAM)
                    else:
                        self.assertEqual(release.GitHub().merge_candidate("automation/candidate-12345-1", BASE, UPSTREAM), SOURCE)
                    self.assertEqual(outputs.call_args.args[0], {"source_sha": SOURCE})
                self.assertEqual(len(askpass_paths), 1)
                self.assertFalse(askpass_paths[0].exists())
                self.assertFalse(askpass_paths[0].parent.exists())


class LocalGitMergeTest(unittest.TestCase):
    """Exercise real Git merge semantics in disposable local repositories."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "repo"
        subprocess.run(["git", "init", "--initial-branch=main", str(self.repository)], check=True, capture_output=True, timeout=20)
        self.git("config", "user.name", "Fixture Author")
        self.git("config", "user.email", "fixture@example.test")
        self.git("config", "commit.gpgsign", "false")
        (self.repository / "shared.txt").write_text("base\n")
        workflow = self.repository / ".github/workflows/ci.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: CI\n")
        self.git("add", ".")
        self.git("commit", "-m", "shared ancestor")
        self.common = self.git("rev-parse", "HEAD")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repository), *args],
                                check=True, text=True, capture_output=True, timeout=20,
                                env=release.public_git_env(os.environ))
        return result.stdout.strip()

    def branch(self, name, changes):
        self.git("checkout", "-b", name, self.common)
        for name, content in changes.items():
            path = self.repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("add", ".")
        self.git("commit", "-m", name)
        return self.git("rev-parse", "HEAD")

    def test_merge_preserves_fork_and_upstream_history_and_source(self):
        fork = self.branch("fork", {"wechat.txt": "independent authorization\n"})
        upstream = self.branch("upstream", {"upstream.txt": "new upstream feature\n"})
        merged = release.merge_local(self.repository, fork, upstream)
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", merged).split()[1:], [fork, upstream])
        self.assertEqual((self.repository / "wechat.txt").read_text(), "independent authorization\n")
        self.assertEqual((self.repository / "upstream.txt").read_text(), "new upstream feature\n")
        self.assertEqual(self.git("show", "-s", "--format=%an <%ae>", merged),
                         "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>")

    def test_conflicting_application_changes_stop_without_choosing_a_side(self):
        fork = self.branch("fork", {"shared.txt": "fork edit\n"})
        upstream = self.branch("upstream", {"shared.txt": "upstream edit\n"})
        with self.assertRaisesRegex(release.ReleaseError, "merge conflicts require review"):
            release.merge_local(self.repository, fork, upstream)
        self.assertEqual(self.git("rev-parse", "HEAD"), fork)
        self.assertEqual(self.git("diff", "--name-only", "--diff-filter=U"), "shared.txt")

    def test_upstream_workflow_changes_stop_before_candidate_can_be_pushed(self):
        fork = self.branch("fork", {"wechat.txt": "independent authorization\n"})
        upstream = self.branch("upstream", {".github/workflows/ci.yml": "name: Changed CI\n"})
        with self.assertRaisesRegex(release.ReleaseError, r"changes \.github/workflows"):
            release.merge_local(self.repository, fork, upstream)
        self.assertEqual(self.git("rev-parse", "refs/heads/fork"), fork)


if __name__ == "__main__":
    unittest.main()
