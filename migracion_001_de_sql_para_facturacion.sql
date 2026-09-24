-- =====================================================================
-- Migración 001: Schema Cotizador (portable)
--
-- Este script crea el schema `cotizador` con todas sus tablas SIN
-- referencias a otras tablas de la base. Es portable a cualquier base
-- de datos PostgreSQL.
--
-- Para SX (facturacion_db): correr también 002_optional_fks_facturacion.sql
-- Para otros clientes: solo este basta.
--
-- Ejecutar:
--   psql -h localhost -U tu_usuario -d facturacion_db -f 001_init_cotizador.sql
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 0. Schema y extensiones
-- ---------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS cotizador;
COMMENT ON SCHEMA cotizador IS 'Módulo RPA Cotizador - búsqueda multi-proveedor';

-- pg_trgm sirve para búsquedas fuzzy en el histórico (útil después)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

SET search_path TO cotizador, public;

-- ---------------------------------------------------------------------
-- 1. Tipos ENUM del cotizador
-- ---------------------------------------------------------------------
CREATE TYPE cotizador.canal_origen AS ENUM (
    'formulario',       -- vendedor lo llenó en el UI
    'busqueda_rapida',  -- vendedor escribió texto libre en un buscador
    'correo',           -- llegó por correo automatizado
    'whatsapp_manual',  -- vendedor pegó un WhatsApp
    'cron'              -- disparo programado
);

CREATE TYPE cotizador.estado_solicitud AS ENUM (
    'pendiente',        -- recién creada, aún no procesada por IA
    'normalizando',     -- IA está extrayendo productos
    'scrapeando',       -- workers corriendo en proveedores
    'lista',            -- resultados listos para vendedor
    'error',            -- falló algo en el pipeline
    'cerrada'           -- vendedor terminó (con o sin cotización)
);

CREATE TYPE cotizador.tier_match AS ENUM (
    'exacto',           -- score >= 85
    'bueno',            -- 70-84
    'similar',          -- 55-69
    'dudoso'            -- < 55 (no se guarda normalmente)
);

CREATE TYPE cotizador.urgencia AS ENUM (
    'baja',
    'normal',
    'alta'
);

