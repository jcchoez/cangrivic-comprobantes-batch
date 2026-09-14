#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, json, logging
from comun_compras import db_conn, autorizar_comprobante_sri

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("autorizacion_retenciones")

LIMITE = int(os.getenv("LIMITE_AUTORIZACION", "1000"))


def fetch_pendientes(limit):
    cn = db_conn()
    try:
        cur = cn.cursor(dictionary=True)
        cur.execute("""
            SELECT compra_id, clave_acceso_retencion
            FROM compras
            WHERE estado_sri_retencion IN ('RECIBIDA','EN PROCESO','PROCESANDO')
              AND (numero_autorizacion_retencion IS NULL
                   OR numero_autorizacion_retencion='')
            ORDER BY ultimo_intento_envio_retencion ASC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()
    finally:
        cn.close()


def main():
    logger.info("Iniciando autorización de comprobantes de retención...")
    pend = fetch_pendientes(LIMITE)
    if not pend:
        logger.info("Sin retenciones pendientes")
        return
    cn = db_conn()
    try:
        cur = cn.cursor()
        for row in pend:
            cid = row["compra_id"]
            clave = row["clave_acceso_retencion"]
            try:
                estado, numero, mensaje, rj = autorizar_comprobante_sri(clave)
                jstr = json.dumps(rj, indent=2, ensure_ascii=False)
                logger.info(f"Retención compra {cid}: {estado} - {numero}")
                cur.execute("""
                    UPDATE compras SET estado_sri_retencion=%s,
                        mensaje_sri_retencion=CONCAT(COALESCE(mensaje_sri_retencion,''), ' | Aut: ', %s),
                        numero_autorizacion_retencion=%s,
                        fecha_autorizacion_retencion=NOW(),
                        respuesta_json_retencion=%s
                    WHERE compra_id=%s
                """, (estado, mensaje, numero, jstr, cid))
                cn.commit()
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"Error autorizando retención {cid}: {e}")
    finally:
        cn.close()
    logger.info("Autorización de retenciones completada")


if __name__ == "__main__":
    main()