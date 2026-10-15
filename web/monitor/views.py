import json
import time
from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from healer.checkpoints import validate
from healer.checkpoints import CheckpointManager
from healer.filevault import FileVault

from . import services

ACTIONS = {"restart_service", "stop_service", "start_service", "reset_circuit",
           "restore_file", "update_file",
           "chaos_kill", "chaos_freeze", "chaos_leak", "chaos_file", "set_healing"}
MAX_BODY = 100_000


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


@require_GET
@ensure_csrf_cookie
def dashboard(request):
    return render(request, "monitor/dashboard.html", {"page": "dashboard"})


@require_GET
def api_state(request):
    cfg = services.get_config()
    return JsonResponse(services.snapshot(services.get_log(cfg)))


@require_POST
def api_command(request):
    if len(request.body) > MAX_BODY:
        return JsonResponse({"error": "request too large"}, status=413)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "expected a JSON object"}, status=400)
    action, target, args = body.get("action"), body.get("target"), body.get("args") or {}
    if action not in ACTIONS:
        return JsonResponse({"error": f"unknown action {action!r}"}, status=400)
    if not isinstance(target, str) or not target:
        return JsonResponse({"error": "missing target"}, status=400)
    if not isinstance(args, dict):
        return JsonResponse({"error": "args must be an object"}, status=400)
    log = services.get_log(services.get_config())
    return JsonResponse({"id": log.enqueue_command(action, target, args)})


@require_GET
def api_command_status(request, command_id):
    log = services.get_log(services.get_config())
    cmd = log.get_command(command_id)
    if cmd is None:
        return JsonResponse({"error": "no such command"}, status=404)
    return JsonResponse({k: cmd[k] for k in ("id", "action", "target", "status", "result")})


def _baseline_text(log, pf) -> str:
    baseline = log.get_baseline(str(pf.path))
    for source in ([Path(baseline["blob_path"])] if baseline else []) + [pf.path]:
        try:
            return source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return ""


@require_GET
@ensure_csrf_cookie
def files(request):
    cfg = services.get_config()
    log = services.get_log(cfg)
    vault = FileVault(log, cfg.healer.data_dir / "snapshots")
    items = []
    for pf in cfg.protected_files:
        key = str(pf.path)
        versions = [dict(v, when=_when(v["created_at"])) for v in log.file_versions(key)]
        items.append({"path": key, "mode": pf.mode, "state": vault.verify(key).state,
                      "versions": versions, "baseline_text": _baseline_text(log, pf)})
    return render(request, "monitor/files.html", {"page": "files", "files": items})


@require_GET
@ensure_csrf_cookie
def checkpoints(request):
    cfg = services.get_config()
    manager = CheckpointManager(services.get_log(cfg), cfg.healer.data_dir / "checkpoints")
    items = []
    for svc in cfg.services:
        if svc.checkpoint is None:
            continue
        rows = []
        for path in sorted(manager.dir_for(svc.name).glob("ckpt-*"), reverse=True):
            status, note, state = "valid", "", ""
            if path.name.endswith(".bad"):
                status = "rejected"
            elif path.name.endswith(".suspect"):
                status = "suspect"
            else:
                try:
                    state = json.dumps(json.loads(path.read_text(encoding="utf-8")).get("state"),
                                    sort_keys=True)[:100]
                except (OSError, ValueError):
                    pass
            try:
                stat = path.stat()
                rows.append({"name": path.name, "status": status, "note": note,
                             "state": state, "size": stat.st_size, "when": _when(stat.st_mtime)})
            except OSError:
                continue
        items.append({"service": svc.name, "rows": rows})
    return render(request, "monitor/checkpoints.html", {"page": "checkpoints", "items": items})

@require_GET
@ensure_csrf_cookie
def chaos(request):
    cfg = services.get_config()
    log = services.get_log(cfg)
    comparison = None
    raw = log.get_meta("last_comparison")
    if raw:
        try:
            data = json.loads(raw)
            comparison = {
                "when": _when(data["ts"]),
                "rows": [{"fault": r["fault"],
                          "off": (f"recovered in {r['off']:.1f}s" if r["off"] is not None
                                  else f"NOT recovered within {data['off_window']:g}s"),
                          "off_bad": r["off"] is None,
                          "on": (f"recovered in {r['on']:.1f}s" if r["on"] is not None
                                 else f"NOT recovered within {data['timeout']:g}s")}
                         for r in data["rows"]]}
        except (ValueError, KeyError, TypeError):
            comparison = None
    context = {
        "page": "chaos",
        "services": [{"name": s.name, "leakable": s.memory_limit_mb is not None}
                     for s in cfg.services],
        "files": [str(pf.path) for pf in cfg.protected_files],
        "comparison": comparison,
    }
    return render(request, "monitor/chaos.html", context)

@require_GET
def api_metrics(request):
    return JsonResponse(services.metrics(services.get_log(services.get_config())))