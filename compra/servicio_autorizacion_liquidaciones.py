#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, json, logging
from comun_compras import db_conn, autorizar_comprobante_sri

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("autorizacion_liquidaciones")

LIMITE = int(os.getenv("LIMITE_AUTORIZACION", "1000"))


def fetch_pendientes(limit):
    cn = db_conn()
    try:
        cur = cn.cursor(dictionary=True)
        cur.execute("""
            SELECT compra_id, clave_acceso
            FROM compras
            WHERE estado_sri IN ('RECIBIDA','EN PROCESO','PROCESANDO')
              AND (numero_autorizacion IS NULL OR numero_autorizacion='')
            ORDER BY ultimo_intento_envio ASC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()
    finally:
        cn.close()


def main():
    logger.info("Iniciando autorización de liquidaciones...")
    pend = fetch_pendientes(LIMITE)
    if not pend:
        logger.info("Sin liquidaciones pendientes de autorizar")
        return
    cn = db_conn()
    try:
        cur = cn.cursor()
        for row in pend:
            cid, clave = row["compra_id"], row["clave_acceso"]
            try:
                estado, numero, mensaje, rj = autorizar_comprobante_sri(clave)
                jstr = json.dumps(rj, indent=2, ensure_ascii=False)
                logger.info(f"Compra {cid}: {estado} - {numero}")
                cur.execute("""
                    UPDATE compras SET estado_sri=%s,
                        mensaje_sri=CONCAT(COALESCE(mensaje_sri,''), ' | Aut: ', %s),
                        numero_autorizacion=%s,
                        fecha_autorizacion=NOW(),
                        respuesta_json=%s
                    WHERE compra_id=%s
                """, (estado, mensaje, numero, jstr, cid))
                cn.commit()
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"Error autorizando compra {cid}: {e}")
    finally:
        cn.close()
    logger.info("Autorización de liquidaciones completada")


if __name__ == "__main__":
    main()