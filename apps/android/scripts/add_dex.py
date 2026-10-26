"""把 classes.dex 塞进 aapt2 产出的 APK。

APK 就是 zip；必须保留每个条目的压缩方式，尤其 resources.arsc 不能重新压缩，
否则 Android 11+ 会因对齐/压缩要求拒绝安装。zipalign 之后再统一对齐。
"""

import sys
import zipfile
from pathlib import Path


def main(base_apk: str, dex: str, output: str) -> None:
    base = Path(base_apk)
    dex_path = Path(dex)
    out_path = Path(output)
    with zipfile.ZipFile(base, "r") as source, zipfile.ZipFile(
        out_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            entry = zipfile.ZipInfo(item.filename, date_time=item.date_time)
            entry.compress_type = item.compress_type
            entry.external_attr = item.external_attr
            target.writestr(entry, data)
        entry = zipfile.ZipInfo("classes.dex", date_time=(2026, 10, 18, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        target.writestr(entry, dex_path.read_bytes())
    print(f"{out_path} ({out_path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
