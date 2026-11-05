"""Optional reference entrypoint (the runner invokes /work/fix.py directly)."""
from pathlib import Path
import runpy
import sys

target = Path("/work/fix.py")
if not target.exists():
    print("missing /work/fix.py", file=sys.stderr)
    sys.exit(2)
runpy.run_path(str(target), run_name="__main__")
