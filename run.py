#!/usr/bin/env python
# Copyright 2025 Remy Blank <remy@c-space.org>
# SPDX-License-Identifier: MIT

import contextlib
import contextvars
import functools
import json
import os
import pathlib
import re
import subprocess
import shutil
import sys
import sysconfig
import tempfile
import threading
import time
import tomllib
from urllib import request
import venv

# The URL of the default config.
CONFIG = 'https://github.com/t-doc-org/common' \
         '/raw/refs/heads/main/config/t-doc.toml'


def main(argv, stdin, stdout, stderr):
    base = pathlib.Path(argv[0]).parent.resolve()

    # Parse command-line options.
    config = os.environ.get('TDOC_CONFIG', CONFIG)
    debug = False
    print_key = None
    version = os.environ.get('TDOC_VERSION')
    i = 1
    while True:
        if i >= len(argv) or not (arg := argv[i]).startswith('--'):
            argv = argv[:1] + argv[i:]
            break
        elif arg == '--':
            argv = argv[:1] + argv[i + 1:]
            break
        elif arg.startswith('--config='):
            config = arg[9:]
        elif arg == '--debug':
            debug = True
        elif arg.startswith('--print='):
            print_key = arg[8:]
        elif arg.startswith('--version='):
            version = arg[10:]
        else:
            raise Exception(f"Unknown option: {arg}")
        i += 1

    # Create a environment builder.
    builder = EnvBuilder(base, config, version, stderr,
                         debug or '--debug' in argv)
    if print_key is not None:
        v = builder.config
        try:
            for k in print_key.split('.'): v = v[k]
        except KeyError:
            return 1
        stdout.write(str(v))
        return 0

    # Find a matching venv, or create one if there is none.
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
    reqs, reqs_up = env.requirements, env.requirements_upgrade
    if reqs_up is not None and reqs_up != reqs:
        cur, new = builder.version_from(reqs), builder.version_from(reqs_up)
        stderr.write(f"""\
An upgrade is available: {builder.package} {cur} => {new}
Release notes: <https://common.t-doc.org/release-notes.html\
#release-{new.replace('.', '-')}>
""")
        stderr.write("Would you like to install the upgrade (y/n)? ")
        stderr.flush()
        resp = input().lower()
        stderr.write("\n")
        if resp in ('y', 'yes', 'o', 'oui', 'j', 'ja'):
            stderr.write("Upgrading...\n")
            new_env = builder.new()
            try:
                new_env.create(reqs_up)
                env = new_env
            except Exception:
                stderr.write("\nThe upgrade failed. Continuing with the "
                             "current version.\n")
            stderr.write("\n")

    # Check for upgrades.
    wait_until = None
    if not (is_dev(builder.version) or is_wheel(builder.version)):
        checker = threading.Thread(target=env.check_upgrade, daemon=True)
        checker.start()
        wait_until = time.monotonic() + 5

    # Run the command.
    try:
        args = argv[1:] if len(argv) > 1 \
               else builder.config['defaults']['command-dev'] \
               if is_dev(builder.version) \
               else builder.config['defaults']['command']
        args[0] = env.bin_path(args[0])
        rc = subprocess.run(args).returncode
    except (Exception, KeyboardInterrupt):
        rc = 1

    # Give the upgrade checker a chance to run at least for a bit.
    if wait_until is not None: checker.join(wait_until - time.monotonic())
    return rc


class Namespace(dict):
    def __getattr__(self, name):
        return self[name]


unset = object()

def merge_dict(dst, src):
    for k, sv in src.items():
        dv = dst.get(k, unset)
        if isinstance(sv, dict) and isinstance(dv, dict):
            merge_dict(dv, sv)
        else:
            dst[k] = sv


version_re = re.compile(r'[0-9][0-9a-z.!+]*')
tag_re = re.compile(r'[a-z][a-z0-9-]*')

def is_version(version): return version_re.match(version)
def is_tag(version): return tag_re.match(version)
def is_dev(version): return version == 'dev'
def is_wheel(version): return pathlib.Path(version).resolve().is_file()


@contextlib.contextmanager
def write_atomic(path, *args, **kwargs):
    with tempfile.NamedTemporaryFile(*args, dir=path.parent,
                                     prefix=path.name + '.',
                                     delete_on_close=False, **kwargs) as f:
        yield f
        f.close()
        pathlib.Path(f.name).replace(path)


