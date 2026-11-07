"""Stand-in for mkvmerge used by the tests: "joins" the parts by writing their
contents to the output, separated by |."""

import sys
from pathlib import Path

args = sys.argv[1:]
out = args[args.index("-o") + 1]
parts = [a for a in args if a.endswith(".mkv") and a != out]
Path(out).write_text("|".join(Path(p).read_text() for p in parts))
