#!/usr/bin/env python3
"""Run the fixed single-page template benchmark."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_service.single_page_benchmark import BenchmarkError, execute


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--cases',
        type=Path,
        default=ROOT / 'benchmarks/single-page-template/cases.v1.json',
    )
    parser.add_argument(
        '--inputs',
        type=Path,
        default=ROOT / 'benchmarks/single-page-template/inputs.v1.json',
    )
    parser.add_argument(
        '--output-root',
        type=Path,
        default=ROOT / 'runtime/benchmarks/single-page-template',
    )
    parser.add_argument(
        '--case',
        action='append',
        help='用例 ID，可重复或使用逗号分隔；省略时运行全部 10 个用例。',
    )
    parser.add_argument('--mode', choices=('smoke', 'formal'), default='smoke')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        return execute(
            cases_path=args.cases.resolve(),
            inputs_path=args.inputs.resolve(),
            output_root=args.output_root.resolve(),
            case_ids=args.case,
            mode=args.mode,
            dry_run=args.dry_run,
        )
    except BenchmarkError as exc:
        print(f'基准运行失败：{exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