class Env:
    requirements_txt = 'requirements.txt'
    requirements_deps_txt = 'requirements-deps.txt'
    requirements_upgrade_txt = 'requirements-upgrade.txt'
    upgrade_txt = 'upgrade.txt'

    env = contextvars.ContextVar('env')

    def __init__(self, path, builder):
        self.path, self.builder = path, builder

    @functools.cached_property
    def requirements(self):
        try:
            return (self.path / self.requirements_txt).read_text()
        except OSError:
            return None

    @functools.cached_property
    def requirements_upgrade(self):
        try:
            return (self.path / self.requirements_upgrade_txt).read_text()
        except OSError:
            return None

    @functools.cached_property
    def sysinfo(self):
        vars = {'base': self.path, 'platbase': self.path,
                'installed_base': self.path, 'intsalled_platbase': self.path}
        return (pathlib.Path(sysconfig.get_path('scripts', scheme='venv',
                                                vars=vars)),
                sysconfig.get_config_vars().get('EXE', ''))

    def bin_path(self, name):
        scripts, ext = self.sysinfo
        return scripts / f'{name}{ext}'

    def create(self, requirements=None):
        self.builder.root.mkdir(exist_ok=True)
        token = self.env.set((self, requirements))
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

    def uv(self, *args, **kwargs):
        p = subprocess.run((self.bin_path('uv'),) + args,
                           stdin=subprocess.DEVNULL, text=True, **kwargs)
        if p.returncode != 0: raise Exception(p.stderr)
        return p.stdout

    def pip(self, *args, json_output=False, **kwargs):
        p = subprocess.run((self.bin_path('python'), '-P', '-m', 'pip',
                            '--require-virtualenv') + args,
                           stdin=subprocess.DEVNULL, **kwargs)
        if p.returncode != 0: raise Exception(p.stderr)
        if not json_output: return p.stdout
        return json.loads(p.stdout, object_pairs_hook=Namespace)

    def check_upgrade(self):
        try:
            config = self.builder.fetch_config()
            reqs = self.builder.requirements(config=config)
            if reqs != self.requirements:
                with write_atomic(
                        self.path / self.requirements_upgrade_txt, 'w') as f:
                    f.write(reqs)
                # TODO(0.62): Remove
                cur = self.builder.version_from(self.requirements)
                new = self.builder.version_from(reqs)
                (self.path / self.upgrade_txt).write_text(f'{cur} {new}',
                                                          'utf-8')
        except Exception:
            if self.builder.debug: raise


class EnvBuilder(venv.EnvBuilder):
    venv_root = '_venv'
    config_toml = 't-doc.toml'

    def __init__(self, base, config, version, out, debug):
        super().__init__(with_pip=True)
        self.base = base
        self.root = base / self.venv_root
        self.config_url = config
        self.out, self.debug = out, debug
        if version is None: version = self.config['version']
        if not (is_version(version) or is_tag(version) or is_wheel(version)):
            raise Exception(f"Invalid version: {version}")
        self.version = version

    @functools.cached_property
    def package(self):
        return self.config['package']

    @functools.cached_property
    def config(self):
        try:
            data = (self.root / self.config_toml).read_bytes()
            config = tomllib.loads(data.decode('utf-8'))
        except Exception:
            return self.fetch_config()
        self.merge_local_config(config)
        return config

    def fetch_config(self):
        if self.config_url.startswith('https://'):
            with request.urlopen(self.config_url, timeout=30) as f:
                data = f.read()
        else:
            data = pathlib.Path(self.config_url).read_bytes()
        config = tomllib.loads(data.decode('utf-8'))
        self.root.mkdir(exist_ok=True)
        with write_atomic(self.root / self.config_toml, 'wb') as f:
            f.write(data)
        self.merge_local_config(config)
        return config

    def merge_local_config(self, config):
        with contextlib.suppress(OSError), \
                (self.base / self.config_toml).open('rb') as f:
            merge_dict(config, tomllib.load(f))

    def requirements(self, config=None):
        if is_dev(v := self.version): return f'-e {self.base.as_uri()}\n'
        if is_wheel(v): return f'{pathlib.Path(v).resolve().as_uri()}\n'
        if config is None: config = self.config
        version_num = config.get('tags', {}).get(v) if is_tag(v) else v
        if version_num is None:
            raise Exception(f"Unknown version tag: {v}\nAvailable tags: "
                            f"{' '.join(sorted(config['tags'].keys()))}")
        return f'{config['package']}=={version_num}\n'

    def version_from(self, requirements):
        pat = re.compile(f'^{re.escape(self.package)}==([^\\s;]+)$')
        if (m := pat.search(requirements)) is not None: return m.group(1)

    def find(self):
        pat = 'dev' if is_dev(self.version) \
              else 'wheel' if is_wheel(self.version) else f'{self.version}-*'
        envs = [Env(path, self) for path in self.root.glob(pat)]
        envs.sort(key=lambda e: e.path, reverse=True)
        matching = [e for e in envs if e.requirements is not None]
        return envs, matching

    def new(self):
        name = 'dev' if is_dev(self.version) \
               else 'wheel' if is_wheel(self.version) \
               else f'{self.version}-{time.time_ns():024x}'
        return Env(self.root / name, self)

    def post_setup(self, ctx):
        super().post_setup(ctx)
        env, requirements = Env.env.get()
        pip_args = []
        if is_dev(self.version):
            uv_req = self.base / 'config' / 'uv.req'
            env.pip('install', '--require-hashes', '--only-binary=:all:',
                    '--no-deps', f'--requirement={uv_req}',
                    check=True, stdout=self.out, stderr=self.out)
            rdpath = env.path / env.requirements_deps_txt
            env.uv('export', '--frozen', '--no-emit-project',
                   '--format=requirements-txt', f'--output-file={rdpath}',
                   cwd=self.base, capture_output=True)
            env.pip('install', '--require-hashes', '--only-binary=:all:',
                    '--no-deps', f'--requirement={rdpath}',
                    check=True, stdout=self.out, stderr=self.out)
            pip_args.append('--no-deps')

        if requirements is None: requirements = self.requirements()
        with write_atomic(env.path / env.requirements_txt) as f:
            rpath = pathlib.Path(f.name)
            rpath.write_text(requirements)
            env.pip('install', '--only-binary=:all:', f'--requirement={rpath}',
                    *pip_args, check=True, stdout=self.out, stderr=self.out)


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
