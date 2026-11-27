"""Build the Lambda ZIPs locally without contacting AWS."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
TARGETS = ("api", "worker")


def build(target: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{target}.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for package in (target, "common"):
            for path in sorted((SERVER / package).rglob("*.py")):
                archive.write(path, path.relative_to(SERVER).as_posix())
        if target == "api":
            for name in ("index.html", "app.css", "app.js"):
                archive.write(ROOT / "client" / "web" / name, f"api/web/{name}")
    return archive_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=(*TARGETS, "all"), default="all")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    for target in TARGETS if args.target == "all" else (args.target,):
        print(build(target, args.output_dir))
