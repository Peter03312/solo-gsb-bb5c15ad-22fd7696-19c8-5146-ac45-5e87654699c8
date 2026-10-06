"""同一复盘两个版本的对照（纯函数，只读）。

配对原则：跨版本按事件的持续身份（uid）配对。改名、恢复产生的版本沿用
同一 uid，因此仍能识别为同一事件；绝不按名称或会重建的行号猜配。
快照中缺少 uid 的事件（本轮升级前保存的输入快照）没有可靠身份，
此时只展示各版本原貌，不虚构逐事件差值。

输入 from_data / to_data 为 db 层组装的两版数据：
  {version, kind, state, title, status, computed_at, input, result}
其中 input 为不可变快照 {events, relations}（事件含 key 与 uid），
result 为当时推算结果（windows 以当时行号为键，快照 key 形如 id_<行号>）。
"""

# 参与对照的输入字段：(字段名, 展示名)
INPUT_FIELDS = [
    ("earliest_start", "最早开始"),
    ("latest_start", "最晚开始"),
    ("duration_minutes", "持续分钟数"),
    ("timezone", "IANA 时区"),
    ("note", "备注"),
]

# 参与对照的推算窗口字段：(字段名, 展示名)
WINDOW_FIELDS = [
    ("earliest_start", "最早可行开始"),
    ("latest_start", "最晚可行开始"),
    ("earliest_end", "最早可行结束"),
    ("latest_end", "最晚可行结束"),
]

UNPAIRABLE_REASON = (
    "版本快照中的事件缺少可靠的持续身份标识（该快照由旧版本保存），"
    "无法跨版本逐事件配对，仅展示各版本原貌，不提供逐事件差值"
)


def _is_draft(e):
    """暂存判定与推算引擎口径一致：缺最早/最晚开始或持续分钟数。"""
    return (not e.get("earliest_start")) or (not e.get("latest_start")) \
        or e.get("duration_minutes") is None


def _window_key(event):
    """快照事件 key（id_<当时行号>）→ 推算结果 windows 的键。"""
    key = str(event.get("key") or "")
    return key[3:] if key.startswith("id_") else key


def _window_of(version_data, event):
    """该事件在此版本推算结果中的可行窗口（仅对照所需字段）；暂存无窗口返回 None。"""
    w = version_data["result"].get("windows", {}).get(_window_key(event))
    if not w:
        return None
    return {field: w.get(field) for field, _ in WINDOW_FIELDS}


def _version_meta(version_data):
    return {
        "version": version_data["version"],
        "kind": version_data.get("kind"),
        "state": version_data.get("state"),
        "title": version_data.get("title"),
        "status": version_data.get("status"),
        "computed_at": version_data.get("computed_at"),
    }


def _event_brief(event, version_data):
    """事件单版本原貌（新增/删除条目用）。"""
    item = {
        "uid": event.get("uid"),
        "name": event.get("name"),
        "earliest_start": event.get("earliest_start"),
        "latest_start": event.get("latest_start"),
        "duration_minutes": event.get("duration_minutes"),
        "timezone": event.get("timezone"),
        "note": event.get("note") or "",
        "draft": _is_draft(event),
        "window": _window_of(version_data, event),
    }
    origin = event.get("origin")
    if isinstance(origin, dict):
        item["origin"] = origin  # 并入事件：展示保存的来源摘要（仅展示，不参与差值）
    return item


