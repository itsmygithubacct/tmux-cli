"""parse_ui_blocks: slot parsing and error taxonomy."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.extensions.registry import (  # noqa: E402
    RegistryConflict,
    UIBlocksError,
    parse_ui_blocks,
)


class ParseUiBlocksTests(unittest.TestCase):

    def test_parses_slots(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ui_blocks.html"
            p.write_text(
                "ignored preamble\n"
                "<!-- {{header}} -->\n"
                "<h1>hi</h1>\n"
                "<!-- {{footer}} -->\n"
                "<p>bye</p>\n"
            )
            blocks = parse_ui_blocks(p)
            self.assertEqual(set(blocks), {"header", "footer"})
            self.assertIn("<h1>hi</h1>", blocks["header"])
            self.assertIn("<p>bye</p>", blocks["footer"])

    def test_missing_file_raises_uiblockserror_not_conflict(self):
        missing = Path(tempfile.gettempdir()) / "definitely_absent_ui_blocks.html"
        with self.assertRaises(UIBlocksError):
            parse_ui_blocks(missing)
        # The read failure must not masquerade as a name collision.
        self.assertFalse(issubclass(UIBlocksError, RegistryConflict))


if __name__ == "__main__":
    unittest.main()
