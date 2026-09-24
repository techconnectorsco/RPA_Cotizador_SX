-- =====================================================================
-- Migración 005: Soporte para múltiples intentos de búsqueda por item
--
-- Cada vez que el vendedor reintenta la búsqueda de un item, se crea
-- una nueva "ejecución" con intento_numero incremental. Los productos
-- encontrados quedan asociados a ese intento.
--
-- Por default el UI muestra solo el último intento (MAX(intento_numero)).
-- =====================================================================

BEGIN;

-- 1. Campo en scraper_ejecuciones
ALTER TABLE cotizador.scraper_ejecuciones
    ADD COLUMN IF NOT EXISTS intento_numero integer NOT NULL DEFAULT 1;

-- 2. Campo en productos_encontrados
ALTER TABLE cotizador.productos_encontrados
    ADD COLUMN IF NOT EXISTS intento_numero integer NOT NULL DEFAULT 1;

-- 3. Índices para consultas rápidas del último intento
CREATE INDEX IF NOT EXISTS scraper_ejec_intento_idx
    ON cotizador.scraper_ejecuciones(solicitud_item_id, intento_numero DESC);

CREATE INDEX IF NOT EXISTS productos_intento_idx
    ON cotizador.productos_encontrados(solicitud_item_id, intento_numero DESC);

-- 4. Campo en solicitud_items para saber el intento actual
ALTER TABLE cotizador.solicitud_items
    ADD COLUMN IF NOT EXISTS intento_actual integer NOT NULL DEFAULT 1;

-- 5. Campo para guardar el texto que originó cada intento (si cambió)
ALTER TABLE cotizador.solicitud_items
    ADD COLUMN IF NOT EXISTS texto_reinterpretacion text;

COMMIT;