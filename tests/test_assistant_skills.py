"""'open assistant' routing and the assistant skills (dry run, no processes started)."""
import unittest

from prowl.brain.prematch import match


class PrematchTest(unittest.TestCase):
    def test_open_assistant_variants(self):
        for u in ["open assistant", "Open assistent", "open the assistant", "please open my asistant",
                  "show me the assistant", "open assistant page"]:
            self.assertEqual(match(u), ("open_assistant", {}), u)

    def test_task_is_handed_over(self):
        self.assertEqual(match("open assistant and find me flights to Tokyo"),
                         ("browse", {"task": "find me flights to Tokyo"}))
        self.assertEqual(match("ask the assistant to compare AirPods prices"),
                         ("browse", {"task": "compare AirPods prices"}))

    def test_other_opens_unaffected(self):
        self.assertEqual(match("open Safari"), ("open_app", {"app": "Safari"}))
        self.assertEqual(match("open github.com")[0], "open_url")


class SkillDryRunTest(unittest.TestCase):
    def test_dry_run_starts_nothing(self):
        import logging
        from prowl.core.config import Config
        from prowl.core.context import Context
        from prowl.skills import load_all
        reg = load_all()
        ctx = Context(config=Config.load(), log=logging.getLogger("t"), speak=lambda t: None,
                      confirm=lambda q: False, dry_run=True)
        self.assertTrue(reg["open_assistant"].run({}, ctx).ok)
        r = reg["browse"].run({"task": "find flights"}, ctx)
        self.assertTrue(r.ok)
        self.assertIn("find flights", r.speech)
        self.assertFalse(reg["browse"].run({}, ctx).ok)


if __name__ == "__main__":
    unittest.main()
