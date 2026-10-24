"""Run the Inspector's real Foundation multi-call projection on the host."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class MultiCallProjectionTests(unittest.TestCase):
    def test_grouping_transactions_and_pending_constituents(self):
        with tempfile.TemporaryDirectory(prefix='inspector-multicall-') as work:
            binary = Path(work) / 'projection-tests'
            sources = [ROOT / 'Inspector' / name for name in
                       ('RuntimeEvent.swift', 'IncomingFilter.swift', 'MultiCallProjection.swift')]
            compiled = subprocess.run(['swiftc', '-swift-version', '6', '-parse-as-library',
                *map(str, sources), str(ROOT / 'Tests/Inspector/MultiCallProjectionTests.swift'), '-o', str(binary)],
                text=True, capture_output=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            tested = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)


if __name__ == '__main__':
    unittest.main()