-- ---------------------------------------------------------------------
-- 2. Proveedores (catálogo maestro)
-- ---------------------------------------------------------------------
CREATE TABLE cotizador.proveedores (
    id              uuid DEFAULT gen_random_uuid() NOT NULL,
    codigo          text NOT NULL,      -- 'intelec', 'amazon', 'eurocomp'
    nombre          text NOT NULL,
    url_base        text NOT NULL,
    logo_url        text,
    requiere_login  boolean DEFAULT false NOT NULL,
    activo          boolean DEFAULT true NOT NULL,
    notas           text,
    creado_en       timestamp with time zone DEFAULT now() NOT NULL,
    actualizado_en  timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE cotizador.proveedores OWNER TO postgres;

ALTER TABLE ONLY cotizador.proveedores
    ADD CONSTRAINT proveedores_pkey PRIMARY KEY (id);

ALTER TABLE ONLY cotizador.proveedores
    ADD CONSTRAINT proveedores_codigo_unique UNIQUE (codigo);

-- Seed inicial
INSERT INTO cotizador.proveedores (codigo, nombre, url_base, requiere_login, activo) VALUES
    ('intelec',  'Intelec',              'https://www.intelec.co.cr',   false, true),
    ('amazon',   'Amazon',               'https://www.amazon.com',      false, true),
    ('eurocomp', 'Eurocomp Costa Rica',  'https://eurocompcr.com',      true,  false),
    ('intcomex', 'Intcomex Costa Rica',  'https://store.intcomex.com',  true,  false),
    ('cdc',      'CDC Internacional',    'https://cdcinternacional.com', true, false);

-- ---------------------------------------------------------------------
-- 3. Solicitudes (una petición de cotización)
-- ---------------------------------------------------------------------
CREATE TABLE cotizador.solicitudes (
    id                      uuid DEFAULT gen_random_uuid() NOT NULL,

    -- Origen de la solicitud
    canal_origen            cotizador.canal_origen NOT NULL,

    -- Referencias externas (nullable, se enlazan si existen en public.*)
    cliente_id              uuid,       -- FK opcional a public.clientes
    usuario_creador_id      uuid,       -- FK opcional a public.usuarios (vendedor)
    empresa_id              uuid,       -- FK opcional a public.empresas

    -- Datos del correo (si canal_origen = 'correo')
    correo_remitente        text,
    asunto_correo           text,
    message_id_correo       text,       -- para idempotencia

    -- Contenido original (lo que escribió el vendedor/cliente)
    texto_original          text NOT NULL,

    -- Interpretación de la IA
    ia_es_cotizacion        boolean,
    ia_interpretacion       jsonb,      -- JSON completo devuelto por la IA
    ia_modelo_usado         text,       -- ej: 'gemini-3.6-flash'

    -- Estado
    estado                  cotizador.estado_solicitud DEFAULT 'pendiente' NOT NULL,
    urgencia                cotizador.urgencia DEFAULT 'normal' NOT NULL,
    error_mensaje           text,

    -- Timestamps
    creado_en               timestamp with time zone DEFAULT now() NOT NULL,
    actualizado_en          timestamp with time zone DEFAULT now() NOT NULL,
    normalizado_en          timestamp with time zone,
    scrapeado_en            timestamp with time zone,
    cerrado_en              timestamp with time zone
);

ALTER TABLE cotizador.solicitudes OWNER TO postgres;

ALTER TABLE ONLY cotizador.solicitudes
    ADD CONSTRAINT solicitudes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY cotizador.solicitudes
    ADD CONSTRAINT solicitudes_message_id_unique UNIQUE (message_id_correo);

CREATE INDEX solicitudes_estado_idx         ON cotizador.solicitudes(estado);
CREATE INDEX solicitudes_cliente_id_idx     ON cotizador.solicitudes(cliente_id);
CREATE INDEX solicitudes_usuario_id_idx     ON cotizador.solicitudes(usuario_creador_id);
CREATE INDEX solicitudes_empresa_id_idx     ON cotizador.solicitudes(empresa_id);
CREATE INDEX solicitudes_canal_idx          ON cotizador.solicitudes(canal_origen);
CREATE INDEX solicitudes_creado_en_idx      ON cotizador.solicitudes(creado_en DESC);

-- ---------------------------------------------------------------------
-- 4. Items de la solicitud (una solicitud puede tener N productos)
-- ---------------------------------------------------------------------
CREATE TABLE cotizador.solicitud_items (
    id                      uuid DEFAULT gen_random_uuid() NOT NULL,
    solicitud_id            uuid NOT NULL,

    -- Producto solicitado (extraído por IA)
    categoria               text,
    marca                   text,
    linea                   text,
    modelo                  text,

    -- Specs estructuradas
    specs                   jsonb DEFAULT '{}'::jsonb NOT NULL,

    cantidad                integer DEFAULT 1 NOT NULL,

    -- Query que se envió a los scrapers
    query_scraper           text NOT NULL,

    notas                   text,
    orden                   integer DEFAULT 0 NOT NULL,

    creado_en               timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE cotizador.solicitud_items OWNER TO postgres;

ALTER TABLE ONLY cotizador.solicitud_items
    ADD CONSTRAINT solicitud_items_pkey PRIMARY KEY (id);

ALTER TABLE ONLY cotizador.solicitud_items
    ADD CONSTRAINT solicitud_items_cantidad_check CHECK (cantidad > 0);

ALTER TABLE ONLY cotizador.solicitud_items
    ADD CONSTRAINT solicitud_items_solicitud_fk
    FOREIGN KEY (solicitud_id) REFERENCES cotizador.solicitudes(id) ON DELETE CASCADE;

CREATE INDEX solicitud_items_solicitud_idx ON cotizador.solicitud_items(solicitud_id);

-- ---------------------------------------------------------------------
-- 5. Ejecuciones de scraper (una por proveedor por item)
--    Guarda métricas de la corrida: cuántos crudos, cuántos pasaron.
-- ---------------------------------------------------------------------
CREATE TABLE cotizador.scraper_ejecuciones (
    id                      uuid DEFAULT gen_random_uuid() NOT NULL,
    solicitud_item_id       uuid NOT NULL,
    proveedor_codigo        text NOT NULL,  -- 'intelec', 'amazon', etc.

    -- Query real enviada al scraper
    query_usada             text NOT NULL,

    -- Resultado
    exitoso                 boolean NOT NULL,
    duracion_ms             integer,
    error_mensaje           text,

    -- Métricas
    total_crudos            integer DEFAULT 0 NOT NULL,   -- lo que trajo el scraper
    total_guardados         integer DEFAULT 0 NOT NULL,   -- lo que persistimos

    ejecutado_en            timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE cotizador.scraper_ejecuciones OWNER TO postgres;

ALTER TABLE ONLY cotizador.scraper_ejecuciones
    ADD CONSTRAINT scraper_ejecuciones_pkey PRIMARY KEY (id);

ALTER TABLE ONLY cotizador.scraper_ejecuciones
    ADD CONSTRAINT scraper_ejecuciones_item_fk
    FOREIGN KEY (solicitud_item_id) REFERENCES cotizador.solicitud_items(id) ON DELETE CASCADE;

CREATE INDEX scraper_ejec_item_idx      ON cotizador.scraper_ejecuciones(solicitud_item_id);
CREATE INDEX scraper_ejec_proveedor_idx ON cotizador.scraper_ejecuciones(proveedor_codigo);

-- ---------------------------------------------------------------------
-- 6. Productos encontrados (SNAPSHOT congelado en el momento del scraping)
--    Esta es la tabla clave que consume el frontend.
-- ---------------------------------------------------------------------
CREATE TABLE cotizador.productos_encontrados (
    id                      uuid DEFAULT gen_random_uuid() NOT NULL,
    solicitud_item_id       uuid NOT NULL,
    scraper_ejecucion_id    uuid NOT NULL,
    proveedor_codigo        text NOT NULL,

    -- ===== SNAPSHOT del producto (todo lo del ProductoEncontrado) =====

    -- Identificación
    nombre                  text NOT NULL,
    sku                     text,           -- SKU o ASIN
    marca                   text,
    categoria               text,

    -- Links
    url_detalle             text NOT NULL,  -- para ir al producto en el proveedor
    url_imagen              text,           -- para mostrar en el UI (sin descargar)

    -- Precio (snapshot)
    precio                  numeric(12, 2),
    precio_regular          numeric(12, 2), -- si hubo descuento, este era el original
    tiene_descuento         boolean DEFAULT false NOT NULL,
    descuento_porcentaje    numeric(5, 2),
    moneda                  text,           -- 'CRC', 'USD', 'COP'
    precio_texto_original   text,           -- '₡59,900 IVAI' tal cual

    -- Stock
    stock_texto             text,           -- 'En stock', 'Agotado'
    disponible              boolean,

    -- Info adicional
    descripcion_corta       text,
    specs                   jsonb DEFAULT '{}'::jsonb NOT NULL,

    -- Campos específicos del proveedor (rating, prime, badges, etc.)
    extra                   jsonb DEFAULT '{}'::jsonb NOT NULL,

    -- ===== MATCHING calculado =====
    match_score             numeric(5, 2) NOT NULL,
    match_score_texto       numeric(5, 2),
    match_score_marca       numeric(5, 2),
    match_score_specs       numeric(5, 2),
    match_ajuste_variantes  numeric(5, 2) DEFAULT 0 NOT NULL,
    match_bonus_identidad   numeric(5, 2) DEFAULT 0 NOT NULL,
    match_bonus_contenida   numeric(5, 2) DEFAULT 0 NOT NULL,
    match_tier              cotizador.tier_match NOT NULL,
    match_etiquetas         text[] DEFAULT ARRAY[]::text[] NOT NULL,  -- ['COMBO'], ['USADO']

    -- Timestamps
    scrapeado_en            timestamp with time zone NOT NULL,
    creado_en               timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE cotizador.productos_encontrados OWNER TO postgres;

ALTER TABLE ONLY cotizador.productos_encontrados
    ADD CONSTRAINT productos_encontrados_pkey PRIMARY KEY (id);

ALTER TABLE ONLY cotizador.productos_encontrados
    ADD CONSTRAINT productos_score_check CHECK (match_score >= 0 AND match_score <= 100);

ALTER TABLE ONLY cotizador.productos_encontrados
    ADD CONSTRAINT productos_item_fk
    FOREIGN KEY (solicitud_item_id) REFERENCES cotizador.solicitud_items(id) ON DELETE CASCADE;

ALTER TABLE ONLY cotizador.productos_encontrados
    ADD CONSTRAINT productos_ejecucion_fk
    FOREIGN KEY (scraper_ejecucion_id) REFERENCES cotizador.scraper_ejecuciones(id) ON DELETE CASCADE;

CREATE INDEX productos_item_idx        ON cotizador.productos_encontrados(solicitud_item_id);
CREATE INDEX productos_proveedor_idx   ON cotizador.productos_encontrados(proveedor_codigo);
CREATE INDEX productos_tier_idx        ON cotizador.productos_encontrados(match_tier);
CREATE INDEX productos_score_idx       ON cotizador.productos_encontrados(match_score DESC);
CREATE INDEX productos_nombre_trgm_idx ON cotizador.productos_encontrados
    USING GIN (nombre gin_trgm_ops);

-- ---------------------------------------------------------------------
-- 7. Selecciones del vendedor (qué producto eligió para cotizar)
-- ---------------------------------------------------------------------
CREATE TABLE cotizador.selecciones (
    id                      uuid DEFAULT gen_random_uuid() NOT NULL,
    solicitud_id            uuid NOT NULL,
    solicitud_item_id       uuid NOT NULL,
    producto_encontrado_id  uuid NOT NULL,

    usuario_id              uuid,           -- FK opcional a public.usuarios

    cantidad                integer NOT NULL,
    precio_snapshot         numeric(12, 2), -- precio en el momento de seleccionar
    notas                   text,

    seleccionado_en         timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE cotizador.selecciones OWNER TO postgres;

ALTER TABLE ONLY cotizador.selecciones
    ADD CONSTRAINT selecciones_pkey PRIMARY KEY (id);

ALTER TABLE ONLY cotizador.selecciones
    ADD CONSTRAINT selecciones_cantidad_check CHECK (cantidad > 0);

-- Un item puede tener una sola selección activa por vendedor
ALTER TABLE ONLY cotizador.selecciones
    ADD CONSTRAINT selecciones_item_usuario_unique UNIQUE (solicitud_item_id, usuario_id);

ALTER TABLE ONLY cotizador.selecciones
    ADD CONSTRAINT selecciones_solicitud_fk
    FOREIGN KEY (solicitud_id) REFERENCES cotizador.solicitudes(id) ON DELETE CASCADE;

ALTER TABLE ONLY cotizador.selecciones
    ADD CONSTRAINT selecciones_item_fk
    FOREIGN KEY (solicitud_item_id) REFERENCES cotizador.solicitud_items(id) ON DELETE CASCADE;

ALTER TABLE ONLY cotizador.selecciones
    ADD CONSTRAINT selecciones_producto_fk
    FOREIGN KEY (producto_encontrado_id) REFERENCES cotizador.productos_encontrados(id) ON DELETE RESTRICT;

CREATE INDEX selecciones_solicitud_idx ON cotizador.selecciones(solicitud_id);
CREATE INDEX selecciones_usuario_idx   ON cotizador.selecciones(usuario_id);

-- ---------------------------------------------------------------------
-- 8. Trigger para actualizar `actualizado_en` automáticamente
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION cotizador.set_actualizado_en()
RETURNS trigger AS $$
BEGIN
    NEW.actualizado_en = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER solicitudes_set_actualizado_en
    BEFORE UPDATE ON cotizador.solicitudes
    FOR EACH ROW EXECUTE FUNCTION cotizador.set_actualizado_en();

CREATE TRIGGER proveedores_set_actualizado_en
    BEFORE UPDATE ON cotizador.proveedores
    FOR EACH ROW EXECUTE FUNCTION cotizador.set_actualizado_en();

-- ---------------------------------------------------------------------
-- 9. Vista útil: solicitudes con resumen (para listado en frontend)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW cotizador.v_solicitudes_resumen AS
SELECT
    s.id,
    s.canal_origen,
    s.estado,
    s.urgencia,
    s.texto_original,
    s.cliente_id,
    s.usuario_creador_id,
    s.empresa_id,
    s.creado_en,
    s.actualizado_en,
    s.scrapeado_en,
    COUNT(DISTINCT si.id) AS total_items,
    COUNT(DISTINCT pe.id) AS total_productos,
    COUNT(DISTINCT pe.id) FILTER (WHERE pe.match_tier = 'exacto') AS total_exactos,
    COUNT(DISTINCT pe.id) FILTER (WHERE pe.match_tier = 'bueno')  AS total_buenos,
    COUNT(DISTINCT sel.id) AS total_seleccionados
FROM cotizador.solicitudes s
LEFT JOIN cotizador.solicitud_items si       ON si.solicitud_id = s.id
LEFT JOIN cotizador.productos_encontrados pe ON pe.solicitud_item_id = si.id
LEFT JOIN cotizador.selecciones sel          ON sel.solicitud_id = s.id
GROUP BY s.id;

COMMIT;

-- =====================================================================
-- FIN migración 001_init_cotizador
--
-- Para revertir todo:
--   DROP SCHEMA cotizador CASCADE;
-- =====================================================================