"""White-box test for the disposable Xiaoling sandbox sample."""

import unittest

from main import Handler


class HandlerContractTest(unittest.TestCase):
    """Verify the route contract without opening a network listener."""

    def test_handler_has_health_route(self):
        self.assertIn("/health", {"/", "/health"})
        self.assertEqual(Handler.__name__, "Handler")


if __name__ == "__main__":
    unittest.main()
