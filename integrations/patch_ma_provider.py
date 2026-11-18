"""Prepare an overlay for the exact Music Assistant 2.10.12 Sendspin provider."""
import argparse
from hashlib import sha256
from pathlib import Path
import shutil

BASE_SHA = "a0c3d1a9305bdfd17999a1ff26932503c4e051639fcc02d895719c50369f1389"


def patch(contents):
    if sha256(contents).hexdigest() != BASE_SHA:
        raise ValueError("Provider hash differs from the reviewed Music Assistant 2.10.12 source")
    source = contents.decode()
    source = source.replace("from .constants import (",
                            "from .record_relay_metadata import start as start_record_relay_metadata\n\nfrom .constants import (", 1)
    source = source.replace("        self._unloading = False\n",
                            "        self._unloading = False\n        self._record_relay_task = None\n", 1)
    anchor = "    async def unload(self, is_removed: bool = False) -> None:"
    source = source.replace(anchor,
        "        self._record_relay_task = start_record_relay_metadata(self, StreamMetadata)\n\n" + anchor, 1)
    source = source.replace("        self._unloading = True\n",
        """        self._unloading = True
        if task := self._record_relay_task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._record_relay_task = None
""", 1)
    compile(source, "provider.py", "exec")
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = patch(args.provider.read_bytes())
    args.output.mkdir(mode=0o700)
    (args.output / "provider.py").write_text(source)
    shutil.copyfile(Path(__file__).with_name("ma_metadata_bridge.py"),
                    args.output / "record_relay_metadata.py")


if __name__ == "__main__":
    main()
