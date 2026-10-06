"""跨地区时区特性单元测试：python tests/test_timezone.py

覆盖：
- tzutil：IANA 名校验；带偏移当地时间的真实时刻校验；
  夏令时跳过时刻（gap）拒绝、重复时刻（fold）按偏移区分、错误偏移拒绝；
- compute：带时区事件按绝对分钟施加持续时长与最短/最长等待，
  跨时区输入先后按绝对时间判断，结果/依据链按各事件当地时间与偏移展示；
  带时区与未带时区事件之间建立关系直接报错；
  未带时区的朴素事件结果与旧语义完全一致。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import compute  # noqa: E402
import tzutil  # noqa: E402


def ev(i, name, es, ls, dur, tz=None):
    e = {"id": i, "name": name, "earliest_start": es, "latest_start": ls,
         "duration_minutes": dur}
    if tz is not None:
        e["timezone"] = tz
    return e


def rel(i, a, b, min_g=None, max_g=None):
    r = {"id": i, "predecessor_id": a, "successor_id": b}
    if min_g is not None:
        r["min_gap_minutes"] = min_g
    if max_g is not None:
        r["max_gap_minutes"] = max_g
    return r


# ------------------------------------------------------------------ tzutil

def test_clean_tz_name():
    assert tzutil.clean_tz_name("Asia/Shanghai") == "Asia/Shanghai"
    assert tzutil.clean_tz_name("  UTC  ") == "UTC"
    assert tzutil.clean_tz_name("") is None
    assert tzutil.clean_tz_name(None) is None
    for bad in ("Asia/Foo", "not a zone", "../etc"):
        try:
            tzutil.clean_tz_name(bad)
        except tzutil.TimezoneError:
            continue
        raise AssertionError("应拒绝非法时区名: " + bad)


def test_validate_ordinary_offset():
    out = tzutil.validate_local_time("2026-10-01T10:00+08:00", "Asia/Shanghai")
    assert out == "2026-10-01T10:00+08:00"
    # 纽约 10 月为夏令时 -04:00
    out = tzutil.validate_local_time("2026-10-01T10:00-04:00", "America/New_York")
    assert out == "2026-10-01T10:00-04:00"
    assert tzutil.validate_local_time("", "UTC") is None


def test_validate_wrong_offset_rejected():
    # 7 月纽约是夏令时（-04:00），给 -05:00 属错误偏移
    try:
        tzutil.validate_local_time("2026-07-01T21:00-05:00", "America/New_York")
    except tzutil.TimezoneError as e:
        assert "不符合时区" in str(e)
    else:
        raise AssertionError("错误偏移应被拒绝")


def test_validate_missing_offset_rejected():
    try:
        tzutil.validate_local_time("2026-10-01T10:00", "Asia/Shanghai")
    except tzutil.TimezoneError as e:
        assert "必须携带 UTC 偏移" in str(e)
    else:
        raise AssertionError("缺偏移应被拒绝")


def test_validate_spring_forward_gap_rejected():
    # 2026-03-08 02:00→03:00 纽约，02:30 这一当地时刻不存在
    for off in ("-05:00", "-04:00"):
        try:
            tzutil.validate_local_time("2026-03-08T02:30" + off, "America/New_York")
        except tzutil.TimezoneError as e:
            assert "不存在" in str(e)
        else:
            raise AssertionError("gap 时刻应被拒绝: " + off)


def test_validate_fall_back_repeated_moment_disambiguated():
    # 2026-11-01 01:30 在纽约出现两次：-04:00（第一次）与 -05:00（第二次），
    # 两个偏移都合法且对应不同的绝对时刻。
    first = tzutil.validate_local_time("2026-11-01T01:30-04:00", "America/New_York")
    second = tzutil.validate_local_time("2026-11-01T01:30-05:00", "America/New_York")
    assert first == "2026-11-01T01:30-04:00"
    assert second == "2026-11-01T01:30-05:00"
    # 其他偏移（如 -03:00）在重复时段也不合法
    try:
        tzutil.validate_local_time("2026-11-01T01:30-03:00", "America/New_York")
    except tzutil.TimezoneError:
        pass
    else:
        raise AssertionError("重复时刻的错误偏移应被拒绝")


def test_format_dt_converts_to_event_zone():
    from datetime import datetime
    inst = datetime.fromisoformat("2026-10-01T02:00:00+00:00")
    assert tzutil.format_dt(inst, "Asia/Shanghai") == "2026-10-01T10:00+08:00"
    assert tzutil.format_dt(inst, "America/New_York") == "2026-09-30T22:00-04:00"
    naive = datetime.fromisoformat("2026-10-01T10:00")
    assert tzutil.format_dt(naive, None) == "2026-10-01T10:00"


# ------------------------------------------------------------------ compute

def test_cross_zone_duration_and_gap_absolute():
    events = [
        # 上海 10:00+08 = 02:00 UTC，持续 15 分钟，02:15 UTC 结束
        ev(1, "上海告警", "2026-10-01T10:00+08:00", "2026-10-01T10:00+08:00", 15,
           "Asia/Shanghai"),
        # 纽约当地窗口很宽
        ev(2, "纽约响应", "2026-09-30T20:00-04:00", "2026-10-01T12:00-04:00", 30,
           "America/New_York"),
    ]
    res = compute.compute(events, [rel(1, 1, 2)])
    assert res["status"] == "ok", res
    w = res["windows"][2]
    # 后项最早开始 = 前项结束绝对时刻 02:15 UTC = 纽约 09-30 22:15-04:00
    assert w["earliest_start"] == "2026-09-30T22:15-04:00", w
    assert w["timezone"] == "America/New_York"
    # 窗口展示带偏移
    assert w["latest_start"] == "2026-10-01T12:00-04:00"
    # 依据链中的时间按各事件当地时间展示（上海行带 +08:00）
    chain_text = " ".join(s["text"] for s in w["earliest_chain"])
    assert "+08:00" in chain_text
    assert "22:15" in chain_text


def test_cross_zone_input_order_is_absolute():
    events = [
        # 纽约 09-30 22:00-04 = 02:00 UTC；上海输入窗口自相矛盾（按绝对时刻）
        ev(1, "纽约A", "2026-10-01T02:30+08:00", "2026-10-01T01:30+08:00", 0,
           "Asia/Shanghai"),
    ]
    res = compute.compute(events, [])
    assert res["status"] == "conflict"
    assert res["conflicts"][0]["type"] == "invalid_range"


def test_min_max_gap_across_zones_absolute():
    events = [
        ev(1, "A", "2026-10-01T10:00+00:00", "2026-10-01T10:00+00:00", 0, "UTC"),
        ev(2, "B", "2026-10-01T10:00+00:00", "2026-10-02T00:00+00:00", 0,
           "Asia/Shanghai"),
    ]
    # A 结束后须在 60~120 分钟内开始 B：B 窗口 11:00~12:00 UTC = 上海 19:00~20:00
    res = compute.compute(events, [rel(1, 1, 2, min_g=60, max_g=120)])
    assert res["status"] == "ok", res
    w = res["windows"][2]
    assert w["earliest_start"] == "2026-10-01T19:00+08:00", w
    assert w["latest_start"] == "2026-10-01T20:00+08:00", w


def test_dst_fold_gap_timedelta_is_absolute():
    # 纽约重复时刻：第一次 01:30-04（05:30 UTC），事件持续 60 分钟，
    # 结束绝对时刻 06:30 UTC = 当地 01:30-05（第二次面盘 01:30），
    # 证明持续时长按绝对分钟施加、结束按当地展示。
    events = [
        ev(1, "重复时刻事件", "2026-11-01T01:30-04:00", "2026-11-01T01:30-04:00",
           60, "America/New_York"),
    ]
    res = compute.compute(events, [])
    w = res["windows"][1]
    assert w["earliest_start"] == "2026-11-01T01:30-04:00"
    assert w["earliest_end"] == "2026-11-01T01:30-05:00", w


def test_cross_zone_backward_propagation_local_display():
    # 后项（纽约）最晚开始经“最短等待”反推出前项（上海）最晚开始，
    # 结论按前项上海当地时间展示，依据链同时包含两地时间与偏移。
    events = [
        ev(1, "上海A", "2026-10-01T08:00+08:00", "2026-10-02T08:00+08:00", 0,
           "Asia/Shanghai"),
        ev(2, "纽约B", "2026-09-30T20:00-04:00", "2026-10-01T12:00-04:00", 0,
           "America/New_York"),
    ]
    res = compute.compute(events, [rel(1, 1, 2, min_g=60)])
    assert res["status"] == "ok", res
    wa = res["windows"][1]
    # 纽约 12:00-04 = 16:00 UTC，减 60 分钟 = 15:00 UTC = 上海 23:00+08
    assert wa["latest_start"] == "2026-10-01T23:00+08:00", wa
    txt = " ".join(s["text"] for s in wa["latest_chain"])
    assert "12:00-04:00" in txt and "23:00+08:00" in txt


def test_mixed_tz_and_naive_relation_raises():
    events = [
        ev(1, "带时区", "2026-10-01T10:00+08:00", "2026-10-01T10:00+08:00", 0,
           "Asia/Shanghai"),
        ev(2, "朴素", "2026-10-01T10:00", "2026-10-01T12:00", 0),
    ]
    try:
        compute.compute(events, [rel(1, 1, 2)])
    except ValueError as e:
        assert "不得建立" in str(e)
    else:
        raise AssertionError("带时区与未带时区事件之间不得建立关系")


def test_naive_events_keep_legacy_semantics():
    events = [
        ev(1, "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev(2, "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    res = compute.compute(events, [rel(1, 1, 2)])
    assert res["status"] == "ok"
    w2 = res["windows"][2]
    assert w2["earliest_start"] == "2026-10-01T10:15"
    assert w2.get("timezone") is None
    assert "+" not in w2["earliest_start"]


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", t.__name__, "->", e)
    sys.exit(1 if failed else 0)
