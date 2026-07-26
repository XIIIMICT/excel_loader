from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Date,
    Float,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.schema import Column

if TYPE_CHECKING:
    from config import Settings


@dataclass(frozen=True, slots=True)
class PdTables:
    metadata: MetaData
    assets: Table
    materials: Table
    indicators: Table
    data: Table
    change_log: Table


def build_pd_tables(settings: Settings) -> PdTables:
    metadata = MetaData(schema=settings.pd_schema)
    assets_table_name = settings.pd_table_name("ref_assets")
    materials_table_name = settings.pd_table_name("ref_materials")
    indicators_table_name = settings.pd_table_name("ref_indicators")
    data_table_name = settings.pd_table_name("pd_data")
    change_log_table_name = settings.pd_table_name("pd_reference_change_log")

    assets = Table(
        assets_table_name,
        metadata,
        Column("id", Uuid, nullable=False, comment="UUID актива."),
        Column("name", Text, nullable=False, comment="Имя актива из производственной сводки."),
        Column("created_at", TIMESTAMP(timezone=True), nullable=False, comment="Дата первого обнаружения актива."),
        Column("updated_at", TIMESTAMP(timezone=True), nullable=False, comment="Дата последнего обновления актива."),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("name"),
        comment="Глобальный справочник активов, распознанных в производственной сводке.",
    )

    materials = Table(
        materials_table_name,
        metadata,
        Column("id", Uuid, nullable=False, comment="UUID сырьевой позиции или группы."),
        Column("name", Text, nullable=False, comment="Имя сырьевой позиции или группы сырья."),
        Column("parent_id", Uuid, nullable=True, comment="UUID родительской сырьевой группы."),
        Column("created_at", TIMESTAMP(timezone=True), nullable=False, comment="Дата первого обнаружения позиции."),
        Column("updated_at", TIMESTAMP(timezone=True), nullable=False, comment="Дата последнего обновления позиции."),
        Column(
            "is_gross",
            Boolean,
            nullable=False,
            comment="Валовый признак. По умолчанию включается, если название содержит 'вал'.",
        ),
        Column(
            "is_commercial",
            Boolean,
            nullable=False,
            comment="Товарный признак. По умолчанию включается, если название содержит 'товар' или 'тов.'.",
        ),
        Column("product_grade", Text, nullable=True, comment="Марка товара. По умолчанию NULL, заполняется вручную."),
        PrimaryKeyConstraint("id"),
        ForeignKeyConstraint(("parent_id",), (f"{settings.pd_schema}.{materials_table_name}.id",)),
        UniqueConstraint("name", "parent_id"),
        comment=(
            "Глобальный справочник сырьевых позиций и групп. Иерархия хранится здесь; "
            "одинаковое имя под другим parent_id считается новой позицией."
        ),
    )

    indicators = Table(
        indicators_table_name,
        metadata,
        Column("id", Uuid, nullable=False, comment="UUID показателя."),
        Column("category_1", Text, nullable=True, comment="Первый уровень группировки показателя."),
        Column("category_2", Text, nullable=True, comment="Второй уровень группировки показателя."),
        Column("category_3", Text, nullable=True, comment="Третий уровень группировки показателя."),
        Column("name", Text, nullable=False, comment="Листовое имя показателя."),
        Column("db_name", Text, nullable=False, comment="Имя колонки показателя в аналитической view."),
        Column("ordinal_number", Integer, nullable=False, comment="Порядок показателя в исходной сводке."),
        Column("created_at", TIMESTAMP(timezone=True), nullable=False, comment="Дата первого обнаружения показателя."),
        Column(
            "updated_at", TIMESTAMP(timezone=True), nullable=False, comment="Дата последнего обновления показателя."
        ),
        PrimaryKeyConstraint("id"),
        UniqueConstraint(
            "category_1",
            "category_2",
            "category_3",
            "name",
        ),
        UniqueConstraint("db_name"),
        comment=(
            "Глобальный справочник показателей PD. Только здесь хранится db_name для формирования pd_view. "
            "Имя показателя хранится как categories + leaf name, по аналогии с формами ReportCollector."
        ),
    )

    data = Table(
        data_table_name,
        metadata,
        Column("loaded_for", Date, nullable=False, comment="Отчетная дата производственной сводки."),
        Column("ordinal_number", Integer, nullable=False, comment="Порядок строки внутри конкретной выгрузки."),
        Column("asset_id", Uuid, nullable=False, comment="Актив строки производственной сводки."),
        Column("material_id", Uuid, nullable=False, comment="Сырьевая позиция строки производственной сводки."),
        Column("indicator_id", Uuid, nullable=False, comment="Показатель, которому относится значение."),
        Column("value", Text, nullable=True, comment="Сырое значение из Excel, сохраненное как текст."),
        Column("value_double", Float, nullable=True, comment="Числовая интерпретация value для аналитических view."),
        Column("loaded_at", TIMESTAMP(timezone=True), nullable=False, comment="Дата и время загрузки файла."),
        PrimaryKeyConstraint("loaded_for", "ordinal_number", "indicator_id"),
        ForeignKeyConstraint(("asset_id",), (f"{settings.pd_schema}.{assets_table_name}.id",)),
        ForeignKeyConstraint(("material_id",), (f"{settings.pd_schema}.{materials_table_name}.id",)),
        ForeignKeyConstraint(("indicator_id",), (f"{settings.pd_schema}.{indicators_table_name}.id",)),
        comment="Факты производственной сводки в длинной форме: дата, строка, показатель, значение.",
    )

    change_log = Table(
        change_log_table_name,
        metadata,
        Column("id", Uuid, nullable=False, comment="UUID события."),
        Column("action", Text, nullable=False, comment="Тип изменения: created или view_fallback."),
        Column("entity_type", Text, nullable=False, comment="Тип сущности: asset, material, indicator или view."),
        Column("entity_name", Text, nullable=False, comment="Имя измененной сущности."),
        Column("details", Text, nullable=True, comment="Дополнительные сведения об изменении."),
        Column("loaded_for", Date, nullable=True, comment="Отчетная дата файла, если событие связано с загрузкой."),
        Column("source_name", Text, nullable=True, comment="Имя файла или источника, вызвавшего изменение."),
        Column("created_at", TIMESTAMP(timezone=True), nullable=False, comment="Дата и время фиксации события."),
        PrimaryKeyConstraint("id"),
        comment="Аудит автоматических изменений справочников и fallback-создания аналитических view.",
    )

    return PdTables(
        metadata=metadata,
        assets=assets,
        materials=materials,
        indicators=indicators,
        data=data,
        change_log=change_log,
    )


def pd_view_reference(settings: Settings) -> str:
    return f"{settings.pd_schema}.{settings.pd_table_name('pd_view')}"
