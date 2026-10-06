"""版本冲突的最小不可满足约束分析（纯函数，只读）。

复盘成员查看某个**可回看版本**的冲突时，本模块按该版本的**不可变输入
快照**定位一组（或多组相互独立的）**不可再缩减的冲突约束**：

  - 整组约束不可满足；
  - 移去其中任一条后，该组其余约束立即可满足（极小不可满足子集，MUS）。

建模为差分约束系统（difference constraints）
-------------------------------------------

设每个参与推算（非暂存）事件的开始时刻为变量 x_i（分钟）：

  输入最早/最晚开始边界（引入固定锚点 x_0 = 0）：
      x_i >= E_i          ⇔   x_0 - x_i ≤ -E_i     （边 i → 0，权 -E_i）
      x_i ≤  L_i          ⇔   x_i - x_0 ≤  L_i     （边 0 → i，权  L_i）

  “A 先于 B”（A 持续 d_A 分钟，最短等待 g、最长等待 m）：
      x_B ≥ x_A + d_A + g ⇔   x_A - x_B ≤ -(d_A+g) （边 B → A，权 -(d_A+g)）
      x_B ≤ x_A + d_A + m ⇔   x_B - x_A ≤  (d_A+m) （边 A → B，权  (d_A+m)，m 可空）

差分约束系统可满足 ⇔ 约束图中**不存在负权环**（Bellman-Ford 判定）。
一个**简单负环**本身就是极小不可满足约束集：环上每条边恰好出现一次，
删去环上任意一条边后环即断开、该子集无环（因而可满足）。提取后另做
一次删除式收缩兜底，确保返回的每一组都严格满足“去掉任一条即可满足”。

时区
----

带时区事件的边界先折算为 **UTC 绝对分钟**再参与判定（持续时长与最短/
最长等待本就以绝对分钟施加），与各事件所在时区、当日夏令时无关；输出
保留事件输入快照中的**当地时间与 UTC 偏移**。未带时区事件沿用朴素本地
时间语义。两种事件之间不允许建立“先于”关系（写库时已拒绝，此处兜底
跳过并说明原因）。

暂存事件与被跳过关系沿用 compute 引擎口径（直接调用 compute 取得判定
结果），绝不参与约束图，并在输出中说明排除原因。无冲突时返回空组。
同一版本快照下结果稳定：边按快照中的事件/关系顺序固定编号，松弛取
严格小于、同权不更新，环按最小约束编号旋转到确定起点。
"""

from datetime import datetime, timezone

import compute

_EPOCH_UTC = datetime(1971, 1, 1, tzinfo=timezone.utc)
_EPOCH_NAIVE = datetime(1971, 1, 1)

TYPE_LABELS = {
    "earliest_bound": "边界·最早开始",
    "latest_bound": "边界·最晚开始",
    "min_wait": "最短等待（含前项持续）",
    "max_wait": "最长等待（含前项持续）",
}


# ------------------------------------------------------------------ 工具

def _gap_min(r):
    g = r.get("min_gap_minutes")
    return 0 if g is None else int(g)


def _gap_max(r):
    m = r.get("max_gap_minutes")
    return None if m is None else int(m)


