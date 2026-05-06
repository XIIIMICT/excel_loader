from nsi_loaders.base import DictionaryLoader
from nsi_loaders.nsi_quotes.core_products import build_payload as build_core_products_payload
from nsi_loaders.nsi_quotes.frequencies import build_payload as build_frequencies_payload
from nsi_loaders.nsi_quotes.frequencies_rank import build_payload as build_frequencies_rank_payload
from nsi_loaders.nsi_quotes.locations import build_payload as build_locations_payload
from nsi_loaders.nsi_quotes.nomenclative_groups import build_payload as build_nomenclative_groups_payload
from nsi_loaders.nsi_quotes.nomenclature import build_payload as build_nomenclature_payload
from nsi_loaders.nsi_quotes.prod_versions import build_payload as build_prod_versions_payload
from nsi_loaders.nsi_quotes.units import build_payload as build_quotes_units_payload
from nsi_loaders.quotes.cheminsight_p import build_payload as build_quotes_cheminsight_p_payload

# source_path считается относительно nsi.sources.data_dir.
# Имя папки оставляем частью пути, потому что внутри data_dir есть разные семейства файлов.
DICTIONARY_LOADERS = (
    DictionaryLoader(
        table_name="nsi_quotes_prod_versions",
        source_path="nsi_quotes/nsi_quotes_prod_versions.xlsb",
        build_payload=build_prod_versions_payload,
        columns_to_normalize=(
            "nomenclature_en",
            "basis",
            "departure_location_name_en",
            "location_name_en",
            "spot_contract_flag",
        ),
        normalization_styles={"basis": "uppercase"},
    ),
    DictionaryLoader(
        table_name="nsi_quotes_nomenclature",
        source_path="nsi_quotes/nsi_quotes_nomenclature.xlsb",
        build_payload=build_nomenclature_payload,
        columns_to_normalize=("nomenclature_en", "nomenclative_group_en"),
    ),
    DictionaryLoader(
        table_name="nsi_quotes_nomenclative_groups",
        source_path="nsi_quotes/nsi_quotes_nomenclative_groups.xlsb",
        build_payload=build_nomenclative_groups_payload,
        columns_to_normalize=("nomenclative_group_en",),
    ),
    DictionaryLoader(
        table_name="nsi_quotes_units",
        source_path="nsi_quotes/nsi_quotes_units.xlsx",
        build_payload=build_quotes_units_payload,
    ),
    DictionaryLoader(
        table_name="nsi_quotes_locations",
        source_path="nsi_quotes/nsi_quotes_locations.xlsb",
        build_payload=build_locations_payload,
        columns_to_normalize=("location_name_en",),
    ),
    DictionaryLoader(
        table_name="nsi_quotes_frequencies",
        source_path="nsi_quotes/nsi_quotes_frequencies.xlsx",
        build_payload=build_frequencies_payload,
    ),
    DictionaryLoader(
        table_name="nsi_quotes_frequencies_rank",
        source_path="nsi_quotes/nsi_quotes_frequencies_rank.xlsx",
        build_payload=build_frequencies_rank_payload,
    ),
    DictionaryLoader(
        table_name="nsi_quotes_core_products",
        source_path="nsi_quotes/nsi_quotes_core_products.xlsb",
        build_payload=build_core_products_payload,
    ),
    DictionaryLoader(
        table_name="quotes_cheminsight_p",
        source_path="quotes/ChemInsight/quotes_cheminsight_p.xlsb",
        build_payload=build_quotes_cheminsight_p_payload,
    ),
)
