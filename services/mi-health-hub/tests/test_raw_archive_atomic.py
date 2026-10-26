import gzip
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from mi_health_hub.sync.raw_archive import RawArchive


def save(archive):
    return archive.save("steps", 100, 200, [{"time": 100, "value": 123}], endpoint="/synthetic")


def test_interrupted_write_does_not_publish_or_poison_retry(tmp_path):
    archive = RawArchive(tmp_path)
    write = gzip.GzipFile.write

    def interrupted(stream, content):
        write(stream, content[:16])
        raise OSError("synthetic interrupted write")

    with patch.object(gzip.GzipFile, "write", interrupted), pytest.raises(OSError):
        save(archive)
    assert not list(tmp_path.rglob("*.json.gz"))
    assert not list(tmp_path.rglob("*.partial"))
    relative, digest, _ = save(archive)
    assert archive.stable_digest(archive.load(relative)) == digest
    assert (tmp_path / relative).stat().st_mode & 0o777 == 0o600


def test_existing_corrupt_archive_is_preserved_and_rebuilt(tmp_path):
    archive = RawArchive(tmp_path)
    relative, digest, _ = save(archive)
    target = tmp_path / relative
    target.write_bytes(b"synthetic interrupted gzip")
    rebuilt, rebuilt_digest, _ = save(archive)
    assert rebuilt == relative and rebuilt_digest == digest
    assert archive.stable_digest(archive.load(relative)) == digest
    quarantined = list(target.parent.glob(target.name + ".corrupt-*"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == b"synthetic interrupted gzip"


def test_valid_archive_deduplicates_without_rewrite(tmp_path):
    archive = RawArchive(tmp_path)
    relative, _, _ = save(archive)
    before = (tmp_path / relative).read_bytes()
    save(archive)
    assert (tmp_path / relative).read_bytes() == before


def test_parallel_identical_archives_are_always_complete(tmp_path):
    archive = RawArchive(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: save(archive), range(8)))
    assert len(set(results)) == 1
    relative, digest, _ = results[0]
    assert archive.stable_digest(archive.load(relative)) == digest
    assert not list(tmp_path.rglob("*.partial"))
