#!/usr/bin/env python
# Copyright 2025 Remy Blank <remy@c-space.org>
# SPDX-License-Identifier: MIT

import contextvars
import json
import os
import pathlib
import re
import subprocess
import shutil
import sys
import sysconfig
import threading
import time
import venv

# Set this variable to temporarily install a specific version.
VERSION = ''

package = 't-doc-common'
default_command = ['tdoc', 'serve', '--open']


def main(argv, stdin, stdout, stderr):
    base = pathlib.Path(argv[0]).parent.resolve()

    # Find a matching venv, or create one if there is none.
    version = os.environ.get('TDOC_VERSION', VERSION)
    builder = EnvBuilder(base, version, stderr, '--debug' in argv)
    envs, matching = builder.find()
    if not matching:
        stderr.write("Installing...\n")
        env = builder.new()
        env.create()
        stderr.write("\n")
    else:
        env = matching[0]

    # Remove old venvs.
    for e in envs:
        if e is not env: e.remove()

    # Check for upgrades, and upgrade if requested.
    cur, new = env.upgrade
    if new is not None:
        stderr.write(f"""\
An upgrade is available: {package} {cur} => {new}
Release notes: <https://common.t-doc.org/release-notes.html\
#release-{new.replace('.', '-')}>
""")
        if VERSION:
            stderr.write("Unset VERSION in run.py and restart the program to "
                         "upgrade.\n\n")
        elif version:
            stderr.write("Unset TDOC_VERSION and restart the program to "
                         "upgrade.\n\n")
        else:
            stderr.write("Would you like to install the upgrade (y/n)? ")
            stderr.flush()
            resp = input().lower()
            stderr.write("\n")
            if resp in ('y', 'yes', 'o', 'oui', 'j', 'ja'):
                stderr.write("Upgrading...\n")
                new_env = builder.new()
                try:
                    new_env.create()
                    env = new_env
                except Exception:
                    stderr.write("\nThe upgrade failed. Continuing with the "
                                 "current version.\n")
                stderr.write("\n")

    # Start the upgrade checker.
    threading.Thread(target=env.check_upgrade, daemon=True).start()

    # Run the command.
    args = argv[1:] if len(argv) > 1 else default_command
    args[0] = env.bin_path(args[0])
    return subprocess.run(args).returncode


class lazy:
    def __init__(self, fn):
        self.fn = fn

    def __get__(self, instance, owner=None):
        if instance is None: return self
        res = self.fn(instance)
        setattr(instance, self.fn.__name__, res)
        return res


class Namespace(dict):
    def __getattr__(self, name):
        return self[name]


class Env:
    requirements_txt = 'requirements.txt'
    upgrade_txt = 'upgrade.txt'

    env = contextvars.ContextVar('env')

    def __init__(self, path, builder):
        self.path, self.builder = path, builder

    @lazy
    def requirements(self):
        try:
            return (self.path / self.requirements_txt).read_text()
        except OSError:
            return None

    @lazy
    def sysinfo(self):
        vars = {'base': self.path, 'platbase': self.path,
                'installed_base': self.path, 'intsalled_platbase': self.path}
        return (pathlib.Path(sysconfig.get_path('scripts', scheme='venv',
                                                vars=vars)),
                sysconfig.get_config_vars().get('EXE', ''))

    def bin_path(self, name):
        scripts, ext = self.sysinfo
        return scripts / f'{name}{ext}'

    def create(self):
        self.builder.root.mkdir(exist_ok=True)
        token = self.env.set(self)
        try:
            self.builder.create(self.path)
        except BaseException:
            self.remove()
            raise
        finally:
            self.env.reset(token)

    def remove(self):
        try:
            if self.path.relative_to(self.builder.root) == pathlib.Path():
                raise Exception(f"{self.path}: Not below venv root")
            def on_error(fn, path, e):
                self.builder.out.write(f"ERROR: {fn}: {path}: {e}\n")
            shutil.rmtree(self.path, onexc=on_error)
        except Exception as e:
            self.builder.out.write(f"ERROR: {e}")

    def pip(self, *args, json_output=False, **kwargs):
        p = subprocess.run((self.bin_path('python'), '-P', '-m', 'pip',
                            '--require-virtualenv') + args,
                           stdin=subprocess.DEVNULL, **kwargs)
        if p.returncode != 0: raise Exception(p.stderr)
        if not json_output: return p.stdout
        return json.loads(p.stdout, object_pairs_hook=Namespace)

    @lazy
    def upgrade(self):
        try:
            upgrade = (self.path / self.upgrade_txt).read_text('utf-8')
            return upgrade.split(' ')[:2]
        except Exception:
            return None, None

    def check_upgrade(self):
        try:
            pkgs = self.pip('list', '--format=json',
                            capture_output=True, text=True, json_output=True)
            for pkg in pkgs:
                if pkg.name == package:
                    if pkg.get('editable_project_location') is not None: return
                    cur = pkg.version
                    break
            else:
                return
            data = self.pip('install', '--dry-run', '--quiet', '--upgrade',
                            '--upgrade-strategy=only-if-needed',
                            '--only-binary=:all:', '--report=-', package,
                            capture_output=True, text=True, json_output=True)
            upgrades = {pkg.metadata.name: pkg.metadata.version
                        for pkg in data.install}
            if (new := upgrades.get(package)) is None: return
            (self.path / self.upgrade_txt).write_text(f'{cur} {new}', 'utf-8')
            self.upgrade = cur, new
        except Exception:
            if self.builder.debug: raise


