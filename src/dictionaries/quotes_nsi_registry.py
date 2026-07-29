from dictionaries.base import DictionaryLoader
from dictionaries.quotes_nsi.core_products import build_payload as build_core_products_payload
from dictionaries.quotes_nsi.frequencies import build_payload as build_frequencies_payload
from dictionaries.quotes_nsi.frequencies_rank import build_payload as build_frequencies_rank_payload
from dictionaries.quotes_nsi.locations import build_payload as build_locations_payload
from dictionaries.quotes_nsi.nomenclative_groups import build_payload as build_nomenclative_groups_payload
from dictionaries.quotes_nsi.nomenclature import build_payload as build_nomenclature_payload
from dictionaries.quotes_nsi.prod_versions import build_payload as build_prod_versions_payload
from dictionaries.quotes_nsi.sources import build_payload as build_sources_payload
from dictionaries.quotes_nsi.units import build_payload as build_units_payload

DICTIONARIES = (
    DictionaryLoader("quotes_nsi_sources", "quotes_nsi/quotes_nsi_sources.xlsx", build_sources_payload),
    DictionaryLoader(
        "quotes_nsi_prod_versions",
        "quotes_nsi/quotes_nsi_prod_versions.xlsb",
        build_prod_versions_payload,
        ("nomenclature_en", "basis", "departure_location_name_en", "location_name_en", "spot_contract_flag"),
        {"basis": "uppercase"},
    ),
    DictionaryLoader(
        "quotes_nsi_nomenclature",
        "quotes_nsi/quotes_nsi_nomenclature.xlsb",
        build_nomenclature_payload,
        ("nomenclature_en", "nomenclative_group_en"),
    ),
    DictionaryLoader(
        "quotes_nsi_nomenclative_groups",
        "quotes_nsi/quotes_nsi_nomenclative_groups.xlsb",
        build_nomenclative_groups_payload,
        ("nomenclative_group_en",),
    ),
    DictionaryLoader("quotes_nsi_units", "quotes_nsi/quotes_nsi_units.xlsx", build_units_payload),
    DictionaryLoader(
        "quotes_nsi_locations", "quotes_nsi/quotes_nsi_locations.xlsb", build_locations_payload, ("location_name_en",)
    ),
    DictionaryLoader("quotes_nsi_frequencies", "quotes_nsi/quotes_nsi_frequencies.xlsx", build_frequencies_payload),
    DictionaryLoader(
        "quotes_nsi_frequencies_rank", "quotes_nsi/quotes_nsi_frequencies_rank.xlsx", build_frequencies_rank_payload
    ),
    DictionaryLoader(
        "quotes_nsi_core_products", "quotes_nsi/quotes_nsi_core_products.xlsb", build_core_products_payload
    ),
)
