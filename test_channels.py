"""渠道投放台账事务编排（channels 模块）的单元测试。

不依赖 Flask：直接对临时 SQLite 数据库验证
登记时同一写事务内复核来源（一致 / 变化 / 缺失 / 阻断 / 重复 -> 不留记录）、
列表与详情在一致读取快照中展示投放状态与凭据当前风险、
撤下保留时间与原因且凭据不改动、台账记录不可删除 / 不可改登记信息。
"""
import json
import os
import sqlite3
import tempfile
import unittest

import channels
from credentials import create_credential

# 与 app.py 中 SCHEMA 保持一致（测试夹具，独立维护）
SCHEMA = """
CREATE TABLE interviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0)
);
CREATE TABLE speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    name TEXT NOT NULL
);
CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    start_seconds INTEGER NOT NULL,
    end_seconds INTEGER NOT NULL,
    CHECK (start_seconds >= 0 AND end_seconds > start_seconds)
);
CREATE TABLE licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    start_seconds INTEGER NOT NULL,
    end_seconds INTEGER NOT NULL,
    allow_media TEXT NOT NULL DEFAULT '[]',
    deny_media TEXT NOT NULL DEFAULT '[]',
    embargo_until TEXT,
    note TEXT NOT NULL DEFAULT '',
    CHECK (start_seconds >= 0 AND end_seconds > start_seconds)
);
CREATE TABLE release_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL,
    interview_title TEXT NOT NULL,
    medium TEXT NOT NULL,
    publication_date TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TRIGGER release_credentials_no_update
BEFORE UPDATE ON release_credentials
BEGIN SELECT RAISE(ABORT, '发行凭据不可修改'); END;
CREATE TRIGGER release_credentials_no_delete
BEFORE DELETE ON release_credentials
BEGIN SELECT RAISE(ABORT, '发行凭据不可删除'); END;
CREATE TABLE channel_distributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'withdrawn')),
    registered_at TEXT NOT NULL DEFAULT (datetime('now')),
    withdrawn_at TEXT,
    withdraw_reason TEXT NOT NULL DEFAULT ''
);
CREATE TRIGGER channel_distributions_no_delete
BEFORE DELETE ON channel_distributions
BEGIN SELECT RAISE(ABORT, '渠道投放记录不可删除'); END;
CREATE TRIGGER channel_distributions_status_only
BEFORE UPDATE ON channel_distributions
WHEN NEW.credential_id <> OLD.credential_id
  OR NEW.channel <> OLD.channel
  OR NEW.url <> OLD.url
  OR NEW.registered_at <> OLD.registered_at
  OR OLD.status = 'withdrawn'
  OR NEW.status NOT IN ('active', 'withdrawn')
  OR (OLD.status = 'active' AND NEW.status = 'active')
BEGIN SELECT RAISE(ABORT, '投放记录仅允许由投放中标记为已撤下'); END;
"""

PUB_DATE = "2026-10-04"
CHANNEL = "音频平台 A"
URL = "https://channel.example.com/a/1"