class EnvBuilder(venv.EnvBuilder):
    venv_root = '_venv'
    venv_dev = 'dev'
    venv_prefix = 'venv'

    def __init__(self, base, version, out, debug):
        super().__init__(with_pip=True)
        self.root = base / self.venv_root
        self.version, self.out, self.debug = version, out, debug
        self.requirements = (
            f'-e {base.as_uri()}' if version == 'dev'
            else version if version.startswith(('https:', 'file:'))
            else p.as_uri() if (p := pathlib.Path(version).resolve()).is_file()
            else f'{package}=={version}' if version else package)

    def find(self):
        pat = self.venv_dev if self.version == 'dev' \
              else f'{self.venv_prefix}-*'
        envs = [Env(path, self) for path in self.root.glob(pat)]
        envs.sort(key=lambda e: e.path, reverse=True)
        matching = [e for e in envs if e.requirements == self.requirements]
        return envs, matching

    def new(self):
        name = 'dev' if self.version == 'dev' \
               else f'{self.venv_prefix}-{time.time_ns():024x}'
        return Env(self.root / name, self)

    def post_setup(self, ctx):
        super().post_setup(ctx)
        env = Env.env.get()
        rpath = env.path / f'{env.requirements_txt}.tmp'
        rpath.write_text(self.requirements)
        env.pip('install', '--only-binary=:all:', '--requirement', rpath,
                check=True, stdout=self.out, stderr=self.out)
        rpath.rename(env.path / env.requirements_txt)


MAX_WPATH = 32768
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

own_process_re = re.compile(r'(?i).*\\(?:py|python[0-9.]*)\.exe$')


def maybe_wait_on_exit(stderr):
    if sys.platform != 'win32': return
    import ctypes.wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    DWORD = ctypes.wintypes.DWORD

    # Check if there are any other console processes besides our own
    # (python.exe and potentially py.exe).
    pids = (DWORD * 2)()
    count = kernel32.GetConsoleProcessList(pids, len(pids))
    if count > 2: return

    # There is at least one process besides python.exe. Check if it's py.exe or
    # the shell.
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    path = ctypes.create_unicode_buffer(MAX_WPATH)
    for pid in pids:
        h = kernel32.OpenProcess(
            DWORD(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ),
            False, pid)
        if h == 0: continue
        try:
            size = psapi.GetModuleFileNameExW(h, None, path, MAX_WPATH)
            if size == 0: continue
            if own_process_re.match(path[:size]) is not None: count -= 1
        finally:
            kernel32.CloseHandle(h)
    if count > 0: return
    stderr.write("\nPress ENTER to exit.")
    stderr.flush()
    input()


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv, sys.stdin, sys.stdout, sys.stderr))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(1)
    except BaseException as e:
        if '--debug' in sys.argv: raise
        sys.stderr.write(f'\nERROR: {e}\n')
        maybe_wait_on_exit(sys.stderr)
        sys.exit(1)
