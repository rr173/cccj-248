"""隔离模式测试入口：python3 -I /workspace/run_tests.py"""

import sys
import unittest

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/tests")

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module_name in ("acceptance_test", "trace_acceptance_test",
                        "cost_acceptance_test", "amendment_acceptance_test"):
        suite.addTests(loader.loadTestsFromName(module_name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
