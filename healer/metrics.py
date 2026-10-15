"""Reliability metrics derived from the event log (no extra bookkeeping).

Per service, from its first lifecycle event until now:
  uptime %      time spent running / total time (stopped and down both count as not up)
  incident      the service failed while up (crash, hang, memory limit); it stays open
                until the service is running again
  MTTR          mean time to recovery: failure -> running again, over recovered incidents
  heal success  recovered incidents / all incidents (open ones count as not recovered yet)
Operator and dependency stops are downtime, but not incidents.
"""
from .eventlog import EventType

STARTS = (EventType.SERVICE_STARTED, EventType.SERVICE_RESTARTED)
FAILURES = (EventType.SERVICE_CRASHED, EventType.SERVICE_HUNG, EventType.RESOURCE_LIMIT)


def _analyse(timeline, now: float) -> dict:
    state, up_since, down_since = None, None, None     # state: up | down | stopped
    up, incidents, durations = 0.0, 0, []
    for ts, etype in timeline:
        if etype in STARTS:
            if state == "down":
                durations.append(ts - down_since)      # incident recovered
            state, up_since, down_since = "up", ts, None
        elif etype in FAILURES:
            if state == "up":
                up += ts - up_since
            if state != "down":                        # more symptoms of an open incident are ignored
                incidents += 1
                state, up_since, down_since = "down", None, ts
        elif etype == EventType.SERVICE_STOPPED and state == "up":
            up += ts - up_since
            state, up_since = "stopped", None
    if state == "up":
        up += now - up_since
    return {"up": up, "window": now - timeline[0][0], "incidents": incidents,
            "durations": durations, "open": 1 if state == "down" else 0}


def _summary(a: dict) -> dict:
    recovered = len(a["durations"])
    return {
        "uptime_pct": round(100.0 * a["up"] / a["window"], 2) if a["window"] > 0 else None,
        "incidents": a["incidents"],
        "recovered": recovered,
        "open": a["open"],
        "mttr_s": round(sum(a["durations"]) / recovered, 1) if recovered else None,
        "success_pct": round(100.0 * recovered / a["incidents"], 1) if a["incidents"] else None,
    }


def compute(events, now: float) -> dict:
    """events: iterable of (ts, service, event_type), oldest first."""
    timelines = {}
    for ts, service, etype in events:
        timelines.setdefault(service, []).append((ts, etype))
    services, all_durations = {}, []
    total = {"up": 0.0, "window": 0.0, "incidents": 0, "open": 0}
    for name in sorted(timelines):
        analysis = _analyse(timelines[name], now)
        services[name] = _summary(analysis)
        all_durations += analysis["durations"]
        for key in total:
            total[key] += analysis[key]
    return {"services": services, "overall": _summary(dict(total, durations=all_durations))}