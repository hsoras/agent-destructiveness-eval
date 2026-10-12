#!/usr/bin/env python3
"""Report all assignments from a frozen manifest and native result records."""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.artifact_upload.reporting import report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--results', type=Path, required=True)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        if manifest.get('proxy', {}).get('mode') == 'llm' or manifest.get('operation') not in ('frozen', 'script'):
            raise ValueError('reporting accepts only frozen/script manifests')
        result = report(manifest, json.loads(args.results.read_text()))
        text = json.dumps(result, indent=2, allow_nan=False) + '\n'
        if args.output:
            with args.output.open('x') as stream:
                stream.write(text)
        else:
            print(text, end='')
        return 0
    except (OSError, ValueError, KeyError) as exc:
        p.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
