"""Turning a bot submission (bot.py, a directory, or a .zip) into a
directory the runner can load from."""

import os
import shutil
import tempfile
import zipfile


def prepare_bot_dir(bot_path, tmp_root=None):
    """Returns (bot_dir, cleanup_dir): a directory holding bot.py (and an
    optional data/), and a temp dir to delete afterwards (or None).
    Accepts a directory, a .zip archive (extracted into a temp dir, with
    checks against absolute paths, traversal and symlinks), or a .py file
    (copied into a temp dir as bot.py). Temp dirs go under `tmp_root` if
    given (Docker VMs such as Colima only share the home directory).
    """
    if tmp_root:
        os.makedirs(tmp_root, exist_ok=True)
    p = os.path.abspath(bot_path)

    if os.path.isdir(p):
        return p, None

    if p.endswith(".zip") and os.path.isfile(p):
        tmpdir = tempfile.mkdtemp(prefix="arena_bot_", dir=tmp_root)
        with zipfile.ZipFile(p) as zf:
            for member in zf.infolist():
                name = member.filename
                if name.startswith("/") or name.startswith("\\"):
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    raise ValueError("Unsafe zip path (absolute): " + repr(name))
                norm = os.path.normpath(os.path.join(tmpdir, name))
                if not norm.startswith(tmpdir + os.sep) and norm != tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    raise ValueError("Unsafe zip path (traversal): " + repr(name))
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    raise ValueError("Unsafe zip path (symlink): " + repr(name))
            zf.extractall(tmpdir)
        if not os.path.isfile(os.path.join(tmpdir, "bot.py")):
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise ValueError("Zip archive must contain bot.py at the root")
        return tmpdir, tmpdir

    if p.endswith(".py") and os.path.isfile(p):
        tmpdir = tempfile.mkdtemp(prefix="arena_bot_", dir=tmp_root)
        shutil.copy(p, os.path.join(tmpdir, "bot.py"))
        return tmpdir, tmpdir

    raise ValueError("Unsupported bot path (must be .py, .zip, or directory): " + repr(p))
