from dictionaries.base import DictionaryLoader
from dictionaries.quotes.cheminsight_p import build_payload as build_cheminsight_payload
from dictionaries.quotes.daily_excel import build_payload as build_daily_excel_payload

DICTIONARIES = (
    DictionaryLoader("quotes_cheminsight_p", "quotes/ChemInsight/quotes_cheminsight_p.xlsb", build_cheminsight_payload),
    DictionaryLoader("quotes_excel_daily", "quotes/Daily Excel/Ежедневные котировки.xlsb", build_daily_excel_payload),
)
