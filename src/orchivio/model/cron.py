"""Five-field cron expressions (UTC), parsed strictly.

Fields: minute hour day-of-month month day-of-week (0-6, Sunday = 0; 7 is also accepted as Sunday).
Each field is a comma list of ``*``, ``n``, ``a-b`` or any of those with ``/step``. Names, ``?``, ``L``,
``W`` and ``#`` are not supported. As in Vixie cron, when both day fields are restricted a day matches if
*either* matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
MAX_SEARCH_DAYS = 366 * 8  # leap-day schedules need up to 8 years


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int, name: str) -> frozenset[int]:
    out: set[int] = set()
    if not text:
        raise CronError(f"empty {name} field")
    for part in text.split(","):
        base, slash, step_s = part.partition("/")
        step = 1
        if slash:
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"bad step in {name}: {part!r}")
            step = int(step_s)
        if base == "*":
            a, b = lo, hi
        else:
            lo_s, dash, hi_s = base.partition("-")
            if not lo_s.isdigit() or (dash and not hi_s.isdigit()):
                raise CronError(f"bad value in {name}: {part!r}")
            a = int(lo_s)
            b = int(hi_s) if dash else (hi if slash else a)
        if not (lo <= a <= hi and lo <= b <= hi) or a > b:
            raise CronError(f"{name} value out of range {lo}-{hi}: {part!r}")
        out.update(range(a, b + 1, step))
    return frozenset(out)


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0-6, Sunday = 0
    dom_star: bool
    dow_star: bool
    text: str

    def _day_ok(self, d: datetime) -> bool:
        if d.month not in self.months:
            return False
        dom, dow = d.day in self.days, (d.isoweekday() % 7) in self.weekdays
        if self.dom_star or self.dow_star:
            return dom and dow  # an unrestricted field matches everything, so this is the restricted one alone
        return dom or dow

    def matches(self, t: datetime) -> bool:
        return t.minute in self.minutes and t.hour in self.hours and self._day_ok(t)

    def next_after(self, after: datetime) -> datetime:
        """The first matching minute strictly after ``after`` (UTC, seconds ignored)."""
        t = (after.astimezone(UTC) if after.tzinfo else after.replace(tzinfo=UTC)).replace(second=0, microsecond=0)
        t += timedelta(minutes=1)
        day = t.replace(hour=0, minute=0)
        for i in range(MAX_SEARCH_DAYS):
            d = day + timedelta(days=i)
            if not self._day_ok(d):
                continue
            for h in sorted(self.hours):
                for m in sorted(self.minutes):
                    cand = d.replace(hour=h, minute=m)
                    if cand >= t:
                        return cand
        raise CronError(f"{self.text!r} never fires")

    def between(self, start: datetime, end: datetime, limit: int = 1000) -> list[datetime]:
        """Occurrences in ``(start, end]``, at most ``limit`` (the earliest ones)."""
        out: list[datetime] = []
        t = start
        while len(out) < limit:
            t = self.next_after(t)
            if t > end:
                break
            out.append(t)
        return out


def parse_cron(text: str) -> Cron:
    parts = text.split()
    if len(parts) != 5:
        raise CronError("expected five fields: minute hour day-of-month month day-of-week")
    sets = [_field(p, lo, hi, n) for p, (lo, hi), n in zip(parts, _RANGES, _NAMES, strict=True)]
    weekdays = frozenset(0 if d == 7 else d for d in sets[4])
    cron = Cron(sets[0], sets[1], sets[2], sets[3], weekdays, parts[2].startswith("*"), parts[4].startswith("*"), " ".join(parts))
    cron.next_after(datetime(2024, 1, 1, tzinfo=UTC))  # raises when it can never fire (e.g. 31 Feb)
    return cron