def _diff_event(uid, from_event, to_event, from_data, to_data):
    """同一身份事件的两版差值；无任何变化返回 None。"""
    input_changes = {}
    for field, _label in INPUT_FIELDS:
        if from_event.get(field) != to_event.get(field):
            input_changes[field] = {
                "from": from_event.get(field), "to": to_event.get(field)}
    from_draft, to_draft = _is_draft(from_event), _is_draft(to_event)
    from_win = _window_of(from_data, from_event)
    to_win = _window_of(to_data, to_event)
    window_changes = {}
    if from_win and to_win:
        for field, _label in WINDOW_FIELDS:
            if from_win.get(field) != to_win.get(field):
                window_changes[field] = {
                    "from": from_win.get(field), "to": to_win.get(field)}
    if (not input_changes and not window_changes
            and from_draft == to_draft
            and from_event.get("name") == to_event.get("name")):
        return None
    entry = {
        "uid": uid,
        "name": {"from": from_event.get("name"), "to": to_event.get("name")},
        "draft": {"from": from_draft, "to": to_draft},
        "input_changes": input_changes,
        "window": {"from": from_win, "to": to_win},
        "window_changes": window_changes,
    }
    # 来源摘要随持续身份走：两端都有时以较晚版本为准展示；来源变化不作为
    # “输入差值”（来源摘要本就不应被改写），只在条目上展示保存的摘要。
    origin = to_event.get("origin") or from_event.get("origin")
    if isinstance(origin, dict):
        entry["origin"] = origin
    return entry


def _relations_by_pair(events, relations):
    """关系按（前项 uid, 后项 uid）归组，保留快照中的出现顺序。

    关系身份由两端事件的持续身份决定；间隔设置（最短/最长等待）是关系的
    可比较属性。引用不到的事件（不应发生）跳过，不虚构配对。
    """
    uid_by_key = {e.get("key"): e.get("uid") for e in events}
    pairs = {}
    for r in relations:
        pa = uid_by_key.get(r.get("predecessor_key"))
        pb = uid_by_key.get(r.get("successor_key"))
        if pa is None or pb is None:
            continue
        pairs.setdefault((pa, pb), []).append({
            "min_gap_minutes": r.get("min_gap_minutes") or 0,
            "max_gap_minutes": r.get("max_gap_minutes"),
        })
    return pairs


def _relation_entry(pair, names):
    pa, pb = pair
    return {
        "predecessor": {"uid": pa, "name": names.get(pa)},
        "successor": {"uid": pb, "name": names.get(pb)},
    }


def _diff_relations(from_data, to_data):
    f_events = from_data["input"].get("events", [])
    t_events = to_data["input"].get("events", [])
    f_pairs = _relations_by_pair(f_events, from_data["input"].get("relations", []))
    t_pairs = _relations_by_pair(t_events, to_data["input"].get("relations", []))
    # 展示名称：优先较晚版本，被删事件回退到较早版本
    names = {e.get("uid"): e.get("name") for e in f_events}
    names.update({e.get("uid"): e.get("name") for e in t_events})

    added, removed, changed = [], [], []
    ordered_pairs = list(f_pairs) + [p for p in t_pairs if p not in f_pairs]
    for pair in ordered_pairs:
        fs = f_pairs.get(pair, [])
        ts = t_pairs.get(pair, [])
        for i in range(max(len(fs), len(ts))):
            f = fs[i] if i < len(fs) else None
            t = ts[i] if i < len(ts) else None
            entry = _relation_entry(pair, names)
            if f is not None and t is not None:
                if f != t:
                    changed.append({**entry, "from": f, "to": t})
            elif t is not None:
                added.append({**entry, "to": t})
            else:
                removed.append({**entry, "from": f})
    return {"added": added, "removed": removed, "changed": changed}


def _conflicts_view(version_data):
    """该版本冲突明细：涉及事件翻译为持续身份 + 名称，不虚构跨版本配对。"""
    by_rowid = {}
    for e in version_data["input"].get("events", []):
        by_rowid[_window_key(e)] = e
    out = []
    for c in version_data["result"].get("conflicts", []):
        out.append({
            "type": c.get("type"),
            "message": c.get("message"),
            "events": [
                {"uid": by_rowid[str(i)].get("uid"), "name": by_rowid[str(i)].get("name")}
                for i in c.get("event_ids", []) if str(i) in by_rowid
            ],
        })
    return out


