import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class RepoWorkflowContractTests(unittest.TestCase):
    def test_ci_gate_requires_validation_and_image_build(self) -> None:
        metadata = json.loads((REPO_ROOT / ".github/github.json").read_text(encoding="utf-8"))
        workflow = (REPO_ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8")

        self.assertEqual(["ci-gate"], metadata["requiredStatusChecks"])
        self.assertIn("name: ci-gate", workflow)
        self.assertIn("if: ${{ always() }}", workflow)
        self.assertIn("VALIDATE_RESULT: ${{ needs.validate.result }}", workflow)
        self.assertIn("IMAGE_RESULT: ${{ needs.image.result }}", workflow)
        self.assertIn('if [ "$VALIDATE_RESULT" != "success" ]', workflow)


if __name__ == "__main__":
    unittest.main()
