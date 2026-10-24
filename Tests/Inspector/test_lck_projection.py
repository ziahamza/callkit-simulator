"""Compile the actual measured LCK Inspector projection on the host."""
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class LCKProjectionTests(unittest.TestCase):
    def test_authoritative_choices_passthrough_targets_and_separate_deadlines(self):
        with tempfile.TemporaryDirectory(prefix='lck-inspector-projection-') as directory:
            binary = Path(directory) / 'projection-tests'
            dependencies = [path for path in sorted((ROOT / 'Inspector').glob('*.swift'))
                            if path.name not in ('InspectorApp.swift', 'InspectorModel.swift')]
            compiled = subprocess.run(['swiftc', '-swift-version', '6', '-parse-as-library',
                *map(str, dependencies),
                str(ROOT / 'Tests/Inspector/LCKInspectorProjectionTests.swift'), '-o', str(binary)],
                text=True, capture_output=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            tested = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)


if __name__ == '__main__':
    unittest.main()
