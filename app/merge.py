"""把另一份复盘某可回看版本的事件链并入当前复盘（预览/确认共用的纯规划逻辑）。

来源取**不可变输入快照**：复制来的事件保留时间范围（含 UTC 偏移）、持续
分钟、备注与 IANA 时区；“先于”关系仅复制**两端事件均入选**的那些，并保留
最短/最长等待。复制来的事件在目标中取得**全新的持续身份（uid）**，绝不沿用
来源 uid；目标原有事件与来源复盘的历史均不变。

本模块只做规划与推算，不写库：
  - build_plan(...)  组装合并输入、运行推算，产出预览数据与 digest；
  - save_payload(plan) 生成写库用的整体替换输入（客户端 key 形式）。

预览与确认走同一份规划：确认时按目标 base_version 重新取数重建 plan，
digest 一致才接受——只要目标版本未变化，合并输入就与预览完全相同，推算
结论（compute 为纯函数）必然与预览一致。
"""

import hashlib
import json

import compute


class MergeError(ValueError):
    """并入输入不合法（来源/目标相同、事件不存在、选择为空或重复等）。"""


def target_token(event_id):
    """推算时目标事件使用的稳定标识（与真实行 id 对应，便于映射回名称）。"""
    return "t_{}".format(event_id)


def source_token(uid):
    """推算时复制事件使用的稳定标识（来源持续身份只在预览内部可见）。"""
    return "s_{}".format(uid)


def _canon_event(token, e):
    """参与 digest 与推算的规范化事件（不含 key/uid 等可变身份）。"""
    return {
        "token": token,
        "name": e.get("name"),
        "earliest_start": e.get("earliest_start"),
        "latest_start": e.get("latest_start"),
        "duration_minutes": e.get("duration_minutes"),
        "note": e.get("note") or "",
        "timezone": e.get("timezone"),
    }


def _gap_min(r):
    g = r.get("min_gap_minutes")
    return 0 if g is None else int(g)


def _gap_max(r):
    m = r.get("max_gap_minutes")
    return None if m is None else int(m)


