-- =====================================================================
-- Migración 002 (OPCIONAL): FKs a public.* (solo si existen)
--
-- Este archivo agrega las foreign keys hacia las tablas de facturación
-- de SX. Solo correr si estás instalando en facturacion_db de SX.
--
-- Si estás instalando en otra base sin esas tablas, NO correr este.
--
-- Ejecutar:
--   psql -h localhost -U tu_usuario -d facturacion_db -f 002_optional_fks_facturacion.sql
-- =====================================================================

BEGIN;

-- Solicitudes → public.clientes, public.usuarios, public.empresas
ALTER TABLE cotizador.solicitudes
    ADD CONSTRAINT solicitudes_cliente_fk
    FOREIGN KEY (cliente_id) REFERENCES public.clientes(id) ON DELETE SET NULL;

ALTER TABLE cotizador.solicitudes
    ADD CONSTRAINT solicitudes_usuario_creador_fk
    FOREIGN KEY (usuario_creador_id) REFERENCES public.usuarios(id) ON DELETE SET NULL;

ALTER TABLE cotizador.solicitudes
    ADD CONSTRAINT solicitudes_empresa_fk
    FOREIGN KEY (empresa_id) REFERENCES public.empresas(id) ON DELETE SET NULL;

-- Selecciones → public.usuarios
ALTER TABLE cotizador.selecciones
    ADD CONSTRAINT selecciones_usuario_fk
    FOREIGN KEY (usuario_id) REFERENCES public.usuarios(id) ON DELETE SET NULL;

COMMIT;

-- =====================================================================
-- Para revertir:
--   ALTER TABLE cotizador.solicitudes DROP CONSTRAINT solicitudes_cliente_fk;
--   ALTER TABLE cotizador.solicitudes DROP CONSTRAINT solicitudes_usuario_creador_fk;
--   ALTER TABLE cotizador.solicitudes DROP CONSTRAINT solicitudes_empresa_fk;
--   ALTER TABLE cotizador.selecciones DROP CONSTRAINT selecciones_usuario_fk;
-- =====================================================================