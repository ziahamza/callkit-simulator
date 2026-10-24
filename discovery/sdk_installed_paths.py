#!/usr/bin/env python3
"""Read installed bytes from fresh, process-bound app launch paths, without RPC."""
import argparse
import json
from pathlib import Path
import plistlib
import uuid

from sdk_m0_conformance import bound_rows, digest
from summarize import one, require


def installed_bundle(root, name, identity, bundle):
    rows, _ = bound_rows(root, name, identity, bundle)
    raw = one(rows, 'launch').get('bundlePath')
    require(isinstance(raw, str) and bool(raw), name + ': missing launched bundle path')
    app = Path(raw)
    require(app.is_absolute() and app.is_dir() and app.resolve(strict=True) == app,
            name + ': noncanonical launched bundle path')
    require(len(app.parts) >= 8 and app.parts[-8:-2] ==
            ('Devices', identity['simulatorID'], 'data', 'Containers', 'Bundle', 'Application') and
            app.suffix == '.app', name + ': bundle outside owned simulator')
    uuid.UUID(app.parent.name)
    info = plistlib.loads((app / 'Info.plist').read_bytes())
    require(info.get('CFBundleIdentifier') == bundle, name + ': installed bundle identity differs')
    executable = info.get('CFBundleExecutable')
    require(isinstance(executable, str) and Path(executable).name == executable and bool(executable),
            name + ': invalid executable name')
    image = app / executable
    require(image.is_file() and image.resolve(strict=True).parent == app,
            name + ': executable escapes launched bundle')
    return app, image


def record(root, run_id, simulator):
    uuid.UUID(simulator)
    identity = dict(runID=run_id, simulatorID=simulator)
    app, image = installed_bundle(root, 'sdk-m0', identity, 'dev.iosvoipsdk.example')
    _, bootstrap = installed_bundle(root, 'bootstrap-healthy', identity, 'dev.callkitsim.sdkbootstrap')
    javascript = app / 'main.jsbundle'
    require(javascript.is_file() and javascript.resolve(strict=True).parent == app,
            'JavaScript escapes launched bundle')
    value = dict(example=digest(image), bootstrap=digest(bootstrap), javascript=digest(javascript))
    with (root / 'installed.json').open('x') as stream:
        stream.write(json.dumps(value, indent=2) + '\n')
    return value


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('run_id')
    parser.add_argument('simulator')
    args = parser.parse_args()
    record(args.root, args.run_id, args.simulator)
