from pathlib import Path

from csv_fixer.validators import contractgate_available, validate_csv_against_contract

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"


def test_contractgate_package_hooked():
    assert contractgate_available() is True


def test_clean_orders_pass():
    report = validate_csv_against_contract(
        FIX / "clean_orders.csv", FIX / "orders_contract.yaml"
    )
    assert report.passed is True
    assert report.rows_checked == 3
    assert report.backend == "contractgate"


def test_messy_orders_fail_before_repair():
    report = validate_csv_against_contract(
        FIX / "messy_orders.csv", FIX / "orders_contract.yaml"
    )
    assert report.passed is False
    assert len(report.violations) > 0