def build_plan(target_snapshot_events, target_live_events, target_live_relations,
               source_data, selected_uids):
    """按选择组装合并规划并推算。

    target_snapshot_events : _current_input 给出的目标快照式事件
                             （key=id_<行id>、uid、时间/持续/备注/时区）
    target_live_events     : 目标 live 事件行（数字 id）
    target_live_relations  : 目标 live 关系行（数字 id 与间隔上下界）
    source_data            : 来源版本的不可变回看数据（含 input 快照）
    selected_uids          : 入选来源事件的持续身份列表

    返回规划 dict（copied_* / compute_* / result / digest 等，见模块函数）。
    选择为空、重复、标识非法或来源快照中找不到对应事件时抛 MergeError。
    """
    if not isinstance(selected_uids, list) or not selected_uids:
        raise MergeError("请至少选择一个要并入的来源事件")
    norm_uids = []
    for u in selected_uids:
        if not isinstance(u, str) or not u.strip():
            raise MergeError("入选事件标识不合法：事件集合必须是非空字符串的 uid 列表")
        norm_uids.append(u.strip())
    if len(set(norm_uids)) != len(norm_uids):
        raise MergeError("入选事件存在重复，请去重后再试")

    src_events = source_data.get("input", {}).get("events", [])
    src_relations = source_data.get("input", {}).get("relations", [])
    src_by_uid = {}
    for e in src_events:
        uid = e.get("uid")
        if not uid:
            # 升级前保存的快照没有可靠身份：不能可靠选取，拒绝且不写任何数据
            raise MergeError("来源版本快照中的事件缺少可靠持续身份标识，不可并入")
        src_by_uid[uid] = e
    missing = [u for u in norm_uids if u not in src_by_uid]
    if missing:
        raise MergeError("入选事件在来源版本中不存在：{}".format("、".join(missing[:5])))
    selected_set = set(norm_uids)

    # 复制事件按来源快照中的出现顺序排列（与客户端选择顺序无关，保证可复现）
    copied_events = [e for e in src_events if e.get("uid") in selected_set]

    # 仅复制两端均入选的“先于”关系，同样按来源快照顺序，保留等待上下界
    uid_by_key = {e.get("key"): e.get("uid") for e in src_events}
    copied_relations = []
    for r in src_relations:
        ua = uid_by_key.get(r.get("predecessor_key"))
        ub = uid_by_key.get(r.get("successor_key"))
        if ua in selected_set and ub in selected_set:
            copied_relations.append({
                "predecessor_uid": ua,
                "successor_uid": ub,
                "min_gap_minutes": _gap_min(r),
                "max_gap_minutes": _gap_max(r),
            })

    # ---- 合并后的推算输入（目标行 + 复制事件；标识只在本次推算内有效）----
    compute_events = []
    for e in target_live_events:
        ce = _canon_event(target_token(e["id"]), e)
        ce["id"] = ce.pop("token")
        compute_events.append(ce)
    for e in copied_events:
        ce = _canon_event(source_token(e["uid"]), e)
        ce["id"] = ce.pop("token")
        compute_events.append(ce)

    compute_relations = []
    for r in target_live_relations:
        compute_relations.append({
            "id": "tr_{}".format(r["id"]),
            "predecessor_id": target_token(r["predecessor_id"]),
            "successor_id": target_token(r["successor_id"]),
            "min_gap_minutes": _gap_min(r),
            "max_gap_minutes": _gap_max(r),
        })
    for i, r in enumerate(copied_relations):
        compute_relations.append({
            "id": "cr_{}".format(i),
            "predecessor_id": source_token(r["predecessor_uid"]),
            "successor_id": source_token(r["successor_uid"]),
            "min_gap_minutes": r["min_gap_minutes"],
            "max_gap_minutes": r["max_gap_minutes"],
        })

    result = compute.compute(compute_events, compute_relations)

    # ---- digest：预览与确认核对同一合并输入的依据 -------------------------
    basis = {
        "target_events": [_canon_event(target_token(e["id"]), e)
                          for e in target_live_events],
        "target_relations": [{
            "token": "tr_{}".format(r["id"]),
            "predecessor": target_token(r["predecessor_id"]),
            "successor": target_token(r["successor_id"]),
            "min_gap_minutes": _gap_min(r),
            "max_gap_minutes": _gap_max(r),
        } for r in target_live_relations],
        "source_events": [_canon_event(source_token(e["uid"]), e)
                          for e in copied_events],
        "source_relations": [{
            "token": "cr_{}".format(i),
            "predecessor": source_token(r["predecessor_uid"]),
            "successor": source_token(r["successor_uid"]),
            "min_gap_minutes": r["min_gap_minutes"],
            "max_gap_minutes": r["max_gap_minutes"],
        } for i, r in enumerate(copied_relations)],
    }
    digest = hashlib.sha256(
        json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")).hexdigest()

    return {
        "target_snapshot_events": target_snapshot_events,
        "target_live_events": target_live_events,
        "target_live_relations": target_live_relations,
        "copied_events": copied_events,
        "copied_relations": copied_relations,
        "compute_events": compute_events,
        "compute_relations": compute_relations,
        "result": result,
        "digest": digest,
    }


def save_payload(plan, source_meta=None):
    """由规划生成写库用的整体替换输入（客户端 key 形式）。

    目标事件沿用原 key（id_<行id>）与 uid；复制事件使用新 key 且**不带
    uid**——写库时由服务端分配全新的持续身份，因此复制事件在目标中绝不
    沿用来源身份。目标关系与复制来的关系统一改按新 key 引用。

    source_meta 给定时（并入确认，由 db 层传入），每个复制事件附带嵌套
    origin 来源摘要：{source_review_id, source_review_title, source_version,
    source_event_uid, source_event_name}。该摘要与复制事件在目标取得的
    全新 uid 绑定，之后随身份保留；它不参与 digest，也不影响推算。
    """
    def origin_for(src_uid):
        if not source_meta:
            return None
        name = source_meta.get("event_names", {}).get(src_uid)
        return {
            "source_review_id": source_meta["review_id"],
            "source_review_title": source_meta["review_title"],
            "source_version": source_meta["version"],
            "source_event_uid": src_uid,
            "source_event_name": name,
        }

    token_to_key = {}
    events = []
    for e in plan["target_snapshot_events"]:
        events.append({
            "key": e["key"],
            "uid": e.get("uid") or "",
            "name": e["name"],
            "earliest_start": e.get("earliest_start"),
            "latest_start": e.get("latest_start"),
            "duration_minutes": e.get("duration_minutes"),
            "note": e.get("note") or "",
            "timezone": e.get("timezone"),
        })
        token_to_key[target_token(str(e["key"])[3:])] = e["key"]

    for i, e in enumerate(plan["copied_events"]):
        new_key = "m_{}_{}".format(i, str(e["uid"])[:8])
        token_to_key[source_token(e["uid"])] = new_key
        item = {
            "key": new_key,
            "uid": "",  # 强制在目标分配新持续身份
            "name": e.get("name"),
            "earliest_start": e.get("earliest_start"),
            "latest_start": e.get("latest_start"),
            "duration_minutes": e.get("duration_minutes"),
            "note": e.get("note") or "",
            "timezone": e.get("timezone"),
        }
        origin = origin_for(e["uid"])
        if origin is not None:
            # 来源摘要随复制事件固化；与新 uid 绑定，来源被删/改名后仍可读
            item["origin"] = origin
        events.append(item)

    relations = []
    for r in plan["target_live_relations"]:
        relations.append({
            "predecessor_key": "id_{}".format(r["predecessor_id"]),
            "successor_key": "id_{}".format(r["successor_id"]),
            "min_gap_minutes": _gap_min(r),
            "max_gap_minutes": _gap_max(r),
        })
    for i, r in enumerate(plan["copied_relations"]):
        relations.append({
            "predecessor_key": token_to_key[source_token(r["predecessor_uid"])],
            "successor_key": token_to_key[source_token(r["successor_uid"])],
            "min_gap_minutes": r["min_gap_minutes"],
            "max_gap_minutes": r["max_gap_minutes"],
        })
    return events, relations