def _to_minutes(dt):
    """datetime → 分钟数。

    带时区按 UTC 绝对分钟；朴素时间按朴素纪元分钟（沿用原语义，
    子系统内比较与锚点边界都相对同一固定参考点）。
    """
    if dt.tzinfo is not None:
        return int((dt - _EPOCH_UTC).total_seconds() // 60)
    return int((dt - _EPOCH_NAIVE).total_seconds() // 60)


def _is_draft_event(e):
    """与 compute / compare 的暂存口径保持一致。"""
    return (not e.get("earliest_start")) or (not e.get("latest_start")) \
        or e.get("duration_minutes") is None


def _event_brief(e):
    return {
        "key": e.get("key"),
        "uid": e.get("uid"),
        "name": e.get("name"),
        "timezone": e.get("timezone"),
    }


# ------------------------------------------------------------------ 建图

def _build_graph(snapshot_events, snapshot_relations, drafts):
    """把不可变快照构建为差分约束图。

    返回 (nodes, edges, active_events)：
      nodes         节点序号 → 快照事件（节点 0 为固定锚点）
      edges         [{id, u, v, w, meta}]，顺序即快照中的固定顺序
    暂存事件与两端涉及暂存/引用缺失的“先于”关系不参与建图
    （排除清单由调用方按 compute 的结果给出，这里仅做一致的过滤）。
    """
    draft_keys = {d["key"] for d in drafts}
    nodes = {0: None}
    node_of_key = {}
    edges = []

    # 节点与边界边：按快照事件顺序
    for e in snapshot_events:
        if e["key"] in draft_keys:
            continue
        idx = len(nodes)
        nodes[idx] = e
        node_of_key[e["key"]] = idx
        emin = _to_minutes(compute._parse(e["earliest_start"]))
        emax = _to_minutes(compute._parse(e["latest_start"]))
        # 最早开始 x_i >= E：边 i → 0，权 -E
        edges.append({
            "id": "{}.earliest".format(e["key"]), "u": idx, "v": 0, "w": -emin,
            "ctype": "earliest_bound", "event": e, "minutes": emin,
        })
        # 最晚开始 x_i <= L：边 0 → i，权 L
        edges.append({
            "id": "{}.latest".format(e["key"]), "u": 0, "v": idx, "w": emax,
            "ctype": "latest_bound", "event": e, "minutes": emax,
        })

    # 关系边：按快照关系顺序；只收录两端均为生效事件的关系（与 compute 一致）
    for j, r in enumerate(snapshot_relations):
        a_key, b_key = r.get("predecessor_key"), r.get("successor_key")
        if a_key not in node_of_key or b_key not in node_of_key:
            continue  # 暂存关联或引用缺失：已（或应）在排除清单中说明
        u, v = node_of_key[a_key], node_of_key[b_key]
        ae = next(e for e in snapshot_events if e["key"] == a_key)
        be = next(e for e in snapshot_events if e["key"] == b_key)
        dur = int(ae["duration_minutes"])
        g, m = _gap_min(r), _gap_max(r)
        rid = "{}.rel{}".format(a_key, j)
        rel_meta = {"index": j, "relation": r, "predecessor": ae, "successor": be}
        # 差分约束标准形 x_v - x_u ≤ w：
        #   最短等待 x_B ≥ x_A + (dur+g) ⇔ x_A - x_B ≤ -(dur+g)：边 B → A
        edges.append({
            "id": rid + ".min", "u": v, "v": u, "w": -(dur + g),
            "ctype": "min_wait", "gap": g, "duration": dur, **rel_meta,
        })
        #   最长等待 x_B ≤ x_A + (dur+m)：边 A → B
        if m is not None:
            edges.append({
                "id": rid + ".max", "u": u, "v": v, "w": dur + m,
                "ctype": "max_wait", "gap": m, "duration": dur, **rel_meta,
            })
    return nodes, edges


# ------------------------------------------------- Bellman-Ford 负环检测

def _negative_cycle(edges, n_nodes):
    """在边子集上跑 Bellman-Ford。

    全节点距离初始 0（等价于增设虚拟源点的 0 权边），边按给定顺序
    （快照固定顺序）松弛、严格小于才更新——同权不更新，前驱首次确定
    后不再被同权边改写，保证同一快照结果稳定。

    返回 (has_cycle, pred)；pred[v] = (u, edge_id) 为最后一轮的前驱。
    """
    dist = [0] * n_nodes
    pred = {}
    for _ in range(n_nodes):
        changed = False
        for e in edges:
            nd = dist[e["u"]] + e["w"]
            if nd < dist[e["v"]]:
                dist[e["v"]] = nd
                pred[e["v"]] = (e["u"], e["id"])
                changed = True
        if not changed:
            return False, pred
    return True, pred


def _feasible(edge_subset, n_nodes):
    """边子集构成的约束系统是否可满足。"""
    has_cycle, _ = _negative_cycle(edge_subset, n_nodes)
    return not has_cycle


def _extract_simple_cycle(pred, n_nodes):
    """从前驱子图中提取一个简单环的边 id 列表。

    前驱子图中每个节点至多一条入边，沿前驱回溯遇到的重复段必为简单环。
    节点按编号顺序扫描，取找到的第一个环，结果确定。
    """
    for start in range(n_nodes):
        seen = {}
        path = []
        cur = start
        while cur is not None and cur not in seen:
            seen[cur] = len(path)
            path.append(cur)
            p = pred.get(cur)
            cur = p[0] if p else None
        if cur is not None:
            cycle_nodes = path[seen[cur]:]
            # 回溯段 c0←c1←…←ck←c0 中，边 pred[c_i] 为 c_{i+1}→c_i；
            # 反序收集即沿边的环方向 c0→ck→…→c1→c0。
            return [pred[node][1] for node in reversed(cycle_nodes)]
    return []


def _minimal_core(cycle_ids, edge_by_id, n_nodes):
    """对简单负环做删除式收缩兜底，得到严格极小的不可满足约束子集。

    简单负环通常本身已极小；逐条试删：删掉后该组仍不可满足的边永久剔除。
    最终集合整体不可满足，且去掉其中任意一条都可满足。
    """
    core = list(cycle_ids)
    for cid in list(core):
        trial = [edge_by_id[k] for k in core if k != cid]
        if not _feasible(trial, n_nodes):
            core.remove(cid)
    return core


# ------------------------------------------------------------------ 展示

def _bound_text(ctype, e, local_time):
    if ctype == "earliest_bound":
        return "事件「{}」的最早开始边界：开始不早于 {}".format(e["name"], local_time)
    return "事件「{}」的最晚开始边界：开始不晚于 {}".format(e["name"], local_time)


def _wait_text(ctype, ae, be, dur, gap):
    if ctype == "min_wait":
        head = "「{}」先于「{}」：前项「{}」持续 {} 分钟".format(
            ae["name"], be["name"], ae["name"], dur)
        if gap:
            head += "、最短等待 {} 分钟".format(gap)
        return head + "，故「{}」开始 ≥「{}」开始之后 {} 分钟".format(
            be["name"], ae["name"], dur + gap)
    return "「{}」先于「{}」：前项「{}」持续 {} 分钟、最长等待 {} 分钟，" \
           "故「{}」开始 ≤「{}」开始之后 {} 分钟".format(
               ae["name"], be["name"], ae["name"], dur, gap,
               be["name"], ae["name"], dur + gap)


def _constraint_view(edge):
    """单条冲突约束的机器 + 可读视图。"""
    ctype = edge["ctype"]
    out = {
        "id": edge["id"],
        "type": ctype,
        "type_label": TYPE_LABELS[ctype],
    }
    if ctype in ("earliest_bound", "latest_bound"):
        e = edge["event"]
        bound_field = "earliest_start" if ctype == "earliest_bound" else "latest_start"
        local_time = e.get(bound_field)
        out.update({
            "event": _event_brief(e),
            "bound": bound_field,
            "local_time": local_time,           # 保留当地时间与 UTC 偏移
            "absolute_minutes": edge["minutes"],
            "text": _bound_text(ctype, e, local_time),
        })
    else:
        ae, be = edge["predecessor"], edge["successor"]
        r = edge["relation"]
        out.update({
            "predecessor": _event_brief(ae),
            "successor": _event_brief(be),
            "predecessor_duration_minutes": edge["duration"],
            "gap_minutes": edge["gap"],
            "delta_minutes": edge["duration"] + edge["gap"],
            "min_gap_minutes": _gap_min(r),
            "max_gap_minutes": _gap_max(r),
            "text": _wait_text(ctype, ae, be, edge["duration"], edge["gap"]),
        })
    return out


def _classify(core_edges):
    """环的归因：invalid_range / cycle / window（与 compute 口径一致）。"""
    involves_anchor = any(e["u"] == 0 or e["v"] == 0 for e in core_edges)
    if not involves_anchor:
        if all(e["ctype"] == "min_wait" for e in core_edges):
            return "cycle"
        return "window"
    if len(core_edges) == 2 and all(
            e["ctype"] in ("earliest_bound", "latest_bound") for e in core_edges):
        evs = {e["event"]["key"] for e in core_edges}
        if len(evs) == 1:
            return "invalid_range"
    return "window"


def _core_message(kind, core_edges, slack):
    """整组不可满足的可读说明。slack 为负环总权（负数=矛盾分钟数）。"""
    gap = -slack

    def names_in_order():
        seen, out = [], []
        for e in core_edges:
            for ev in (e.get("predecessor"), e.get("successor"), e.get("event")):
                if ev is not None and ev["key"] not in seen:
                    seen.append(ev["key"])
                    out.append(ev["name"])
        return out

    if kind == "invalid_range":
        e = core_edges[0]["event"]
        es, ls = e.get("earliest_start"), e.get("latest_start")
        return "事件「{}」输入的最早开始 {} 晚于最晚开始 {}（相差 {} 分钟），" \
               "两条开始边界自身矛盾".format(e["name"], es, ls, gap)

    if kind == "cycle":
        nm = names_in_order()
        chain = " → ".join("「{}」".format(n) for n in nm + nm[:1])
        return "“先于”关系构成不可满足的有向环：{}，沿环兜一圈要求各事件的开始" \
               "再提前 {} 分钟，任何开始时刻都无法同时满足".format(chain, gap)

    names = "、".join("「{}」".format(n) for n in names_in_order())
    n_wait = sum(1 for e in core_edges if e["ctype"] in ("min_wait", "max_wait"))
    if any(e["u"] == 0 or e["v"] == 0 for e in core_edges):
        return "{}的开始边界与 {} 条“先于”关系的等待约束（含前项持续时间）互相矛盾：" \
               "最短等待路径抬高的最早界，高于最晚开始/最长等待路径压低的最晚界" \
               "（沿约束环兜一圈要求同一时刻提前 {} 分钟），不存在可行的开始时刻" \
               .format(names, n_wait, gap)
    return "{}之间的 {} 条“先于”关系在多条路径上互相矛盾：沿一条路径要求的最小" \
           "间隔大于沿另一条路径允许的最大间隔（沿约束环兜一圈要求同一时刻提前 " \
           "{} 分钟），不存在可行的开始时刻".format(names, n_wait, gap)


def _group_view(index, core_ids, edge_by_id, nodes):
    core_edges = [edge_by_id[cid] for cid in core_ids]
    slack = sum(e["w"] for e in core_edges)
    kind = _classify(core_edges)
    constraints = [_constraint_view(e) for e in core_edges]
    # 关联事件：按环上首次出现顺序去重（不含锚点）
    events, seen = [], set()
    for e in core_edges:
        for ev in (e.get("predecessor"), e.get("successor"), e.get("event")):
            if ev is not None and ev["key"] not in seen:
                seen.add(ev["key"])
                events.append(_event_brief(ev))
    return {
        "index": index,
        "kind": kind,
        "slack_minutes": slack,
        "contradiction_minutes": -slack,
        "message": _core_message(kind, core_edges, slack),
        "constraints": constraints,
        "events": events,
    }


# ------------------------------------------------- 排除项（暂存/跳过关系）

def _excluded_views(snapshot_events, snapshot_relations, drafts):
    """把 compute 的暂存/跳过结果翻译为快照身份（key/uid/名称）。"""
    draft_by_key = {d["key"]: d for d in drafts}
    ev_by_key = {e["key"]: e for e in snapshot_events}

    draft_views = [{
        "key": d["key"],
        "uid": ev_by_key.get(d["key"], {}).get("uid"),
        "name": d["name"],
        "timezone": d.get("timezone"),
        "reason": d["reason"].replace("已暂存，不参与本次推算",
                                      "处于暂存状态，不参与冲突分析"),
    } for d in drafts]

    excluded_relations = []
    for j, r in enumerate(snapshot_relations):
        a_key, b_key = r.get("predecessor_key"), r.get("successor_key")
        if a_key in draft_by_key or b_key in draft_by_key \
                or a_key not in ev_by_key or b_key not in ev_by_key:
            ae, be = ev_by_key.get(a_key), ev_by_key.get(b_key)
            if a_key in draft_by_key or b_key in draft_by_key:
                inactive = [ev_by_key[k]["name"] for k in (a_key, b_key)
                            if k in draft_by_key]
                reason = "关联事件「{}」处于暂存状态，该“先于”关系不参与冲突分析".format(
                    "、".join(inactive))
            else:
                reason = "关系引用了不存在的事件，已忽略，不参与冲突分析"
            excluded_relations.append({
                "id": "{}.rel{}".format(a_key, j),
                "relation_index": j,
                "predecessor": _event_brief(ae) if ae else {"key": a_key, "uid": None,
                                                            "name": None, "timezone": None},
                "successor": _event_brief(be) if be else {"key": b_key, "uid": None,
                                                          "name": None, "timezone": None},
                "min_gap_minutes": _gap_min(r),
                "max_gap_minutes": _gap_max(r),
                "reason": reason,
            })
    return draft_views, excluded_relations


# ------------------------------------------------------------------ 主入口

def analyze_input(snapshot_events, snapshot_relations):
    """按不可变快照输入分析最小不可满足约束组（纯函数，不写任何数据）。

    返回：
      status              "ok" | "conflict"（与 compute 对同一快照的结论一致）
      groups              极小冲突约束组列表；无冲突为 []，每组整组不可满足、
                          去掉任一条约束后该组可满足；同快照重复调用结果稳定
      drafts              被排除的暂存事件（不参与分析）及原因
      excluded_relations  被跳过的“先于”关系及原因
    """
    snapshot_events = list(snapshot_events or [])
    snapshot_relations = list(snapshot_relations or [])

    # 1) 沿用 compute 的口径判定暂存事件/被跳过关系，同时取得冲突结论
    compute_events = [dict(e, id=e["key"]) for e in snapshot_events]
    compute_relations = [{
        "id": "rel{}".format(j),
        "predecessor_id": r.get("predecessor_key"),
        "successor_id": r.get("successor_key"),
        "min_gap_minutes": _gap_min(r),
        "max_gap_minutes": _gap_max(r),
    } for j, r in enumerate(snapshot_relations)]
    result = compute.compute(compute_events, compute_relations)
    drafts = [{
        "key": d["id"], "name": d["name"],
        "timezone": d.get("timezone"), "reason": d["reason"],
    } for d in result["drafts"]]

    draft_views, excluded_relations = _excluded_views(
        snapshot_events, snapshot_relations, drafts)

    out = {
        "status": result["status"],
        "groups": [],
        "drafts": draft_views,
        "excluded_relations": excluded_relations,
    }
    if result["status"] != "conflict":
        return out  # 无冲突（含仅有暂存事件的版本）：空组

    # 2) 构建差分约束图，迭代找出相互独立的极小不可满足约束组
    nodes, edges = _build_graph(snapshot_events, snapshot_relations, drafts)
    n_nodes = len(nodes)
    edge_by_id = {e["id"]: e for e in edges}

    working = list(edges)
    groups = []
    while True:
        has_cycle, pred = _negative_cycle(working, n_nodes)
        if not has_cycle:
            break
        cycle_ids = _extract_simple_cycle(pred, n_nodes)
        if not cycle_ids:
            # 理论上不可达：检测到负环必能在第 n 轮的前驱子图中找到环
            break
        # 环旋转到最小约束编号起始，保证展示起点稳定
        start = min(range(len(cycle_ids)), key=lambda k: cycle_ids[k])
        cycle_ids = cycle_ids[start:] + cycle_ids[:start]
        # 删除式收缩兜底，严格保证极小性
        core_ids = _minimal_core(cycle_ids, edge_by_id, n_nodes)
        # 收缩后按环的相对顺序重排（以仍保留的最小编号为起点）
        keep = set(core_ids)
        ordered = [cid for cid in cycle_ids if cid in keep]
        groups.append(_group_view(len(groups) + 1, ordered, edge_by_id, nodes))
        # 整组约束从工作图移除，继续寻找其他独立冲突
        working = [e for e in working if e["id"] not in keep]

    # 组间顺序按组内最小约束编号确定（快照固定顺序，稳定且符合直觉）后重新编号
    groups.sort(key=lambda g: min(c["id"] for c in g["constraints"]))
    for i, g in enumerate(groups):
        g["index"] = i + 1

    out["groups"] = groups
    return out


def analyze_snapshot(version_data):
    """对 db 层组装的版本回看数据（含不可变 input 快照）做冲突分析。"""
    snapshot = version_data.get("input", {}) or {}
    return analyze_input(snapshot.get("events", []), snapshot.get("relations", []))
