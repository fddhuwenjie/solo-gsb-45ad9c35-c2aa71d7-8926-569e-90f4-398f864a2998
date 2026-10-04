"""渠道投放台账：为已封存的发行凭据登记渠道投放。

事务编排与 HTTP 层分离：本模块只依赖 sqlite3 连接（行需为
sqlite3.Row），不依赖 Flask，可独立测试。

登记（create_distribution）：录入凭据 ID、渠道名与投放链接后，在同一
SQLite 写事务（BEGIN IMMEDIATE）内复核来源 —— 仅当原访谈仍在、
资料与封存时一致且当前整段时间轴可发布时，才保存「投放中」记录及
登记时间；否则回滚、不留记录，并说明原因（来源缺失 / 资料有变化 /
当前存在不可发布区间）。

列表与详情（list_distributions / distribution_detail）：在一致的读取
快照（同一只读事务）中展示投放状态与凭据当前风险 —— 资料后来变化即
提示「需处理」（即便仍可发布）；原访谈删除则提示「来源缺失」，
历史链接、登记时间与封存结论仍保留。

撤下（withdraw_distribution）：确认渠道内容已撤下后填写原因，
将「投放中」记录标记为「已撤下」并保留撤下时间；风险提示仍可查，
凭据本身不改动。台账记录不可删除，已撤下记录不可再改。
"""
from urllib.parse import urlparse

import credentials

# 投放状态
DIST_ACTIVE = "active"      # 投放中
DIST_WITHDRAWN = "withdrawn"  # 已撤下

DIST_STATUS_LABELS = {
    DIST_ACTIVE: "投放中",
    DIST_WITHDRAWN: "已撤下",
}

# 凭据当前风险（依据一致读取快照中的复核结果）
RISK_OK = "ok"              # 与发行时一致，可发布
RISK_ACTION = "action"      # 需处理：资料有变化（即便仍可发布）
RISK_MISSING = "missing"    # 来源缺失：原访谈已删除

RISK_LABELS = {
    RISK_OK: "与发行时一致",
    RISK_ACTION: "需处理",
    RISK_MISSING: "来源缺失",
}

# 登记被拒原因（不留记录）
REASON_MISSING = "source_missing"        # 原访谈已删除
REASON_CHANGED = "source_changed"        # 资料与封存时不一致
REASON_BLOCKED = "currently_blocked"     # 当前整段并非全部可发布
REASON_DUPLICATE = "duplicate_active"    # 同一凭据在该渠道已有投放中记录


def validate_placement(raw_channel, raw_url):
    """校验渠道名与投放链接，返回 (values, error)。

    合法时 values 为 {"channel": ..., "url": ...}；非法时 error 为中文原因。
    """
    channel = str(raw_channel or "").strip()
    url = str(raw_url or "").strip()
    if not channel:
        return None, "渠道名不能为空"
    if not url:
        return None, "投放链接不能为空"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, "投放链接需为 http(s):// 开头的完整链接"
    return {"channel": channel, "url": url}, None


def _distribution_dict(row):
    return {
        "id": row["id"],
        "credential_id": row["credential_id"],
        "channel": row["channel"],
        "url": row["url"],
        "status": row["status"],
        "status_label": DIST_STATUS_LABELS[row["status"]],
        "registered_at": row["registered_at"],
        "withdrawn_at": row["withdrawn_at"],
        "withdraw_reason": row["withdraw_reason"],
    }


def _risk_of(review):
    """由凭据复核结果得出台账风险字段（review 为 None 表示来源缺失）。"""
    if review is None or review["status"] == credentials.STATUS_MISSING:
        code = RISK_MISSING
    elif review["status"] == credentials.STATUS_REVIEW:
        # 资料有任何变化即「需处理」—— 即便重新校验仍可发布
        code = RISK_ACTION
    else:
        code = RISK_OK
    return {"risk": code, "risk_label": RISK_LABELS[code]}


def _credential_brief(review):
    """列表用的凭据摘要（含当前风险）。"""
    return {
        "id": review["id"],
        "interview_id": review["interview_id"],
        "interview_title": review["interview_title"],
        "medium": review["medium"],
        "publication_date": review["publication_date"],
        "created_at": review["created_at"],
        "status": review["status"],
        "status_label": review["status_label"],
        "changes": review["changes"],
        **_risk_of(review),
    }


def _credential_block(review):
    """详情用的凭据视图：封存结论 + 一致快照中的当前风险复核。"""
    return {
        "id": review["id"],
        "interview_id": review["interview_id"],
        "interview_title": review["interview_title"],
        "medium": review["medium"],
        "publication_date": review["publication_date"],
        "created_at": review["created_at"],
        "status": review["status"],
        "status_label": review["status_label"],
        "changes": review["changes"],
        **_risk_of(review),
        "sealed": review["sealed"],
        "current": review["current"],
    }


