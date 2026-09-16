"""Pure language selection checks; no accounts, files or remote requests."""
import unittest

from cli.qoj_cli import CLIError, cpp_standard, select_language


def option(label, value=None):
    return {"label": label, "value": label if value is None else value}


class CLILanguageTests(unittest.TestCase):
    def test_standard_years_are_not_plain_numeric_suffixes(self):
        names = ["C++98", "C++03", "C++11", "C++14", "C++17", "C++20", "C++23", "C++26"]
        self.assertEqual([cpp_standard(option(name)) for name in names],
                         [1998, 2003, 2011, 2014, 2017, 2020, 2023, 2026])
        self.assertEqual(cpp_standard(option("C++ (GCC 26)")), -1)

    def test_draft_aliases_and_upstream_numeric_ids(self):
        names = ["GNU++0x", "GNU++1y", "GNU++1z", "GNU++2a", "GNU++2b", "GNU++2c"]
        self.assertEqual([cpp_standard(option(name)) for name in names],
                         [2011, 2014, 2017, 2020, 2023, 2026])
        detail = {"languages": [option("GNU G++98", "1"), option("GNU G++2b", "2"), option("GNU++2c", "3")]}
        for extension in ("cpp", "cc", "cxx"):
            with self.subTest(extension=extension):
                self.assertEqual(select_language(detail, None, extension)["value"], "3")

    def test_default_26_and_fallback_use_only_advertised_values(self):
        detail = {"languages": [option("C++98 (GCC 15)", "legacy"), option("C++23 (GCC 14)", "23"),
                                option("C++26 (GCC 14)", "26")]}
        self.assertEqual(select_language(detail, None, "cpp")["value"], "26")
        detail["languages"].pop()
        self.assertEqual(select_language(detail, None, "cpp")["value"], "23")

    def test_explicit_choices_override_automatic_preference(self):
        detail = {"languages": [option("C++98", "old"), option("C++26", "new")]}
        self.assertEqual(select_language(detail, "old", "cpp")["value"], "old")
        self.assertEqual(select_language(detail, "C++98", "cpp")["value"], "old")
        self.assertEqual(select_language(detail, "cpp", "cpp")["value"], "new")

    def test_ambiguous_compiler_or_missing_cpp_still_requires_explicit_choice(self):
        for options in ([option("GNU G++26", "gcc"), option("Clang C++26", "clang")], [option("Python3")]):
            with self.subTest(options=options):
                with self.assertRaises(CLIError):
                    select_language({"languages": options}, None, "cpp")


if __name__ == "__main__":
    unittest.main()