class ChannelsTest(unittest.TestCase):
    """夹具：访谈 600 秒；甲发言 0-600，授权 0-600 允许音频 -> 可发布。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        c = self.conn
        c.execute("INSERT INTO interviews (title, duration_seconds)"
                  " VALUES ('访谈', 600)")
        c.execute("INSERT INTO speakers (interview_id, name) VALUES (1, '甲')")
        c.execute("INSERT INTO segments (interview_id, speaker_id,"
                  " start_seconds, end_seconds) VALUES (1, 1, 0, 600)")
        c.execute("INSERT INTO licenses (speaker_id, start_seconds,"
                  " end_seconds, allow_media, deny_media, embargo_until,"
                  " note) VALUES (1, 0, 600, '[\"audio\"]', '[]', NULL, '')")
        c.commit()
        self.cid = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="audio")[0]["id"]

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def dist_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM channel_distributions").fetchone()["n"]

    def register(self, cid=None, channel=CHANNEL, url=URL):
        return channels.create_distribution(
            self.conn, credential_id=self.cid if cid is None else cid,
            channel=channel, url=url)

    # ---------------------------------------------------------- 登记校验

    def test_validate_placement(self):
        self.assertEqual(channels.validate_placement("  渠道 ", " https://x.io/a "),
                         ({"channel": "渠道", "url": "https://x.io/a"}, None))
        self.assertIsNotNone(channels.validate_placement("", URL)[1])
        self.assertIsNotNone(channels.validate_placement(CHANNEL, "")[1])
        self.assertIsNotNone(
            channels.validate_placement(CHANNEL, "not-a-url")[1])
        self.assertIsNotNone(
            channels.validate_placement(CHANNEL, "ftp://x.io/a")[1])
        self.assertIsNotNone(
            channels.validate_placement(CHANNEL, "https:///a")[1])

    # ---------------------------------------------------------- 登记成功

    def test_register_success_writes_active_with_timestamp(self):
        result, status = self.register()
        self.assertEqual(status, 201)
        self.assertEqual(result["status"], channels.DIST_ACTIVE)
        self.assertEqual(result["status_label"], "投放中")
        self.assertEqual(result["channel"], CHANNEL)
        self.assertEqual(result["url"], URL)
        self.assertIsNotNone(result["registered_at"])
        self.assertIsNone(result["withdrawn_at"])
        # 同一快照中的凭据风险：与发行时一致
        self.assertEqual(result["credential"]["risk"], channels.RISK_OK)
        self.assertEqual(result["credential"]["risk_label"], "与发行时一致")
        # 封存结论随详情返回
        self.assertEqual(
            result["credential"]["sealed"]["result"]["summary"]
            ["publishable_seconds"], 600)
        self.assertEqual(self.dist_count(), 1)

    def test_register_unknown_credential_returns_404(self):
        result, status = self.register(cid=9999)
        self.assertEqual(status, 404)
        self.assertEqual(self.dist_count(), 0)

    # ---------------------------------------------------------- 复核拒绝

    def test_register_blocked_when_source_changed_even_if_publishable(self):
        # 追加一条同样允许音频的授权：资料变化，但仍可发布
        self.conn.execute(
            "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
            " allow_media, deny_media, embargo_until, note)"
            " VALUES (1, 0, 600, '[\"audio\"]', '[]', NULL, '补充')")
        self.conn.commit()
        result, status = self.register()
        self.assertEqual(status, 409)
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], channels.REASON_CHANGED)
        self.assertEqual(result["changes"], ["授权"])
        self.assertEqual(result["credential"]["risk"], channels.RISK_ACTION)
        self.assertEqual(self.dist_count(), 0)  # 不留记录

    def test_register_blocked_when_currently_not_publishable(self):
        # 授权改为拒绝音频：资料变化且当前阻断 -> 拒绝并不留记录，
        # 拒绝结果附当前复核视图（含阻断区间）。
        self.conn.execute(
            "UPDATE licenses SET allow_media = '[]', deny_media = '[\"audio\"]'"
            " WHERE speaker_id = 1")
        self.conn.commit()
        result, status = self.register()
        self.assertEqual(status, 409)
        self.assertFalse(result["created"])
        self.assertIn(result["reason"],
                      (channels.REASON_CHANGED, channels.REASON_BLOCKED))
        self.assertEqual(self.dist_count(), 0)
        blocked = result["credential"]["current"]["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertTrue(any("拒绝" in r for r in blocked[0]["reasons"]))

    def test_register_blocked_when_only_publishability_broken(self):
        # 构造「资料一致」封存后、当前阻断且快照比较不变是不可能的
        # （授权改动必然属于资料变化）；此处验证阻断明细随拒绝返回。
        self.conn.execute("DELETE FROM licenses WHERE speaker_id = 1")
        self.conn.commit()
        result, status = self.register()
        self.assertEqual(status, 409)
        self.assertFalse(result["created"])
        self.assertEqual(self.dist_count(), 0)
        self.assertEqual(result["credential"]["risk"], channels.RISK_ACTION)
        self.assertEqual(result["credential"]["changes"], ["授权"])
        self.assertEqual(len(result["credential"]["current"]["blocked"]), 1)

    def test_register_blocked_when_source_missing(self):
        # 先登记成功，再删访谈：新登记必须被拒（来源缺失），不留记录
        ok, st = self.register(channel="先登记的渠道")
        self.assertEqual(st, 201)
        before = self.dist_count()
        self.conn.execute("DELETE FROM interviews WHERE id = 1")
        self.conn.commit()
        result, status = self.register(channel="另一个渠道")
        self.assertEqual(status, 409)
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], channels.REASON_MISSING)
        self.assertEqual(result["credential"]["risk"], channels.RISK_MISSING)
        self.assertIsNone(result["credential"]["current"])
        # 封存结论仍随拒绝原因返回
        self.assertEqual(
            result["credential"]["sealed"]["result"]["summary"]
            ["publishable_seconds"], 600)
        self.assertEqual(self.dist_count(), before)  # 不留新记录

    def test_duplicate_active_channel_rejected_but_withdrawn_allows_reregister(self):
        result, status = self.register()
        self.assertEqual((status, result["status"]), (201, "active"))
        dup, st = self.register()
        self.assertEqual(st, 409)
        self.assertEqual(dup["reason"], channels.REASON_DUPLICATE)
        self.assertEqual(self.dist_count(), 1)
        # 不同渠道可以登记
        other, st2 = self.register(channel="音频平台 B",
                                   url="https://b.example.com/x")
        self.assertEqual(st2, 201)
        # 同一渠道在撤下后允许重新登记
        withdrawn, wst = channels.withdraw_distribution(
            self.conn, result["id"], "授权到期")
        self.assertEqual(wst, 200)
        again, ast = self.register()
        self.assertEqual(ast, 201)
        self.assertEqual(self.dist_count(), 3)

    # ---------------------------------------------------------- 列表 / 详情

    def test_list_and_detail_reflect_risk_from_one_snapshot(self):
        d1, _ = self.register(channel="平台 A")
        d2, _ = self.register(channel="平台 B", url="https://b/x")
        items = channels.list_distributions(self.conn)
        self.assertEqual([i["id"] for i in items], [d1["id"], d2["id"]])
        for item in items:
            self.assertEqual(item["status"], "active")
            self.assertEqual(item["credential"]["risk"], channels.RISK_OK)
            self.assertEqual(item["credential"]["risk_label"], "与发行时一致")
        # 资料变化 -> 列表提示「需处理」，即便仍可发布
        self.conn.execute(
            "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
            " allow_media, deny_media, embargo_until, note)"
            " VALUES (1, 0, 600, '[\"audio\"]', '[]', NULL, '又补一条')")
        self.conn.commit()
        items = channels.list_distributions(self.conn)
        self.assertTrue(all(i["credential"]["risk"] == channels.RISK_ACTION
                            for i in items))
        self.assertTrue(all(i["credential"]["changes"] == ["授权"]
                            for i in items))
        detail = channels.distribution_detail(self.conn, d1["id"])
        self.assertEqual(detail["credential"]["risk"], channels.RISK_ACTION)
        self.assertEqual(detail["credential"]["current"]["blocked"], [])
        self.assertEqual(
            detail["credential"]["sealed"]["result"]["summary"]
            ["blocked_seconds"], 0)

    def test_missing_source_keeps_history(self):
        d, _ = self.register()
        self.conn.execute("DELETE FROM interviews WHERE id = 1")
        self.conn.commit()
        detail = channels.distribution_detail(self.conn, d["id"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["status"], "active")  # 投放状态不变
        self.assertEqual(detail["channel"], CHANNEL)
        self.assertEqual(detail["url"], URL)
        self.assertIsNotNone(detail["registered_at"])
        self.assertEqual(detail["credential"]["risk"], channels.RISK_MISSING)
        self.assertEqual(detail["credential"]["risk_label"], "来源缺失")
        self.assertIsNone(detail["credential"]["current"])
        # 历史链接、时间和封存结论仍保留
        self.assertEqual(
            detail["credential"]["sealed"]["result"]["summary"]
            ["publishable_seconds"], 600)
        item = [i for i in channels.list_distributions(self.conn)
                if i["id"] == d["id"]][0]
        self.assertEqual(item["credential"]["risk"], channels.RISK_MISSING)

    def test_unknown_distribution_returns_none(self):
        self.assertIsNone(channels.distribution_detail(self.conn, 9999))

    # ---------------------------------------------------------- 撤下

    def test_withdraw_requires_reason(self):
        d, _ = self.register()
        result, status = channels.withdraw_distribution(self.conn, d["id"], "  ")
        self.assertEqual(status, 400)
        row = self.conn.execute(
            "SELECT * FROM channel_distributions WHERE id = ?",
            (d["id"],)).fetchone()
        self.assertEqual(row["status"], "active")

    def test_withdraw_marks_record_and_keeps_time_risk_and_credential(self):
        d, _ = self.register()
        result, status = channels.withdraw_distribution(
            self.conn, d["id"], "授权方要求撤下")
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], channels.DIST_WITHDRAWN)
        self.assertEqual(result["status_label"], "已撤下")
        self.assertEqual(result["withdraw_reason"], "授权方要求撤下")
        self.assertIsNotNone(result["withdrawn_at"])
        self.assertEqual(result["registered_at"], d["registered_at"])
        # 渠道、链接等登记信息不变
        self.assertEqual(result["channel"], CHANNEL)
        self.assertEqual(result["url"], URL)
        # 撤下后风险提示仍可查，凭据不改动
        self.assertEqual(result["credential"]["risk"], channels.RISK_OK)
        sealed = json.loads(self.conn.execute(
            "SELECT result FROM release_credentials WHERE id = ?",
            (self.cid,)).fetchone()["result"])
        self.assertEqual(sealed["summary"]["publishable_seconds"], 600)

    def test_withdraw_twice_rejected(self):
        d, _ = self.register()
        self.assertEqual(channels.withdraw_distribution(
            self.conn, d["id"], "原因")[1], 200)
        result, status = channels.withdraw_distribution(
            self.conn, d["id"], "再撤一次")
        self.assertEqual(status, 409)
        self.assertIn("已撤下", result["error"])
        # 撤下原因与时间不被覆盖
        row = self.conn.execute(
            "SELECT * FROM channel_distributions WHERE id = ?",
            (d["id"],)).fetchone()
        self.assertEqual(row["withdraw_reason"], "原因")

    def test_withdraw_unknown_returns_404(self):
        self.assertEqual(channels.withdraw_distribution(
            self.conn, 9999, "原因")[1], 404)

    def test_withdraw_when_source_later_missing_still_possible(self):
        d, _ = self.register()
        self.conn.execute("DELETE FROM interviews WHERE id = 1")
        self.conn.commit()
        result, status = channels.withdraw_distribution(
            self.conn, d["id"], "来源访谈删除，预防性撤下")
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "withdrawn")
        self.assertEqual(result["credential"]["risk"], channels.RISK_MISSING)

    # ---------------------------------------------------------- 台账不可篡改

    def test_distribution_immutable_and_undeletable(self):
        d, _ = self.register()
        # 不可删除
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM channel_distributions")
        self.conn.rollback()
        # 登记信息不可改
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE channel_distributions SET channel = '篡改渠道'")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE channel_distributions SET url = 'https://evil'")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE channel_distributions SET registered_at = '2000-01-01'")
        self.conn.rollback()
        # 合法撤下可以
        self.conn.execute(
            "UPDATE channel_distributions SET status = 'withdrawn',"
            " withdrawn_at = datetime('now'), withdraw_reason = 'x'"
            " WHERE id = ?", (d["id"],))
        self.conn.commit()
        # 已撤下不可再改
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE channel_distributions SET withdraw_reason = 'y'")
        self.conn.rollback()
        self.assertEqual(self.dist_count(), 1)


if __name__ == "__main__":
    unittest.main()
