"""Read-only Microsoft cloud discovery after explicit account login."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True

from _df_runtime import bootstrap

bootstrap()

from team_drive.bounded_json import loads
from team_drive.errors import DriveError
from team_drive.graph_auth import TokenProvider
from team_drive.graph_discovery import GraphDiscovery


def main(argv=None):
    parser = argparse.ArgumentParser(description="列出获准的微软云盘或解析文件夹地址；不下载正文，不建立索引。")
    parser.add_argument("action", choices=("drives", "site", "folders", "resolve", "folder"))
    parser.add_argument("--account-config", required=True)
    parser.add_argument("--url")
    parser.add_argument("--drive-id")
    parser.add_argument("--folder-id")
    parser.add_argument("--site-id")
    args = parser.parse_args(argv)
    try:
        path = Path(args.account_config).expanduser().resolve(strict=True)
        with path.open("rb") as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError()
        config = loads(raw)["graph"]
        cache = Path(config["auth_cache"]).expanduser()
        if not cache.is_absolute():
            cache = path.parent / cache
        discovery = GraphDiscovery(TokenProvider(config, cache))
        if args.action in ("site", "resolve") and not args.url:
            raise ValueError()
        if args.action in ("folders", "folder") and not args.drive_id:
            raise ValueError()
        if args.action == "drives":
            result = discovery.list_drives(site_id=args.site_id)
        elif args.action == "site":
            result = discovery.resolve_site(args.url)
        elif args.action == "folders":
            result = discovery.list_folders(args.drive_id, args.folder_id)
        elif args.action == "folder":
            result = discovery.get_folder(args.drive_id, args.folder_id)
        else:
            result = discovery.resolve_folder(args.url, drive_id=args.drive_id, site_id=args.site_id)
        print(json.dumps(dict(result, ok=True), ensure_ascii=False, allow_nan=False))
        return 0
    except DriveError as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": exc.message,
                                        "retryable": exc.retryable, "next_action": exc.next_action}}
    except (OSError, KeyError, ValueError, TypeError, RecursionError):
        result = {"ok": False, "error": {"code": "INVALID_DISCOVERY_INPUT",
                   "message": "请使用已登录的账号配置，并提供完整文件夹链接或选择已列出的文件夹。"}}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