def create_distribution(conn, credential_id, channel, url):
    """在同一写事务中复核来源并登记渠道投放。

    仅当原访谈仍在、资料与封存时一致且当前整段可发布时，
    才写入「投放中」记录（含登记时间）并提交；否则回滚不留记录。

    返回 (result, status)：
        201 —— 已登记，result 为台账详情（含凭据风险与封存结论）；
        404 —— 凭据不存在；
        409 —— 复核未通过或该渠道已有投放中记录，未落任何记录
               （result.reason 标明原因；来源在时附 credential 复核视图）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        credential = credentials.load_credential(conn, credential_id)
        if credential is None:
            conn.rollback()
            return {"error": "凭据不存在"}, 404
        review = credentials.review_loaded(conn, credential)
        if review["status"] == credentials.STATUS_MISSING:
            conn.rollback()
            return {
                "created": False,
                "reason": REASON_MISSING,
                "error": "来源缺失：原访谈已删除，无法复核当前可发布性，"
                         "未登记投放记录",
                "credential": _credential_block(review),
            }, 409
        if review["status"] == credentials.STATUS_REVIEW:
            conn.rollback()
            return {
                "created": False,
                "reason": REASON_CHANGED,
                "error": "资料与封存时不一致（变化：%s），未登记投放记录"
                         % ("、".join(review["changes"]) or "未知"),
                "changes": review["changes"],
                "credential": _credential_block(review),
            }, 409
        # 资料变化优先判定（当前阻断必然源于授权等资料变化）；
        # currently_blocked 作为防御性分支保留，并随结果附阻断区间。
        blocked = review["current"]["blocked"]
        if blocked and review["status"] == credentials.STATUS_VALID:
            conn.rollback()
            return {
                "created": False,
                "reason": REASON_BLOCKED,
                "error": "当前存在 %d 个不可发布区间，整段并非全部可发布，"
                         "未登记投放记录" % len(blocked),
                "blocked": blocked,
                "credential": _credential_block(review),
            }, 409
        dup = conn.execute(
            "SELECT id FROM channel_distributions WHERE credential_id = ?"
            " AND channel = ? AND status = ?",
            (credential_id, channel, DIST_ACTIVE)).fetchone()
        if dup is not None:
            conn.rollback()
            return {
                "created": False,
                "reason": REASON_DUPLICATE,
                "error": "该凭据在渠道「%s」已有投放中记录（台账 #%d），"
                         "请勿重复登记" % (channel, dup["id"]),
                "existing_id": dup["id"],
            }, 409
        cur = conn.execute(
            "INSERT INTO channel_distributions (credential_id, channel, url)"
            " VALUES (?, ?, ?)",
            (credential_id, channel, url))
        row = conn.execute(
            "SELECT * FROM channel_distributions WHERE id = ?",
            (cur.lastrowid,)).fetchone()
        conn.commit()
        distribution = _distribution_dict(row)
    except Exception:
        conn.rollback()
        raise
    return {**distribution, "credential": _credential_block(review)}, 201


def _snapshot_read(conn):
    """开启一致读取快照（只读事务）。"""
    conn.execute("BEGIN")


def distribution_detail(conn, distribution_id):
    """台账详情：投放状态 + 凭据当前风险（一致读取快照）。

    返回详情视图；台账记录不存在时返回 None。
    """
    _snapshot_read(conn)
    try:
        row = conn.execute(
            "SELECT * FROM channel_distributions WHERE id = ?",
            (distribution_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        detail = _distribution_dict(row)
        credential = credentials.load_credential(conn, row["credential_id"])
        # 台账不删、凭据不删（触发器保证），credential 必在；稳妥起见仍兜底
        detail["credential"] = _credential_block(
            credentials.review_loaded(conn, credential))
        conn.rollback()  # 只读事务：释放快照
    except Exception:
        conn.rollback()
        raise
    return detail


def list_distributions(conn):
    """列出全部投放记录（同一事务快照内逐条复核凭据风险）。"""
    _snapshot_read(conn)
    try:
        rows = conn.execute(
            "SELECT * FROM channel_distributions ORDER BY id").fetchall()
        items = []
        for row in rows:
            credential = credentials.load_credential(conn, row["credential_id"])
            review = credentials.review_loaded(conn, credential)
            items.append({
                **_distribution_dict(row),
                "credential": _credential_brief(review),
            })
        conn.rollback()  # 只读事务：释放快照
    except Exception:
        conn.rollback()
        raise
    return items


def withdraw_distribution(conn, distribution_id, reason):
    """将投放记录标记为已撤下（仅「投放中」可撤下）。

    撤下原因必填；撤下时间由数据库写入。只更新台账行，凭据不改动。
    返回 (result, status)：
        200 —— 已撤下，result 为台账详情（含凭据当前风险与封存结论）；
        404 —— 台账记录不存在；
        409 —— 记录已是「已撤下」状态，不能重复撤下。
    """
    reason = str(reason or "").strip()
    if not reason:
        return {"error": "撤下原因不能为空"}, 400
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM channel_distributions WHERE id = ?",
            (distribution_id,)).fetchone()
        if row is None:
            conn.rollback()
            return {"error": "投放记录不存在"}, 404
        if row["status"] == DIST_WITHDRAWN:
            conn.rollback()
            return {
                "error": "该投放记录已撤下，不能重复操作",
                "withdrawn_at": row["withdrawn_at"],
            }, 409
        conn.execute(
            "UPDATE channel_distributions SET status = ?,"
            " withdrawn_at = datetime('now'), withdraw_reason = ?"
            " WHERE id = ?",
            (DIST_WITHDRAWN, reason, distribution_id))
        updated = conn.execute(
            "SELECT * FROM channel_distributions WHERE id = ?",
            (distribution_id,)).fetchone()
        credential = credentials.load_credential(conn, row["credential_id"])
        review = credentials.review_loaded(conn, credential)
        conn.commit()
        distribution = _distribution_dict(updated)
    except Exception:
        conn.rollback()
        raise
    return {**distribution, "credential": _credential_block(review)}, 200
