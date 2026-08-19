import tempfile
import unittest
from pathlib import Path

from breg_watch.brreg import RegisteredEntity
from breg_watch.companies import (
    Company,
    CompanyListError,
    reconcile_company,
    load_companies,
    valid_orgnr,
    write_companies,
)


class CompanyListTests(unittest.TestCase):
    def test_loads_valid_companies_and_boolean_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companies.csv"
            path.write_text(
                "orgnr,name,active\n000000019,Eksempel ASA,true\n",
                encoding="utf-8",
            )

            companies = load_companies(path)

        self.assertEqual(companies[0].orgnr, "000000019")
        self.assertEqual(companies[0].name, "Eksempel ASA")
        self.assertTrue(companies[0].active)

    def test_rejects_invalid_orgnr_duplicate_and_active_value(self):
        cases = [
            "orgnr,name,active\n123456789,Ugyldig AS,true\n",
            "orgnr,name,active\n000000019,A AS,true\n000000019,B AS,false\n",
            "orgnr,name,active\n000000019,A AS,yes\n",
        ]
        for content in cases:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "companies.csv"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(CompanyListError):
                    load_companies(path)

    def test_validates_norwegian_mod11_check_digit(self):
        self.assertTrue(valid_orgnr("000000019"))
        self.assertFalse(valid_orgnr("000000020"))
        self.assertFalse(valid_orgnr("123"))

    def test_reconciles_name_without_changing_organisation_number(self):
        company = Company("000000019", "FIKTIVT GAMMELT NAVN AS", True)

        reconciled = reconcile_company(
            company, RegisteredEntity("000000019", "FIKTIVT SELSKAP AS", "active")
        )

        self.assertEqual(reconciled, Company("000000019", "FIKTIVT SELSKAP AS", True))

    def test_deactivates_deleted_or_unknown_company_without_changing_identity(self):
        company = Company("000000019", "FIKTIVT GAMMELT NAVN AS", True)
        cases = [
            ("deleted", "FIKTIVT SELSKAP AS", "FIKTIVT SELSKAP AS"),
            ("unknown", None, "FIKTIVT GAMMELT NAVN AS"),
            ("removed", None, "FIKTIVT GAMMELT NAVN AS"),
        ]
        for status, official_name, expected_name in cases:
            with self.subTest(status=status):
                reconciled = reconcile_company(
                    company, RegisteredEntity("000000019", official_name, status)
                )
                self.assertEqual(
                    reconciled, Company("000000019", expected_name, False)
                )

    def test_writes_reconciled_companies_with_norwegian_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companies.csv"
            write_companies(path, [Company("000000019", "SØR-NORGE ÅS", True)])

            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "orgnr,name,active\n000000019,SØR-NORGE ÅS,true\n",
            )


if __name__ == "__main__":
    unittest.main()
