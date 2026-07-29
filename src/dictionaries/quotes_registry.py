from dictionaries.base import CombinedDictionaryLoader, DictionaryLoader
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
        ("sci/SCI СКИ.XLSX", "sci/SCI СКС_new.XLSX"),
        build_sci_payload,
    ),
)
