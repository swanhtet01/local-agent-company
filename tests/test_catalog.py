import io
import json
import unittest
from unittest.mock import patch

from local_company.spreadsheet import check_catalog_json
from local_company.cli import main


class CatalogTests(unittest.TestCase):
    def rows(self):
        return [dict(name=n, price=p, currency="MMK", category=c) for n, p, c in
                [("Latte", "4500", "Drinks"), ("Croissant", "3500", "Bakery"),
                 ("Green tea", None, "Drinks")]]

    def test_preserves_known_values_and_blocks_missing_price(self):
        source = self.rows()
        result = check_catalog_json(json.dumps(source))
        self.assertFalse(result["valid"])
        self.assertEqual([{k: v for k, v in r.items() if k != "valid"} for r in result["rows"]], source)
        self.assertEqual(result["errors"], [{"row": 3, "field": "price", "code": "owner_input_required"}])
        self.assertFalse(result["import_performed"])

    def test_invalid_money_is_never_coerced(self):
        for value in [True, 4500, -1, "NaN", "Infinity", "0", "-3", "1e3", "4,500", "2.01"]:
            with self.subTest(value=value):
                row = self.rows()[0]
                row["price"] = value
                self.assertFalse(check_catalog_json(json.dumps([row]))["valid"])

    def test_valid_currency_precision(self):
        row = self.rows()[0]
        row.update(currency="THB", price="12.50")
        self.assertTrue(check_catalog_json(json.dumps([row]))["valid"])

    def test_duplicates_unknown_fields_and_malformed_input(self):
        row = self.rows()[0]
        for source in ["[]", "{}", '{"a":1,"a":2}', "[NaN]", "x" * 1_000_001,
                       json.dumps([row, row]), json.dumps([{**row, "stock": 3}]),
                       json.dumps([{**row, "currency": []}])]:
            self.assertFalse(check_catalog_json(source)["valid"])

    def test_cli_does_not_initialize_company_or_model(self):
        for price, status in [("4500", 0), (None, 1)]:
            row = self.rows()[0]
            row["price"] = price
            with patch("sys.argv", ["local-company", "catalog-check"]), patch("sys.stdin", io.StringIO(json.dumps([row]))), patch("sys.stdout", new_callable=io.StringIO) as output, patch("local_company.cli.Company") as company:
                self.assertEqual(main(), status)
                company.assert_not_called()
                self.assertFalse(json.loads(output.getvalue())["model_called"])
