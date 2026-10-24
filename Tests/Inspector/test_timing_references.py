"""Exercise the Inspector's actual Foundation projection with the host Swift compiler."""
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class TimingReferenceTests(unittest.TestCase):
    def test_journal_projection(self):
        with tempfile.TemporaryDirectory(prefix='inspector-timing-') as work:
            binary = Path(work) / 'timing-tests'
            compiled = subprocess.run([
                'swiftc', '-swift-version', '6', '-parse-as-library',
                str(ROOT / 'Inspector/RuntimeEvent.swift'),
                str(ROOT / 'Inspector/RuntimeTimingReferences.swift'),
                str(ROOT / 'Tests/Inspector/TimingReferenceTests.swift'), '-o', str(binary),
            ], text=True, capture_output=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            tested = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)


if __name__ == '__main__':
    unittest.main()
