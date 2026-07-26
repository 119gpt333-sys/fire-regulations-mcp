from __future__ import annotations

import argparse
import json

from .store import RegulatoryStore
from .sync import DEFAULT_DATA_DIR


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP 운영뷰에 반영할 법규 변경을 로컬에서 검토합니다."
    )
    parser.add_argument("action", choices=("list", "approve", "reject"))
    parser.add_argument("--source-type", choices=("law", "admrul"))
    parser.add_argument("--official-id")
    parser.add_argument("--version-id")
    parser.add_argument("--reviewer")
    parser.add_argument("--reason")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    args = parser.parse_args()

    store = RegulatoryStore(f"{args.data_dir}/index/fire_regulations.db")
    store.initialize()
    if args.action == "list":
        print(json.dumps(store.list_pending_changes(), ensure_ascii=False, indent=2))
        return

    required = {
        "--source-type": args.source_type,
        "--official-id": args.official_id,
        "--version-id": args.version_id,
        "--reviewer": args.reviewer,
        "--reason": args.reason,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        parser.error(f"{args.action}에는 {', '.join(missing)} 값이 필요합니다.")
    decision = "approved" if args.action == "approve" else "rejected"
    store.review_version(
        args.source_type,
        args.official_id,
        args.version_id,
        decision=decision,
        reviewer=args.reviewer,
        reason=args.reason,
    )
    print(
        json.dumps(
            {
                "source_type": args.source_type,
                "official_id": args.official_id,
                "version_id": args.version_id,
                "decision": decision,
                "reviewer": args.reviewer,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
