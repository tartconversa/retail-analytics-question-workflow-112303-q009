import csv
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {'requests.csv': (['request_id', 'requester', 'department', 'question_text', 'region_scope', 'period_start', 'period_end', 'request_state'], 48), 'metric_catalog.csv': (['metric_id', 'metric_name', 'definition_version', 'grain', 'owner', 'valid_from', 'status'], 24), 'dataset_access.csv': (['dataset_id', 'region', 'sensitivity', 'purpose', 'requester_role', 'access_decision', 'policy_version'], 42), 'analysis_runs.csv': (['run_id', 'request_id', 'metric_id', 'dataset_id', 'data_version', 'coverage_state', 'analyst', 'delivered_at'], 54), 'actions.csv': (['action_id', 'run_id', 'business_owner', 'decision', 'accepted_at', 'due_at', 'completion_state', 'result_ref'], 60)}

class MaterialTests(unittest.TestCase):
    def test_declared_csv_contracts(self):
        for filename, (columns, count) in EXPECTED.items():
            with self.subTest(filename=filename):
                with (ROOT / "data" / filename).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                self.assertEqual(reader.fieldnames, columns)
                self.assertEqual(len(rows), count)
                self.assertTrue(all(None not in row for row in rows))

if __name__ == "__main__":
    unittest.main()
