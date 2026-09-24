-- =====================================================================
-- Migración 004: Tabla de proveedores sugeridos
-- =====================================================================

BEGIN;

CREATE TYPE cotizador.estado_sugerencia AS ENUM (
    'pendiente',      -- recién sugerido, esperando revisión
    'en_revision',    -- el equipo técnico lo está evaluando
    'aprobado',       -- se decidió integrarlo (pero aún no está en el código)
    'implementado',   -- ya existe como scraper activo
    'rechazado'       -- no se va a integrar
);

CREATE TABLE cotizador.proveedores_sugeridos (
    id                      uuid DEFAULT gen_random_uuid() NOT NULL,

    nombre_proveedor        text NOT NULL,
    url_sitio               text NOT NULL,

    tiene_convenio          boolean DEFAULT false NOT NULL,
    requiere_login          boolean DEFAULT false NOT NULL,
    notas                   text,
    razon_sugerencia        text,           -- por qué el vendedor lo sugiere

    estado                  cotizador.estado_sugerencia DEFAULT 'pendiente' NOT NULL,
    respuesta_admin         text,           -- notas del admin al revisar

    usuario_sugerente_id    uuid,           -- FK opcional a public.usuarios
    revisado_por_id         uuid,           -- FK opcional al admin que revisó

    creado_en               timestamp with time zone DEFAULT now() NOT NULL,
    actualizado_en          timestamp with time zone DEFAULT now() NOT NULL,
    revisado_en             timestamp with time zone
);

ALTER TABLE cotizador.proveedores_sugeridos OWNER TO postgres;

ALTER TABLE ONLY cotizador.proveedores_sugeridos
    ADD CONSTRAINT proveedores_sugeridos_pkey PRIMARY KEY (id);

CREATE INDEX proveedores_sugeridos_estado_idx
    ON cotizador.proveedores_sugeridos(estado);

CREATE INDEX proveedores_sugeridos_creado_idx
    ON cotizador.proveedores_sugeridos(creado_en DESC);

-- Trigger para actualizado_en
CREATE TRIGGER proveedores_sugeridos_set_actualizado_en
    BEFORE UPDATE ON cotizador.proveedores_sugeridos
    FOR EACH ROW EXECUTE FUNCTION cotizador.set_actualizado_en();

COMMIT;