"""JSONL worker: one request per line; stdout contains protocol responses only."""
import argparse
import json
import signal
import sys

from .engine import DriveError, failure
from .bridge import Bridge


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        engine = Bridge(args.config)
    except DriveError as exc:
        print(json.dumps({"id": None, "result": failure(exc.code, exc.message, exc.retryable, exc.next_action)}), flush=True)
        return 1
    except Exception:
        print(json.dumps({"id": None, "result": failure("CONFIGURATION_FAILED", "The local configuration could not be loaded.")}), flush=True)
        return 1
    def terminate(signum, frame):
        engine.close()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        while True:
            line = sys.stdin.buffer.readline(65537)
            if not line:
                break
            request_id = None
            try:
                if len(line) > 65536:
                    # Consume this request so its tail is not interpreted as a new message.
                    while line and not line.endswith(b"\n"):
                        line = sys.stdin.buffer.readline(65537)
                    result = failure("REQUEST_TOO_LARGE", "The request exceeds 64 KiB.")
                else:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError()
                    request_id = request.get("id")
                    if isinstance(request_id, (dict, list)):
                        request_id = None
                        raise ValueError()
                    result = engine.call(request.get("method"), request.get("params", {}))
            except DriveError as exc:
                result = failure(exc.code, exc.message, exc.retryable, exc.next_action)
            except (ValueError, TypeError, UnicodeError):
                result = failure("INVALID_REQUEST", "Use one JSON object with id, method, and params per line.")
            print(json.dumps({"id": request_id, "result": result}, ensure_ascii=False, separators=(",", ":"), allow_nan=False), flush=True)
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
