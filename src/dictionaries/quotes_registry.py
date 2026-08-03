from dictionaries.base import CombinedDictionaryLoader, DictionaryLoader, PatternDictionaryLoader
from dictionaries.quotes.baiinfo import FILE_PATTERN as BAIINFO_FILE_PATTERN
from dictionaries.quotes.baiinfo import build_payload as build_baiinfo_payload
from dictionaries.quotes.cheminsight_p import build_payload as build_cheminsight_payload
from dictionaries.quotes.daily_excel import build_payload as build_daily_excel_payload
from dictionaries.quotes.sci import build_payload as build_sci_payload

DICTIONARIES = (
    DictionaryLoader("quotes_cheminsight_p", "quotes/ChemInsight/quotes_cheminsight_p.xlsb", build_cheminsight_payload),
    DictionaryLoader("quotes_excel_daily", "quotes/Daily Quotes/Ежедневные котировки.xlsb", build_daily_excel_payload),
)

COMBINED_DICTIONARIES = (
    CombinedDictionaryLoader(
        "quotes_excel_sci",
        ("quotes/SCI/SCI СКИ.XLSX", "quotes/SCI/SCI СКС_new.XLSX"),
        build_sci_payload,
    ),
)

PATTERN_DICTIONARIES = (
    PatternDictionaryLoader(
        "quotes_excel_baiinfo",
        "quotes/Baiinfo",
        BAIINFO_FILE_PATTERN,
        build_baiinfo_payload,
    ),
)
