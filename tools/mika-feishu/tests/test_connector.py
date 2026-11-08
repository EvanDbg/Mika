import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

from mika_feishu.api import ApiError, MikaApi
from mika_feishu.bridge import Bridge, normalize
from mika_feishu.store import Store


def event(mid="om_1", text="查询天气", user="ou_owner", chat_type="p2p"):
    return {"event": {"sender": {"sender_type": "user", "sender_id": {"open_id": user}},
                      "message": {"message_id": mid, "chat_id": "oc_chat", "chat_type": chat_type,
                                  "message_type": "text", "create_time": str(int(time.time() * 1000)),
                                  "content": json.dumps({"text": text})}}}


class FakeApi:
    def __init__(self):
        self.writes = []
        self.status = {"task_id": "task_1", "conversation": "mcp-test", "status": "running"}
        self.error = None

    def task(self, tid):
        return dict(self.status)

    def start(self, goal, conversation=None):
        self.writes.append(("start", goal, conversation))
        if self.error:
            raise self.error
        return dict(self.status)

    def action(self, tid, action, text=None):
        self.writes.append((action, text))
        return {"accepted": True}

    safe_id = staticmethod(MikaApi.safe_id)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.db")
        self.api, self.sent = FakeApi(), []
        self.bridge = Bridge(self.api, self.store, lambda *args: self.sent.append(args), ["ou_owner"])

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def deliver(self, text="查询天气", mid="om_1"):
        self.assertTrue(self.bridge.receive(event(mid, text)))
        self.bridge.work_once()

    def test_unauthorized_and_group_never_execute(self):
        self.assertFalse(self.bridge.receive(event(user="ou_other")))
        self.assertFalse(self.bridge.receive(event(chat_type="group")))
        self.assertFalse(self.bridge.work_once())
        self.assertEqual(self.api.writes, [])

    def test_duplicate_event_after_restart_executes_once(self):
        self.deliver()
        restarted = Bridge(self.api, self.store, lambda *a: None, ["ou_owner"])
        self.assertFalse(restarted.receive(event()))
        self.assertEqual(len(self.api.writes), 1)

    def test_lost_post_response_blocks_future_tasks(self):
        self.api.error = ApiError("timeout", uncertain=True)
        self.deliver()
        self.deliver("再试一次", "om_2")
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.store.session("oc_chat:ou_owner")["uncertain"], 1)

    def test_definite_rejection_does_not_leave_uncertainty(self):
        self.api.error = ApiError("http_400", status=400)
        self.deliver()
        self.assertEqual(self.store.session("oc_chat:ou_owner")["uncertain"], 0)

    def test_crash_after_claim_is_not_replayed(self):
        self.bridge.receive(event())
        self.store.pop()  # simulate crash between durable claim and receipt persistence
        self.bridge.recover()
        self.assertFalse(self.bridge.work_once())
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.store.session("oc_chat:ou_owner")["uncertain"], 1)

    def test_questions_answered_but_phone_approvals_cannot_be_answered(self):
        self.deliver()
        self.api.status.update(status="waiting_on_phone")
        self.deliver("/answer 同意", "om_2")
        self.assertEqual(len(self.api.writes), 1)
        self.api.status.update(status="waiting_for_answer", question="哪个城市？")
        self.deliver("上海", "om_3")
        self.assertEqual(self.api.writes[-1], ("answer", "上海"))

    def test_terminal_notifications_sent_once_and_resume_same_conversation(self):
        self.deliver()
        self.api.status.update(status="done", answer="今天晴天")
        self.bridge.poll_once()
        count = len(self.sent)
        self.bridge.poll_once()
        self.assertEqual(len(self.sent), count)
        self.deliver("明天呢", "om_2")
        self.assertEqual(self.api.writes[-1], ("start", "明天呢", "mcp-test"))

    def test_cancel_ack_is_not_reported_as_cancelled(self):
        self.deliver()
        self.deliver("/cancel", "om_2")
        self.assertNotIn("Task cancelled", self.sent[-1][1])
        self.api.status.update(status="cancelled")
        self.bridge.poll_once()
        self.assertIn("Task cancelled", self.sent[-1][1])

    def test_pairing_is_one_time_private_and_persistent(self):
        bridge = Bridge(self.api, self.store, lambda *a: None)
        code = bridge.enable_pairing()
        self.assertFalse(bridge.receive(event(text="/pair " + code, chat_type="group")))
        self.assertTrue(bridge.receive(event(text="/pair " + code)))
        self.assertFalse(bridge.receive(event("om_other", "/pair " + code, user="ou_other")))
        self.assertEqual(Bridge(self.api, self.store, lambda *a: None).owners(), {"ou_owner"})

    def test_unknown_status_does_not_start_another_task(self):
        self.deliver()
        self.api.status["status"] = "future_status"
        self.deliver("又一件事", "om_2")
        self.assertEqual(len(self.api.writes), 1)

    def test_stale_events_ignored(self):
        data = event()
        data["event"]["message"]["create_time"] = "1"
        self.assertIsNone(normalize(data))

    def test_persisted_old_message_is_not_executed_after_restart(self):
        accepted = normalize(event())
        accepted["created_at"] = 1
        self.store.enqueue(accepted)
        self.bridge.work_once()
        self.assertEqual(self.api.writes, [])
        self.assertIn("expired", self.sent[-1][1])

    def test_real_sdk_event_serialization(self):
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        payload = json.loads(lark.JSON.marshal(P2ImMessageReceiveV1(event())))
        self.assertEqual(normalize(payload)["user"], "ou_owner")

    def test_private_config_permissions_and_secret_roundtrip(self):
        from mika_feishu.config import load_config, save_config
        path = Path(self.tmp.name) / "private" / "config.json"
        config = {"feishu_app_id": "cli_fake", "feishu_app_secret": "not-real",
                  "mika_url": "http://127.0.0.1:8765", "mika_token": "not-real"}
        save_config(config, path)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(load_config(path)["mika_token"], "not-real")


