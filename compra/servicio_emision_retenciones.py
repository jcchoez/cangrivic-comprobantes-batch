#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, random, decimal, logging, traceback
from lxml import etree
from comun_compras import (
    db_conn, fetch_empresa, fetch_proveedor, fetch_compra_items,
    generar_clave_acceso, validar_certificado, firmar_xml,
    enviar_comprobante_sri, siguiente_secuencial_retencion,
    AMBIENTE, TIPO_EMISION, PORC_RETENCION, COD_RETENCION, Compra,
)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("emision_retenciones")

LIMITE_ENVIO = int(os.getenv("LIMITE_ENVIO", "1000"))
DIR_XML = os.path.join(os.getcwd(), "retenciones")
os.makedirs(DIR_XML, exist_ok=True)


def fetch_compras_para_retencion(limit):
    cn = db_conn()
    try:
        cur = cn.cursor()
        cur.execute("""
            SELECT c.compra_id, c.fecha_compra, c.total, c.empresa_id,
                   c.secuencial, c.codigo_numerico, c.establecimiento,
                   c.punto_emision, c.clave_acceso, c.proveedor_id,
                   c.secuencial_retencion, c.codigo_numerico_retencion
            FROM compras c
            WHERE c.estado_sri='AUTORIZADO'
              AND (c.estado_sri_retencion IS NULL
                   OR c.estado_sri_retencion IN ('PENDIENTE','ERROR'))
              AND COALESCE(c.intentos_envio_retencion,0) < 5
            ORDER BY c.fecha_autorizacion ASC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        compras = []
        for row in rows:
            (cid, fecha, total, emp_id, sec, cod_num, estab, pto,
             clave_liq, prov_id, sec_ret, cod_ret) = row
            fecha_str = fecha.strftime("%Y-%m-%d %H:%M:%S") if hasattr(fecha, "strftime") else str(fecha)
            emp  = fetch_empresa(cur, emp_id)
            prov = fetch_proveedor(cur, prov_id)
            items = fetch_compra_items(cur, cid)

            # Asignar/crear secuencial y código numérico de retención
            if sec_ret is None:
                sec_ret = siguiente_secuencial_retencion(cur, emp_id)
                cur.execute("UPDATE compras SET secuencial_retencion=%s WHERE compra_id=%s",
                            (f"{sec_ret:09d}", cid))
            if not cod_ret:
                cod_ret = f"{random.randint(1, 99999999):08d}"
                cur.execute("UPDATE compras SET codigo_numerico_retencion=%s WHERE compra_id=%s",
                            (cod_ret, cid))
            cn.commit()

            compras.append(Compra(
                compra_id=int(cid), fecha=fecha_str,
                total=decimal.Decimal(total),
                secuencial=int(sec),
                empresa=emp, proveedor=prov, items=items,
                codigo_numerico=str(cod_num),
                establecimiento=estab or emp.establecimiento,
                punto_emision=pto or emp.punto_emision,
                tipo_comprobante="07",
                clave_acceso_liquidacion=clave_liq,
                secuencial_retencion=int(sec_ret),
                codigo_numerico_retencion=str(cod_ret),
            ))
        return compras
    finally:
        cn.close()


def retencion_xml(c: Compra, version="1.0.0"):
    fecha_parte = c.fecha.split()[0]
    fecha_ddmmaaaa = fecha_parte[8:10] + fecha_parte[5:7] + fecha_parte[0:4]
    fecha_emision  = f"{fecha_parte[8:10]}/{fecha_parte[5:7]}/{fecha_parte[0:4]}"
    periodo_fiscal = f"{fecha_parte[5:7]}/{fecha_parte[0:4]}"

    clave = generar_clave_acceso(
        fecha_ddmmaaaa, "07", c.empresa.ruc, AMBIENTE,
        c.establecimiento, c.punto_emision, c.secuencial_retencion,
        c.codigo_numerico_retencion, TIPO_EMISION,
    )

    base = c.total.quantize(decimal.Decimal("0.01"))
    valor_ret = (base * PORC_RETENCION / decimal.Decimal("100")).quantize(decimal.Decimal("0.01"))

    # numDocSustento = estab(3) + ptoEmi(3) + secuencial(9) de la liquidación
    num_doc_sustento = f"{c.establecimiento}{c.punto_emision}{c.secuencial:09d}"

    root = etree.Element("comprobanteRetencion", id="comprobante", version=version)

    # --- infoTributaria ---
    it = etree.SubElement(root, "infoTributaria")
    etree.SubElement(it, "ambiente").text = str(AMBIENTE)
    etree.SubElement(it, "tipoEmision").text = str(TIPO_EMISION)
    etree.SubElement(it, "razonSocial").text = c.empresa.nombre[:300]
    etree.SubElement(it, "ruc").text = c.empresa.ruc
    etree.SubElement(it, "claveAcceso").text = clave
    etree.SubElement(it, "codDoc").text = "07"
    etree.SubElement(it, "estab").text = c.establecimiento
    etree.SubElement(it, "ptoEmi").text = c.punto_emision
    etree.SubElement(it, "secuencial").text = f"{c.secuencial_retencion:09d}"
    if c.empresa.direccion:
        etree.SubElement(it, "dirMatriz").text = c.empresa.direccion[:300]
    etree.SubElement(it, "contribuyenteRimpe").text = \
        "CONTRIBUYENTE NEGOCIO POPULAR - RÉGIMEN RIMPE"

    # --- infoCompRetencion ---
    info = etree.SubElement(root, "infoCompRetencion")
    etree.SubElement(info, "fechaEmision").text = fecha_emision
    if c.empresa.direccion:
        etree.SubElement(info, "dirEstablecimiento").text = c.empresa.direccion[:300]
    etree.SubElement(info, "obligadoContabilidad").text = c.empresa.obligado_contabilidad
    etree.SubElement(info, "tipoIdentificacionSujetoRetenido").text = c.proveedor.tipo_identificacion
    etree.SubElement(info, "razonSocialSujetoRetenido").text = c.proveedor.nombre[:300]
    etree.SubElement(info, "identificacionSujetoRetenido").text = c.proveedor.identificacion
    etree.SubElement(info, "periodoFiscal").text = periodo_fiscal

    # --- impuestos (1% con código 312A) ---
    imps = etree.SubElement(root, "impuestos")
    imp = etree.SubElement(imps, "impuesto")
    etree.SubElement(imp, "codigo").text = "1"                # 1 = Renta
    etree.SubElement(imp, "codigoRetencion").text = COD_RETENCION  # 312A
    etree.SubElement(imp, "baseImponible").text = f"{base:.2f}"
    etree.SubElement(imp, "porcentajeRetener").text = f"{PORC_RETENCION:.1f}"
    etree.SubElement(imp, "valorRetenido").text = f"{valor_ret:.2f}"
    etree.SubElement(imp, "codDocSustento").text = "03"       # 03 = Liquidación
    etree.SubElement(imp, "numDocSustento").text = num_doc_sustento
    etree.SubElement(imp, "fechaEmisionDocSustento").text = fecha_emision

    # --- infoAdicional ---
    ia = etree.SubElement(root, "infoAdicional")
    if c.proveedor.direccion:
        etree.SubElement(ia, "campoAdicional", nombre="Direccion").text = c.proveedor.direccion[:300]
    if c.proveedor.telefono:
        etree.SubElement(ia, "campoAdicional", nombre="Telefono").text = str(c.proveedor.telefono)[:300]
    if c.proveedor.correo:
        etree.SubElement(ia, "campoAdicional", nombre="Email").text = str(c.proveedor.correo)[:300]

    xml = etree.tostring(root, encoding="UTF-8", xml_declaration=True).decode("utf-8")
    return clave, xml


def main():
    logger.info("Iniciando emisión de comprobantes de retención al SRI...")
    compras = fetch_compras_para_retencion(LIMITE_ENVIO)
    if not compras:
        logger.info("No hay retenciones pendientes")
        return
    logger.info(f"Encontradas {len(compras)} retenciones para emitir")

    cn = db_conn()
    try:
        cur = cn.cursor()
        for c in compras:
            try:
                if not validar_certificado(c.empresa.cert_path, c.empresa.cert_password):
                    cur.execute("""
                        UPDATE compras SET estado_sri_retencion='ERROR',
                            mensaje_sri_retencion=%s,
                            intentos_envio_retencion=COALESCE(intentos_envio_retencion,0)+1
                        WHERE compra_id=%s
                    """, ("Certificado inválido", c.compra_id))
                    cn.commit(); continue

                clave, xml_sin = retencion_xml(c)
                xml_firmado = firmar_xml(xml_sin, c.empresa.cert_path, c.empresa.cert_password)

                with open(os.path.join(DIR_XML, f"{clave}.xml"), "w", encoding="utf-8") as f:
                    f.write(xml_firmado)

                estado, mensaje = enviar_comprobante_sri(xml_firmado)
                logger.info(f"Retención compra {c.compra_id}: {estado} - {mensaje}")

                cur.execute("""
                    UPDATE compras SET estado_sri_retencion=%s,
                        mensaje_sri_retencion=%s,
                        clave_acceso_retencion=%s,
                        ultimo_intento_envio_retencion=NOW(),
                        intentos_envio_retencion=COALESCE(intentos_envio_retencion,0)+1
                    WHERE compra_id=%s
                """, (estado, str(mensaje)[:500], clave, c.compra_id))
                cn.commit()
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error retención compra {c.compra_id}: {e}")
                logger.error(traceback.format_exc())
                cur.execute("""
                    UPDATE compras SET estado_sri_retencion='ERROR',
                        mensaje_sri_retencion=%s,
                        intentos_envio_retencion=COALESCE(intentos_envio_retencion,0)+1
                    WHERE compra_id=%s
                """, (str(e)[:500], c.compra_id))
                cn.commit()
    finally:
        cn.close()
    logger.info("Emisión de retenciones completada")


if __name__ == "__main__":
    main()