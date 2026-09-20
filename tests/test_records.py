import unittest
from pathlib import Path
from src.records import load_records

class RecordsTest(unittest.TestCase):
    def test_public_fixture(self):
        value = load_records(Path("fixtures/context.json"))
        self.assertEqual(value["domain"], "platform-data-quality")
        self.assertGreaterEqual(len(value["records"]), 2)

if __name__ == "__main__":
    unittest.main()
