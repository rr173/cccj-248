"""隔离模式测试入口：python3 -I /workspace/run_tests.py"""

import sys
import unittest

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/tests")

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("acceptance_test")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
