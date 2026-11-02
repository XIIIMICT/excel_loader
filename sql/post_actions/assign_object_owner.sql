DO $assign_owner$
DECLARE
    object_record RECORD;
BEGIN
    -- Таблицы, обычные и материализованные представления в рабочих схемах.
    -- __SOURCE_OWNER__ подставляется загрузчиком ролью подключения
    -- (common.postgres.user / POSTGRES_USER): только объекты, созданные этой
    -- ролью, переписываются на object_owner. Баунд-параметры не работают
    -- внутри тела DO $$ ... $$, поэтому значение экранируется в main.py.
    FOR object_record IN
        SELECT namespace.nspname AS schema_name, relation.relname AS object_name
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON relation.relnamespace = namespace.oid
        JOIN pg_roles AS owner_role ON relation.relowner = owner_role.oid
        WHERE namespace.nspname IN ('raw', 'dict', 'el')
          AND relation.relkind IN ('r', 'v', 'm')
          AND owner_role.rolname = __SOURCE_OWNER__
    LOOP
        -- В PostgreSQL ALTER TABLE применим также к VIEW и MATERIALIZED VIEW.
        EXECUTE format(
            'ALTER TABLE %I.%I OWNER TO object_owner;',
            object_record.schema_name,
            object_record.object_name
        );
    END LOOP;
END $assign_owner$;
