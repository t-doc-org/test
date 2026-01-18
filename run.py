#!/usr/bin/env python
# Copyright 2025 Remy Blank <remy@c-space.org>
# SPDX-License-Identifier: MIT

import contextlib
import pathlib
import sys
import tempfile
import threading
import time
from urllib import request

# The URL of the "common" repository.
REPO = 'https://github.com/t-doc-org/common'

# Use certifi if it's available.
ssl_ctx = None
with contextlib.suppress(ImportError):
    import ssl
    import certifi
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())


def main(argv, stdin, stdout, stderr):
    try:
        with Stage2(argv) as stage2:
            sys.exit(stage2.run(stdin, stdout, stderr))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(1)
    except BaseException as e:
        if '--debug' in sys.argv: raise
        sys.stderr.write(f'\nERROR: {e}\n')
        sys.exit(1)


class Stage2:
    venv_root = '_venv'
    run_stage2 = 'run-stage2.py'

    def __init__(self, argv):
        self.argv = argv
        self.base = pathlib.Path(argv[0]).parent.resolve()
        self.venv = self.base / self.venv_root
        self.updater = None

    def __enter__(self): return self

    def __exit__(self, value, typ, tb):
        if self.updater is not None:
            self.updater.join(self.wait_until - time.monotonic())

    def run(self, stdin, stdout, stderr):
        mod = self.get()
        return mod['main'](argv=self.argv, stdin=stdin, stdout=stdout,
                           stderr=stderr, base=self.base)

    def get(self):
        with contextlib.suppress(Exception):
            data = (self.venv / self.run_stage2).read_bytes()
            mod = self.exec(data)
            self.updater = threading.Thread(target=self.update, args=(data,),
                                            daemon=True)
            self.updater.start()
            self.wait_until = time.monotonic() + 5
            return mod
        data = self.fetch()
        mod = self.exec(data)
        self.write(data)
        return mod

    def fetch(self):
        with contextlib.suppress(Exception):
            return (self.base / 'config' / self.run_stage2).read_bytes()
        with request.urlopen(
                f'{REPO}/raw/refs/heads/main/config/{self.run_stage2}',
                context=ssl_ctx, timeout=30) as f:
            return f.read()

    def exec(self, data):
        path = self.venv / self.run_stage2
        code = compile(data.decode('utf-8'), str(path), 'exec')
        mod = {'__name__': path.stem, '__file__': str(path), '__cached__': None,
               '__doc__': None, '__loader__': None, '__package__': None,
               '__spec__': None}
        exec(code, mod)
        return mod

    def write(self, data):
        self.venv.mkdir(exist_ok=True)
        path = self.venv / self.run_stage2
        with tempfile.NamedTemporaryFile('wb', dir=path.parent,
                                         prefix=path.name + '.',
                                         delete_on_close=False) as f:
            f.write(data)
            f.close()
            pathlib.Path(f.name).replace(path)

    def update(self, cached):
        try:
            if (data := self.fetch()) == cached: return
            self.exec(data)  # Check that stage2 compiles and executes
            self.write(data)
        except Exception:
            if '--debug' in self.argv: raise


if __name__ == '__main__':
    main(sys.argv, sys.stdin, sys.stdout, sys.stderr)
