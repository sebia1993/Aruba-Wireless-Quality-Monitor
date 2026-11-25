import unittest
from tools.check_dependency_audit import evaluate

class DependencyAuditTests(unittest.TestCase):
    def test_exact_inventory(self):
        report={"dependencies":[{"name":"example_pkg","version":"1.0","vulns":[]},{"name":"another","version":"2.1","vulns":[]}]}
        freeze="Example-Pkg==1.0\nanother==2.1\n"
        self.assertEqual(evaluate(freeze,report,0)["result"],"pass")
        self.assertEqual(evaluate(freeze,report,1)["result"],"blocked")
        self.assertIn("missing_packages",evaluate(freeze,{"dependencies":report["dependencies"][:1]},0)["issues"])
        report["dependencies"][1]["version"]="2.0"
        self.assertIn("version_mismatch",evaluate(freeze,report,0)["issues"])
    def test_skipped_and_advisory_not_success(self):
        self.assertIn("skipped_packages",evaluate("foo==1",{"dependencies":[{"name":"foo","skip_reason":"not found"}]},0)["issues"])
        self.assertIn("known_vulnerabilities",evaluate("foo==1",{"dependencies":[{"name":"foo","version":"1","vulns":[{"id":"GHSA-fixture","fix_versions":["2"]}]}]},1)["issues"])
    def test_unpinned_duplicate_empty(self):
        for freeze in ("foo>=1","foo==1\nfoo==2",""):
            with self.assertRaises(ValueError):evaluate(freeze,{"dependencies":[]},0)
        with self.assertRaises(ValueError):evaluate("foo==1",{"dependencies":[{"name":"foo","version":"1","vulns":[]},{"name":"foo","version":"1","vulns":[]}]},0)
