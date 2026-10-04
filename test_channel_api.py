"""HTTP 层测试：渠道投放台账接口。

需要 Flask（本地无依赖时可跳过；Docker 镜像内可直接运行）：
    python -m unittest test_channel_api.py -v
"""
import os
import sqlite3
import tempfile
import unittest

_fd, _DB = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _DB

from app import app  # noqa: E402

PUB_DATE = "2026-10-04"
CHANNEL = "音频平台 A"
URL = "https://channel.example.com/a/1"


class ChannelApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def setUp(self):
        conn = sqlite3.connect(_DB)
        conn.execute("PRAGMA foreign_keys = ON")
        # 发行凭据与投放记录均受触发器保护不可删除，清空源头表即可
        for table in ("licenses", "segments", "speakers", "interviews"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        self.dist_base = len(self.client.get("/api/distributions").get_json())
        # 夹具：访谈 600 秒；甲发言 0-600，授权 0-600 允许音频
        c = self.client
        self.iid = c.post("/api/interviews", json={
            "title": "访谈", "duration_seconds": 600}).get_json()["id"]
        self.sp1 = c.post(f"/api/interviews/{self.iid}/speakers",
                          json={"name": "甲"}).get_json()["id"]
        c.post(f"/api/interviews/{self.iid}/segments", json={
            "speaker_id": self.sp1, "start_seconds": 0, "end_seconds": 600})
        c.post(f"/api/speakers/{self.sp1}/licenses", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": ""})
        self.cid = c.post(f"/api/interviews/{self.iid}/credentials",
                          json={"publication_date": PUB_DATE,
                                "medium": "audio"}).get_json()["id"]

    def dist_count(self):
        return len(self.client.get("/api/distributions").get_json())

    def register(self, cid=None, channel=CHANNEL, url=URL):
        return self.client.post(
            f"/api/credentials/{cid or self.cid}/distributions",
            json={"channel": channel, "url": url})

    # ---------------------------------------------------------- 登记

    def test_unknown_credential_returns_404(self):
        resp = self.register(cid=9999)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.dist_count(), self.dist_base)

    def test_bad_channel_and_url_rejected(self):
        self.assertEqual(self.register(channel="  ").status_code, 400)
        self.assertEqual(self.register(url="").status_code, 400)
        self.assertEqual(self.register(url="not-a-link").status_code, 400)
        self.assertEqual(self.register(url="ftp://x.io/a").status_code, 400)
        self.assertEqual(self.dist_count(), self.dist_base)

    def test_register_success(self):
        resp = self.register()
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["status_label"], "投放中")
        self.assertEqual(data["channel"], CHANNEL)
        self.assertTrue(data["registered_at"])
        self.assertIsNone(data["withdrawn_at"])
        self.assertEqual(data["credential"]["risk"], "ok")
        self.assertEqual(data["credential"]["risk_label"], "与发行时一致")
        self.assertEqual(
            data["credential"]["sealed"]["result"]["summary"]
            ["publishable_seconds"], 600)
        self.assertEqual(self.dist_count(), self.dist_base + 1)

    def test_register_rejected_when_source_changed_no_record(self):
        # 追加同样允许音频的授权 -> 仍可发布但资料变化
        self.client.post(f"/api/speakers/{self.sp1}/licenses", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": "补充"})
        resp = self.register()
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["created"])
        self.assertEqual(data["reason"], "source_changed")
        self.assertEqual(data["changes"], ["授权"])
        self.assertEqual(data["credential"]["risk"], "action")
        self.assertEqual(data["credential"]["risk_label"], "需处理")
        self.assertEqual(self.dist_count(), self.dist_base)  # 不留记录

    def test_register_rejected_when_source_missing(self):
        self.client.delete(f"/api/interviews/{self.iid}")
        resp = self.register()
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["created"])
        self.assertEqual(data["reason"], "source_missing")
        self.assertEqual(data["credential"]["risk"], "missing")
        self.assertIsNone(data["credential"]["current"])
        self.assertEqual(self.dist_count(), self.dist_base)

    def test_duplicate_active_channel_rejected(self):
        self.assertEqual(self.register().status_code, 201)
        resp = self.register()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["reason"], "duplicate_active")
        # 另一渠道不受影响
        self.assertEqual(
            self.register(channel="平台 B", url="https://b.example.com")
            .status_code, 201)
        self.assertEqual(self.dist_count(), self.dist_base + 2)

    # ---------------------------------------------------------- 列表 / 详情

    def test_list_shares_snapshot_and_shows_risk(self):
        d1 = self.register().get_json()["id"]
        self.register(channel="平台 B", url="https://b/x")
        items = self.client.get("/api/distributions").get_json()
        self.assertEqual([i["id"] for i in items if i["id"] >= d1],
                         [d1, d1 + 1])
        for item in items:
            if item["id"] >= d1:
                self.assertEqual(item["credential"]["risk"], "ok")
        detail = self.client.get(f"/api/distributions/{d1}").get_json()
        self.assertEqual(detail["url"], URL)
        self.assertEqual(detail["credential"]["id"], self.cid)
        self.assertEqual(
            detail["credential"]["sealed"]["result"]["summary"]
            ["publishable_seconds"], 600)

    def test_risk_action_after_change_even_if_still_publishable(self):
        did = self.register().get_json()["id"]
        self.client.post(f"/api/speakers/{self.sp1}/licenses", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": "补充"})
        detail = self.client.get(f"/api/distributions/{did}").get_json()
        self.assertEqual(detail["status"], "active")
        self.assertEqual(detail["credential"]["risk"], "action")
        self.assertEqual(detail["credential"]["changes"], ["授权"])
        self.assertEqual(detail["credential"]["current"]["blocked"], [])
        item = [i for i in self.client.get("/api/distributions").get_json()
                if i["id"] == did][0]
        self.assertEqual(item["credential"]["risk"], "action")

    def test_risk_missing_keeps_link_time_and_sealed_result(self):
        did = self.register().get_json()["id"]
        self.client.delete(f"/api/interviews/{self.iid}")
        detail = self.client.get(f"/api/distributions/{did}").get_json()
        self.assertEqual(detail["status"], "active")
        self.assertEqual(detail["channel"], CHANNEL)
        self.assertEqual(detail["url"], URL)
        self.assertTrue(detail["registered_at"])
        self.assertEqual(detail["credential"]["risk"], "missing")
        self.assertEqual(detail["credential"]["risk_label"], "来源缺失")
        self.assertIsNone(detail["credential"]["current"])
        self.assertEqual(
            detail["credential"]["sealed"]["result"]["summary"]
            ["publishable_seconds"], 600)

    def test_distribution_not_found(self):
        self.assertEqual(self.client.get("/api/distributions/9999").status_code,
                         404)

    # ---------------------------------------------------------- 撤下

    def test_withdraw_requires_reason(self):
        did = self.register().get_json()["id"]
        self.assertEqual(
            self.client.post(f"/api/distributions/{did}/withdraw",
                             json={"reason": "  "}).status_code, 400)

    def test_withdraw_success_keeps_time_and_does_not_touch_credential(self):
        did = self.register().get_json()["id"]
        before = self.client.get(f"/api/credentials/{self.cid}").get_json()
        resp = self.client.post(f"/api/distributions/{did}/withdraw",
                                json={"reason": "授权方要求撤下"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "withdrawn")
        self.assertEqual(data["status_label"], "已撤下")
        self.assertEqual(data["withdraw_reason"], "授权方要求撤下")
        self.assertTrue(data["withdrawn_at"])
        self.assertTrue(data["registered_at"])
        self.assertEqual(data["channel"], CHANNEL)
        self.assertEqual(data["url"], URL)
        # 风险提示仍可查，凭据不改动
        self.assertEqual(data["credential"]["risk"], "ok")
        after = self.client.get(f"/api/credentials/{self.cid}").get_json()
        self.assertEqual(after["sealed"], before["sealed"])
        # 列表也显示已撤下
        item = [i for i in self.client.get("/api/distributions").get_json()
                if i["id"] == did][0]
        self.assertEqual(item["status"], "withdrawn")

    def test_withdraw_twice_rejected(self):
        did = self.register().get_json()["id"]
        self.assertEqual(
            self.client.post(f"/api/distributions/{did}/withdraw",
                             json={"reason": "第一次"}).status_code, 200)
        resp = self.client.post(f"/api/distributions/{did}/withdraw",
                                json={"reason": "第二次"})
        self.assertEqual(resp.status_code, 409)
        detail = self.client.get(f"/api/distributions/{did}").get_json()
        self.assertEqual(detail["withdraw_reason"], "第一次")

    def test_withdraw_unknown_returns_404(self):
        resp = self.client.post("/api/distributions/9999/withdraw",
                                json={"reason": "x"})
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
