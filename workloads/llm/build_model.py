#!/usr/bin/env python3
"""Build run-local simulator corrections with the native simulator ABI."""
import argparse
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from prepare import PACKAGE, sha256


def read_flags(path):
    flags = {}
    for line in path.read_text().splitlines():
        if line.startswith('CXX_'):
            key, value = line.split(' = ', 1)
            flags[key] = shlex.split(value)
    return flags


def build_engine(workdir, stage, models, compiler):
    """Relink engine variants with one corrected source and the existing ABI."""
    native = workdir / 'build/engine/engine'
    output = models / 'engine'
    objects = output / 'objects'
    objects.mkdir(parents=True)
    metadata = {}
    for target, library in [('gvsoc', 'pulpvp'), ('gvsoc_asserts', 'pulpvp-asserts'),
                            ('gvsoc_debug', 'pulpvp-debug'), ('gvsoc_profile', 'pulpvp-profile')]:
        cmake = native / 'CMakeFiles' / (target + '.dir')
        flags = read_flags(cmake / 'flags.make')
        obj = objects / (target + '.o')
        compile_command = [compiler, *flags['CXX_DEFINES'], *flags['CXX_INCLUDES'], *flags['CXX_FLAGS'],
                           '-c', str(stage / 'power_trace.cpp'), '-o', str(obj)]
        subprocess.run(compile_command, check=True)
        command = shlex.split((cmake / 'link.txt').read_text())
        command[command.index('-o') + 1] = str(output / ('lib' + library + '.so'))
        replaced = [x for x in command if x.endswith('/src/power/power_trace.cpp.o')]
        if len(replaced) != 1:
            raise ValueError('native engine link recipe is incompatible; rebuild with --build-hardware')
        command = [str(obj) if x == replaced[0] else x for x in command]
        metadata[target] = dict(compile_command=compile_command, link_command=command,
            object_sha256={x: sha256(native / x) for x in command if x.endswith('.o')})
        subprocess.run(command, cwd=native, check=True)
    (stage / 'engine-build.json').write_text(json.dumps(metadata, indent=2) + '\n')
    # The installed shell frontend prepends its own library path. Use an
    # equivalent run-local entry point which prioritizes the corrected engine.
    install = workdir / 'install'
    frontend = '#!/usr/bin/env bash\nset -e\n'
    frontend += 'export LD_LIBRARY_PATH=' + shlex.quote(str(output)) + ':' + shlex.quote(str(install / 'lib')) + ':${LD_LIBRARY_PATH:-}\n'
    frontend += 'export PYTHONPATH=' + shlex.quote(str(install / 'python')) + ':${PYTHONPATH:-}\n'
    frontend += 'export PATH=' + shlex.quote(str(install / 'bin')) + ':$PATH\n'
    frontend += 'exec ' + shlex.quote(str(install / 'bin/gapy')) + ' --platform=gvsoc --target-dir=' + shlex.quote(str(install / 'generators')) + ' --model-dir=' + shlex.quote(str(install / 'models')) + ' "$@"\n'
    (stage / 'gvsoc').write_text(frontend)
    (stage / 'gvsoc').chmod(0o755)
    shutil.copy2(stage / 'gvsoc', output / 'gvsoc')


def build_model(softhier, workdir, stage, models):
    source = softhier / 'pulp/pulp/chips/soft_hier_old/priority_arbiter_filter.cpp'
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, stage / source.name)
    shutil.copy2(softhier / "engine/engine/src/power/power_trace.cpp", stage / "power_trace.cpp")
    original_noc = source.parent / 'floonoc'
    shutil.copytree(original_noc, stage / 'floonoc',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for patch in sorted((PACKAGE / 'simulator').glob('*.patch')):
        subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-i', str(patch)], cwd=stage, check=True)
    native = workdir / 'build'
    cache = (native / 'CMakeCache.txt').read_text()
    compiler = re.search(r'^CMAKE_CXX_COMPILER:FILEPATH=(.+)$', cache, re.M)
    if not compiler:
        raise ValueError('native CMake build has no C++ compiler; rebuild with --build-hardware')
    components = [
        ('priority_arbiter_filter', 'llm_priority_arbiter', [stage / source.name]),
        ('floonoc_floonoc', 'llm_floonoc', [stage / 'floonoc' / name for name in
         ('floonoc.cpp', 'floonoc_router.cpp', 'floonoc_network_interface.cpp')]),
    ]
    for component, model, sources in components:
        for variant, directory, library in [('optim', '', 'pulpvp'), ('debug', 'debug', 'pulpvp-debug'),
                                            ('asserts', 'asserts', 'pulpvp-asserts'), ('profile', 'profile', 'pulpvp-profile')]:
            candidates = list((native / 'engine/CMakeFiles').glob(f'gen_pulp_chips_soft_hier_old_{component}_cpp_*_{variant}.dir/flags.make'))
            if len(candidates) != 1:
                raise ValueError(f'expected one native {component}/{variant} build; rebuild with --build-hardware')
            flags = read_flags(candidates[0])
            output = models / directory / (model + '.so')
            output.parent.mkdir(parents=True, exist_ok=True)
            # Reuse the simulator compiler, ABI flags and headers. The installed
            # engine and original models are never replaced.
            command = [compiler[1], *flags['CXX_DEFINES'], *flags['CXX_INCLUDES'], *flags['CXX_FLAGS'],
                       '-shared', *map(str, sources), '-o', str(output),
                       '-L' + str(workdir / 'install/lib'), '-l' + library,
                       '-Wl,-rpath,' + str(workdir / 'install/lib'), '-lz', '-lpthread', '-ldl']
            subprocess.run(command, check=True)

    build_engine(workdir, stage, models, compiler[1])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('softhier', 'workdir', 'stage', 'models'):
        parser.add_argument('--' + key, type=Path, required=True)
    args = parser.parse_args()
    build_model(args.softhier, args.workdir, args.stage, args.models)
