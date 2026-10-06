"""跨地区时区校验与展示工具（仅依赖标准库 zoneinfo）。

带时区事件约定：
  - 事件可指定一个 IANA 时区名（如 Asia/Shanghai、America/New_York）；
  - 事件填写的最早/最晚开始必须是**带 UTC 偏移的当地时间**；
  - 偏移必须符合该时区当日规则：夏令时“重复时刻”（回退一小时，同一面盘
    时间出现两次）按偏移区分是哪一次；夏令时“跳过的时刻”（向前拨一小时
    造成的不存在当地时间）以及与规则不符的偏移一律拒绝。

校验原理（zoneinfo）：把面盘时间分别按 fold=0/1 落到时间轴再折返回该时区，
  - 两个 fold 都无法折返到同一面盘时间 ⇒ 该当地时间不存在（gap）；
  - 可以折返但当时的实际偏移都不等于用户给出的偏移 ⇒ 偏移错误；
  - 普通时刻两个 fold 等价；重复时刻两个 fold 给出两个不同偏移，按用户
    给出的偏移唯一确定是哪一次。
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TimezoneError(ValueError):
    """时区名非法，或带偏移当地时间不真实/偏移不符。"""


def clean_tz_name(value):
    """规范化 IANA 时区名；空值返回 None；非法名抛 TimezoneError。

    合法名原样保留（别名如 Asia/Calcutta 同样可用，不做改写）。
    """
    if value is None:
        return None
    name = str(value).strip()
    if not name:
        return None
    if len(name) > 64:
        raise TimezoneError("IANA 时区名称过长（最多 64 个字符）")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise TimezoneError(
            "「{}」不是有效的 IANA 时区名（例如 Asia/Shanghai、Europe/London、"
            "America/New_York）".format(name))
    return name


def _offset_text(offset):
    """timedelta 偏移 → UTC+HH:MM / UTC-HH:MM。"""
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return "UTC{}{:02d}:{:02d}".format(sign, total // 60, total % 60)


def _candidate(naive, zone, fold):
    """把面盘时间按指定 fold 落到时间轴并折返；

    折返后面盘时间一致才是真实存在的当地时刻，返回该 aware datetime；
    否则（夏令时跳过的不存在时刻）返回 None。
    """
    attempted = naive.replace(tzinfo=zone, fold=fold)
    back = attempted.astimezone(timezone.utc).astimezone(zone)
    if back.replace(tzinfo=None) == naive:
        return attempted
    return None


def validate_local_time(value, tzname):
    """校验带时区事件的一个时间输入。

    value  : ISO 日期时间字符串，**必须携带 UTC 偏移**；
    tzname : 已校验合法的 IANA 时区名。

    成功返回规范化后的分钟精度 ISO 字符串（保留正确偏移，如
    "2026-11-01T01:30-05:00"）；空值返回 None；非法情况抛 TimezoneError。
    """
    if value in (None, ""):
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise TimezoneError("时间「{}」不是合法的 ISO 日期时间".format(text))
    if parsed.tzinfo is None:
        raise TimezoneError(
            "时间「{}」必须携带 UTC 偏移（如 {}+08:00），用以表明该当地时间"
            "相对 UTC 的绝对时刻".format(text, text))

    zone = ZoneInfo(tzname)
    provided_offset = parsed.utcoffset()
    naive_wall = parsed.replace(tzinfo=None)

    chosen = None
    for fold in (0, 1):
        cand = _candidate(naive_wall, zone, fold)
        if cand is not None and cand.utcoffset() == provided_offset:
            chosen = cand
            break

    if chosen is None:
        valid = [c for fold in (0, 1)
                 for c in (_candidate(naive_wall, zone, fold),) if c is not None]
        wall_text = naive_wall.isoformat(timespec="minutes")
        if not valid:
            raise TimezoneError(
                "当地时间 {} 在时区 {} 不存在（处于夏令时切换被跳过的时段），"
                "请改用真实存在的当地时刻".format(wall_text, tzname))
        # 同一面盘时间存在两次且两个生效偏移不同 ⇒ 夏令时回退的重复时刻
        is_overlap = len({c.utcoffset() for c in valid}) > 1
        if is_overlap:
            raise TimezoneError(
                "时间 {} 携带的偏移 {} 不符合时区 {} 当时的规则；该面盘时间在夏令时"
                "回退时出现两次，请用当时生效的偏移（{} 或 {}）以区分".format(
                    wall_text, _offset_text(provided_offset), tzname,
                    _offset_text(valid[0].utcoffset()),
                    _offset_text(valid[1].utcoffset())))
        raise TimezoneError(
            "时间 {} 携带的偏移 {} 不符合时区 {} 当时的规则（应为 {}）".format(
                wall_text, _offset_text(provided_offset), tzname,
                _offset_text(valid[0].utcoffset())))

    return chosen.astimezone(zone).isoformat(timespec="minutes")


def format_dt(dt, tzname=None):
    """分钟精度展示：带时区事件先折算到其 IANA 时区再输出（保留偏移）；

    未带时区的朴素时间按既有语义直接输出。
    """
    if dt.tzinfo is not None and tzname:
        dt = dt.astimezone(ZoneInfo(tzname))
    return dt.isoformat(timespec="minutes")