class ContractTests(unittest.TestCase):
    """Wire-level contract derived from official v0.1.31 RemoteTaskApi/RemoteStatus."""
    def setUp(self):
        self.calls, self.version, self.caps, self.mode = [], 1, ["tasks", "task_steer"], "ok"
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                parent.calls.append((self.command, self.path, body, self.headers.get("Authorization")))
                if self.path == "/api/v1":
                    response = {"api_version": parent.version, "server_version": "0.1.31",
                                "capabilities": parent.caps, "limits": {"max_goal": 4000}}
                else:
                    response = {"task_id": "task_1", "status": "running", "conversation": "mcp-test"}
                if parent.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/unexpected")
                    self.end_headers()
                    return
                if self.command == "POST" and parent.mode == "lost":
                    self.connection.shutdown(2)
                    self.connection.close()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
            do_GET = handle_request
            do_POST = handle_request
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = MikaApi(f"http://127.0.0.1:{self.server.server_port}", "test-token")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_bearer_and_versioned_task_payload(self):
        self.api.start("总结今天的消息")
        self.assertEqual(self.calls[-1], ("POST", "/api/v1/tasks", {"goal": "总结今天的消息"}, "Bearer test-token"))

    def test_future_api_version_prevents_writes(self):
        self.version = 2
        with self.assertRaises(ApiError):
            self.api.start("test")
        self.assertEqual([c[0] for c in self.calls], ["GET"])

    def test_feature_detection_prevents_unsupported_steer(self):
        self.caps = ["tasks"]
        with self.assertRaises(ApiError):
            self.api.action("task_1", "steer", "追加")
        self.assertEqual([c[0] for c in self.calls], ["GET"])

    def test_lost_response_not_retried(self):
        self.mode = "lost"
        with self.assertRaises(ApiError) as ctx:
            self.api.start("test")
        self.assertTrue(ctx.exception.uncertain)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_redirect_never_followed(self):
        self.mode = "redirect"
        with self.assertRaises(ApiError):
            self.api.check()
        self.assertEqual(len(self.calls), 1)

    def test_additive_fields_tolerated(self):
        info = self.api.check()
        self.assertEqual(info["server_version"], "0.1.31")


if __name__ == "__main__":
    unittest.main()
