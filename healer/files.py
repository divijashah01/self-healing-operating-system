"""Manage protected files:  python3 -m healer.files [list | approve | restore]"""
import argparse
import sys
import time
from pathlib import Path

from .config import PROJECT_ROOT, load_config
from .eventlog import EventLog
from .filevault import FileVault


def _find(cfg, raw: str):
    path = (PROJECT_ROOT / raw).resolve()      # same rule the config loader uses
    for pf in cfg.protected_files:
        if pf.path == path:
            return pf
    sys.exit(f"error: {raw} is not listed under protected_files")


def cmd_list(cfg, eventlog, vault) -> None:
    print("(* = current baseline)")
    for pf in cfg.protected_files:
        state = vault.verify(str(pf.path)).state
        print(f"\n{pf.path}  [{state}]  mode={pf.mode}  keep={pf.keep_versions}")
        for v in eventlog.file_versions(str(pf.path)):
            mark = "*" if v["is_baseline"] else " "
            when = time.strftime("%H:%M:%S", time.localtime(v["created_at"]))
            print(f"  {mark} v{v['version']:<3} {v['sha256'][:12]}  {v['size']:>6}B  "
                  f"{when}  {v['note']}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="healer.files")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list", help="show protected files and their versions (default)")
    p_app = sub.add_parser("approve", help="accept a change as the new baseline")
    p_app.add_argument("path")
    p_app.add_argument("--from", dest="source",
                       help="install this file as the new content (recommended: the guard "
                            "would revert a direct edit before you could approve it)")
    p_res = sub.add_parser("restore", help="write a stored version back and make it the baseline")
    p_res.add_argument("path")
    p_res.add_argument("--version", type=int, help="default: current baseline")
    args = parser.parse_args()

    cfg = load_config(args.config)
    eventlog = EventLog(cfg.healer.db_path)
    vault = FileVault(eventlog, cfg.healer.data_dir / "snapshots")
    try:
        if args.cmd == "approve":
            pf = _find(cfg, args.path)
            if args.source:
                data = Path(args.source).read_bytes()
                v = vault.update(str(pf.path), data, pf.keep_versions,
                                 note=f"approved from {args.source}")
            else:
                v = vault.adopt(str(pf.path), pf.keep_versions, note="approved current content")
            print(f"baseline is now v{v}")
        elif args.cmd == "restore":
            pf = _find(cfg, args.path)
            v = vault.restore(str(pf.path), args.version, reason="manual")
            print(f"restored v{v} (now the baseline)")
        else:
            cmd_list(cfg, eventlog, vault)
    except (KeyError, OSError, ValueError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()