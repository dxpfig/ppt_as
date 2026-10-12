import argparse
import sys
from pathlib import Path
if len(sys.argv) > 1 and sys.argv[1] == "recover-source":
    from .source_archive import recover_source
    parser = argparse.ArgumentParser(description="Recover the original export-time PPTD snapshot; later PowerPoint edits are not included")
    parser.add_argument("command")
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(recover_source(args.pptx, args.output))
else:
    from .pptx import main
    main()
