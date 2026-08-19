import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from breg_watch.brreg import RegisteredEntity, TransientBrregError
from breg_watch.cli import (
    PUBLIC_RUNNER_ENV,
    _enforce_public_runner_policy,
    _summary_for_output,
    directory_size,
    main,
)


class CliAndWorkflowTests(unittest.TestCase):
    def test_online_validation_updates_names_and_deactivates_unknown_entities(self):
        class FakeRegistryClient:
            def registered_entity(self, orgnr):
                return {
                    "000000019": RegisteredEntity("000000019", "FIKTIVT SELSKAP AS", "active"),
                    "000000027": RegisteredEntity("000000027", None, "unknown"),
                }[orgnr]

        with tempfile.TemporaryDirectory() as directory:
            companies = Path(directory) / "companies.csv"
            companies.write_text(
                "orgnr,name,active\n"
                "000000019,FIKTIVT GAMMELT NAVN AS,true\n"
                "000000027,FIKTIVT ANNET SELSKAP AS,true\n",
                encoding="utf-8",
            )
            output = StringIO()
            with patch("breg_watch.cli.BrregClient", return_value=FakeRegistryClient()):
                with redirect_stdout(output):
                    result = main(
                        [
                            "validate",
                            "--companies",
                            str(companies),
                            "--online",
                            "--update",
                        ]
                    )

            self.assertEqual(result, 0)
            self.assertEqual(
                companies.read_text(encoding="utf-8"),
                "orgnr,name,active\n"
                "000000019,FIKTIVT SELSKAP AS,true\n"
                "000000027,FIKTIVT ANNET SELSKAP AS,false\n",
            )
            self.assertIn('"old_name": "FIKTIVT GAMMELT NAVN AS"', output.getvalue())
            self.assertIn('"reason": "unknown"', output.getvalue())

    def test_online_validation_does_not_rewrite_csv_after_network_failure(self):
        class FailingRegistryClient:
            def registered_entity(self, orgnr):
                raise TransientBrregError("temporary failure")

        with tempfile.TemporaryDirectory() as directory:
            companies = Path(directory) / "companies.csv"
            original = "orgnr,name,active\n000000019,FIKTIVT SELSKAP AS,true\n"
            companies.write_text(original, encoding="utf-8")
            errors = StringIO()
            with patch("breg_watch.cli.BrregClient", return_value=FailingRegistryClient()):
                with redirect_stderr(errors):
                    result = main(
                        [
                            "validate",
                            "--companies",
                            str(companies),
                            "--online",
                            "--update",
                        ]
                    )

            self.assertEqual(result, 2)
            self.assertEqual(companies.read_text(encoding="utf-8"), original)
            self.assertIn("temporary failure", errors.getvalue())

    def test_validate_rebuild_render_and_budget_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            companies = root / "companies.csv"
            companies.write_text(
                "orgnr,name,active\n000000019,Eksempel ASA,false\n", encoding="utf-8"
            )
            data = root / "data"
            site = root / "site"
            db = root / "state" / "monitor.sqlite"

            self.assertEqual(main(["validate", "--companies", str(companies)]), 0)
            self.assertEqual(
                main(
                    [
                        "rebuild",
                        "--companies",
                        str(companies),
                        "--metadata-dir",
                        str(data),
                        "--db",
                        str(db),
                    ]
                ),
                0,
            )
            self.assertEqual(main(["render", "--db", str(db), "--site-dir", str(site)]), 0)
            self.assertTrue((site / "index.html").is_file())
            self.assertEqual(directory_size([data, site]), (site / "index.html").stat().st_size)
            self.assertEqual(
                main(
                    [
                        "budget-check",
                        "--max-bytes",
                        "1",
                        str(data),
                        str(site),
                    ]
                ),
                2,
            )

            original_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                self.assertEqual(main(["budget-check"]), 0)
            finally:
                os.chdir(original_cwd)

    def test_run_exit_code_reflects_summary_status(self):
        for status, expected in (("success", 0), ("partial", 1)):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    companies = root / "companies.csv"
                    companies.write_text(
                        "orgnr,name,active\n000000019,Eksempel ASA,false\n",
                        encoding="utf-8",
                    )
                    output = StringIO()
                    with patch(
                        "breg_watch.cli.MonitorService.run",
                        return_value={"status": status},
                    ):
                        with redirect_stdout(output):
                            result = main(
                                [
                                    "run",
                                    "--companies",
                                    str(companies),
                                    "--metadata-dir",
                                    str(root / "data"),
                                    "--db",
                                    str(root / "state" / "monitor.sqlite"),
                                    "--documents-dir",
                                    str(root / "documents"),
                                    "--site-dir",
                                    str(root / "site"),
                                ]
                            )

                    self.assertEqual(result, expected)
                    self.assertIn(f'"status": "{status}"', output.getvalue())

    def test_public_ci_is_read_only_and_has_no_secrets(self):
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("companies.example.csv", workflow)
        self.assertIn("python3 scripts/check_public_safety.py", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("issues: write", workflow)
        self.assertNotIn("actions: write", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("upload-artifact", workflow)

    def test_public_monitor_is_fail_closed_and_private_only(self):
        workflow = Path(".github/workflows/monitor.yml").read_text(encoding="utf-8")
        self.assertIn("schedule:", workflow)
        self.assertEqual(workflow.count("cron:"), 1)
        self.assertIn('cron: "47 * * * *"', workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("pull_request_target:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("issues: write", workflow)
        self.assertNotIn("actions: write", workflow)
        self.assertIn("repository: marmarmar-code/Breg-bot-runtime", workflow)
        self.assertIn("ssh-key: ${{ secrets.RUNTIME_DEPLOY_KEY }}", workflow)
        self.assertIn(
            "BREG_COMPANIES_CSV_B64: ${{ secrets.BREG_COMPANIES_CSV_B64 }}",
            workflow,
        )
        self.assertIn("SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}", workflow)
        self.assertIn('BREG_PUBLIC_RUNNER: "1"', workflow)
        self.assertIn("umask 077", workflow)
        self.assertIn("base64 --decode > companies.csv", workflow)
        self.assertIn("::add-mask::", workflow)
        self.assertIn("--archive local", workflow)
        self.assertIn("--notify slack", workflow)
        self.assertIn("--redact-output", workflow)
        self.assertNotIn("--archive github", workflow)
        self.assertNotIn("--notify github", workflow)
        self.assertNotIn("gh release", workflow)
        self.assertNotIn("gh issue", workflow)
        self.assertNotIn("inputs.orgnr", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertNotIn("actions/cache", workflow)
        self.assertNotIn("GITHUB_TOKEN:", workflow)
        self.assertNotIn("Private Git data size", workflow)
        self.assertNotIn("--online", workflow)
        self.assertIn("git add data site", workflow)
        self.assertIn(
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            workflow,
        )

    def test_public_runner_policy_rejects_unsafe_modes(self):
        safe = SimpleNamespace(
            archive="local", notify="slack", redact_output=True, orgnr=None
        )
        unsafe = [
            SimpleNamespace(archive="github", notify="slack", redact_output=True, orgnr=None),
            SimpleNamespace(archive="local", notify="github", redact_output=True, orgnr=None),
            SimpleNamespace(archive="local", notify="slack", redact_output=False, orgnr=None),
            SimpleNamespace(archive="local", notify="slack", redact_output=True, orgnr="000000019"),
        ]
        with patch.dict("os.environ", {PUBLIC_RUNNER_ENV: "1"}, clear=False):
            _enforce_public_runner_policy(safe)
            for args in unsafe:
                with self.subTest(args=args), self.assertRaises(ValueError):
                    _enforce_public_runner_policy(args)

    def test_redacted_output_contains_only_status(self):
        summary = {
            "status": "partial",
            "run_id": "private-run-id",
            "requested_orgnr": "000000019",
            "checked": 1,
            "new_filings": 1,
            "errors": [{"orgnr": "000000019", "kind": "TransientBrregError"}],
        }
        visible = _summary_for_output(summary, redact=True)
        self.assertEqual(visible, {"status": "partial"})
        self.assertNotIn("000000019", str(visible))
        self.assertEqual(_summary_for_output(summary, redact=False), summary)

    def test_gitignore_excludes_working_database_and_local_documents(self):
        gitignore = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn("state/", gitignore)
        self.assertIn("documents/", gitignore)
        self.assertIn(".env", gitignore)
        self.assertIn("*.log", gitignore)
        self.assertIn("*.pdf", gitignore)
        self.assertIn("companies.csv", gitignore)
        self.assertIn(".venv/", gitignore)
        self.assertIn("venv/", gitignore)
        self.assertIn("data/", gitignore)
        self.assertIn("site/", gitignore)


if __name__ == "__main__":
    unittest.main()