def build_compare(from_data, to_data):
    """对照两个版本，返回只读差值视图（不改动任何输入与版本状态）。"""
    f_events = from_data["input"].get("events", [])
    t_events = to_data["input"].get("events", [])
    out = {
        "from": _version_meta(from_data),
        "to": _version_meta(to_data),
        "title_changed": from_data.get("title") != to_data.get("title"),
    }

    # 任一版本存在缺可靠身份（uid）的事件：只展示各版本原貌，不虚构差值
    if not all(e.get("uid") for e in f_events + t_events):
        out["pairable"] = False
        out["reason"] = UNPAIRABLE_REASON
        out["from"]["input"] = from_data["input"]
        out["from"]["result"] = from_data["result"]
        out["to"]["input"] = to_data["input"]
        out["to"]["result"] = to_data["result"]
        return out

    f_by_uid = {e["uid"]: e for e in f_events}
    t_by_uid = {e["uid"]: e for e in t_events}
    f_uids, t_uids = set(f_by_uid), set(t_by_uid)

    added = [_event_brief(e, to_data) for e in t_events if e["uid"] not in f_uids]
    removed = [_event_brief(e, from_data) for e in f_events if e["uid"] not in t_uids]
    renamed, changed, unchanged = [], [], []
    for e in t_events:  # 共同事件按较晚版本顺序展示
        uid = e["uid"]
        if uid not in f_uids:
            continue
        fe, te = f_by_uid[uid], e
        if fe.get("name") != te.get("name"):
            rename = {"uid": uid, "from": fe.get("name"), "to": te.get("name")}
            origin = te.get("origin") or fe.get("origin")
            if isinstance(origin, dict):
                rename["origin"] = origin
            renamed.append(rename)
        entry = _diff_event(uid, fe, te, from_data, to_data)
        if entry is None:
            same = {"uid": uid, "name": te.get("name")}
            if isinstance(te.get("origin"), dict):
                same["origin"] = te["origin"]
            unchanged.append(same)
        else:
            changed.append(entry)

    relations = _diff_relations(from_data, to_data)

    # 暂存状态变化（仅统计两版都存在的事件；新增/删除事件的暂存标记
    # 已在 added/removed 条目的 draft 字段体现）
    f_drafts = {u for u in f_uids if _is_draft(f_by_uid[u])}
    t_drafts = {u for u in t_uids if _is_draft(t_by_uid[u])}
    common = f_uids & t_uids
    drafts = {
        "resolved": [{"uid": u, "name": t_by_uid[u].get("name")}
                     for u in t_by_uid if u in common and u in f_drafts and u not in t_drafts],
        "became": [{"uid": u, "name": t_by_uid[u].get("name")}
                   for u in t_by_uid if u in common and u not in f_drafts and u in t_drafts],
        "remaining": [{"uid": u, "name": t_by_uid[u].get("name")}
                      for u in t_by_uid if u in common and u in f_drafts and u in t_drafts],
        "from_count": len(f_drafts),
        "to_count": len(t_drafts),
    }

    conflicts_from = _conflicts_view(from_data)
    conflicts_to = _conflicts_view(to_data)

    out.update({
        "pairable": True,
        "summary": {
            "events_added": len(added),
            "events_removed": len(removed),
            "events_renamed": len(renamed),
            "events_changed": len(changed),
            "events_unchanged": len(unchanged),
            "relations_added": len(relations["added"]),
            "relations_removed": len(relations["removed"]),
            "relations_changed": len(relations["changed"]),
            "conflicts_from": len(conflicts_from),
            "conflicts_to": len(conflicts_to),
            "drafts_from": drafts["from_count"],
            "drafts_to": drafts["to_count"],
        },
        "events": {
            "added": added,
            "removed": removed,
            "renamed": renamed,
            "changed": changed,
            "unchanged": unchanged,
        },
        "relations": relations,
        "conflicts": {"from": conflicts_from, "to": conflicts_to},
        "drafts": drafts,
    })
    return out
