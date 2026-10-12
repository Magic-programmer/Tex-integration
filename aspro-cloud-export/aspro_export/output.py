import csv
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from .api import ExportError


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(path, headers, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=",", lineterminator="\n")
        writer.writerow(headers)
        count = 0
        for row in rows:
            if len(row) != len(headers):
                raise ExportError("CSV row width differs from header")
            writer.writerow(row)
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def validate_csv(path, headers, count):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        if next(reader, None) != headers:
            raise ExportError("CSV header validation failed")
        actual = 0
        for row in reader:
            if len(row) != len(headers):
                raise ExportError("CSV row validation failed")
            actual += 1
        if actual != count:
            raise ExportError("CSV record count validation failed")


class Snapshot:
    """Publish the entire validated generation via a single current symlink."""
    def __init__(self, output_dir):
        self.root = Path(output_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.generations = self.root / "generations"
        self.generations.mkdir(exist_ok=True)
        self.stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=self.generations))
        self.published = False

    def commit(self, reports):
        manifest = {"reports": []}
        for name, headers, count in reports:
            path = self.stage / name
            validate_csv(path, headers, count)
            manifest["reports"].append({"file": name, "rows": count,
                "sha256": sha256_file(path)})
        if not reports:
            raise ExportError("Cannot publish an empty report set")
        with (self.stage / "manifest.json").open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(self.stage)
        final = self.generations / uuid.uuid4().hex
        os.replace(self.stage, final)
        fsync_directory(self.generations)
        link = self.root / (".current-" + uuid.uuid4().hex)
        try:
            link.symlink_to(final.relative_to(self.root), target_is_directory=True)
            os.replace(link, self.root / "current")
            fsync_directory(self.root)
            self.published = True
        finally:
            link.unlink(missing_ok=True)
        # Keep the previous generation for recovery. No date-based archive.
        others = sorted((p for p in self.generations.iterdir() if p.is_dir() and p != final
                         and not p.name.startswith(".pending-")), key=lambda p: p.stat().st_mtime,
                        reverse=True)
        for old in others[1:]:
            shutil.rmtree(old)

    def close(self):
        if self.stage.exists():
            shutil.rmtree(self.stage)
