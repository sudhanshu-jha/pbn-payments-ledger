from datetime import date

from django.test import SimpleTestCase

from mock_processor import validators


class LuhnTests(SimpleTestCase):
    def test_known_valid_numbers(self):
        for number in (
            "4242424242424242",
            "4000000000000002",
            "5555555555554444",
            "378282246310005",
        ):
            self.assertTrue(validators.luhn_valid(number), number)

    def test_invalid_numbers(self):
        for number in ("4242424242424241", "1234567890123456", "", "abcd", "4242 4242"):
            self.assertFalse(validators.luhn_valid(number), number)


class ExpiryTests(SimpleTestCase):
    def test_current_month_is_valid(self):
        self.assertTrue(validators.expiry_valid("06/26", today=date(2026, 6, 30)))

    def test_past_month_is_invalid(self):
        self.assertFalse(validators.expiry_valid("05/26", today=date(2026, 6, 1)))

    def test_four_digit_year(self):
        self.assertTrue(validators.expiry_valid("12/2030", today=date(2026, 6, 1)))

    def test_garbage(self):
        for value in ("", "13/30", "1/30", "12-30", "12/3"):
            self.assertFalse(validators.expiry_valid(value, today=date(2026, 1, 1)), value)


class BankTests(SimpleTestCase):
    def test_routing_number_must_be_nine_digits(self):
        self.assertTrue(validators.routing_number_valid("021000021"))
        self.assertFalse(validators.routing_number_valid("02100002"))
        self.assertFalse(validators.routing_number_valid("0210000211"))
        self.assertFalse(validators.routing_number_valid("02100002a"))

    def test_account_number_length(self):
        self.assertTrue(validators.account_number_valid("1234"))
        self.assertTrue(validators.account_number_valid("12345678901234567"))
        self.assertFalse(validators.account_number_valid("123"))
        self.assertFalse(validators.account_number_valid("123456789012345678"))
