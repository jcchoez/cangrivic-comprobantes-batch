#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, decimal, logging, traceback
from lxml import etree
from comun_compras import (
    db_conn, fetch_empresa, fetch_proveedor, fetch_compra_items,
    generar_clave_acceso, validar_certificado, firmar_xml,
    enviar_comprobante_sri, AMBIENTE, TIPO_EMISION, Compra,
)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("emision_liquidaciones")

LIMITE_ENVIO = int(os.getenv("LIMITE_ENVIO", "1000"))
DIR_XML = os.path.join(os.getcwd(), "liquidaciones")
os.makedirs(DIR_XML, exist_ok=True)


def fetch_compras_pendientes(limit):
    cn = db_conn()
    try:
        cur = cn.cursor()
        cur.execute("""
            SELECT c.compra_id, c.fecha_compra, c.total, c.empresa_id,
                   c.secuencial, c.codigo_numerico, c.establecimiento,
                   c.punto_emision, c.tipo_comprobante, c.proveedor_id
            FROM compras c
            WHERE COALESCE(c.estado_sri,'') NOT IN ('AUTORIZADO','ENVIADO','RECIBIDA')
              AND COALESCE(c.intentos_envio,0) < 5
            ORDER BY c.fecha_compra ASC
            LIMIT %s
        """, (limit,))
        compras = []
        for row in cur.fetchall():
            (cid, fecha, total, emp_id, sec, cod_num, estab, pto,
             tipo, prov_id) = row
            fecha_str = fecha.strftime("%Y-%m-%d %H:%M:%S") if hasattr(fecha, "strftime") else str(fecha)
            emp  = fetch_empresa(cur, emp_id)
            prov = fetch_proveedor(cur, prov_id)
            items = fetch_compra_items(cur, cid)
            compras.append(Compra(
                compra_id=int(cid), fecha=fecha_str,
                total=decimal.Decimal(total), secuencial=int(sec),
                empresa=emp, proveedor=prov, items=items,
                codigo_numerico=str(cod_num),
                establecimiento=estab or emp.establecimiento,
                punto_emision=pto or emp.punto_emision,
                tipo_comprobante=tipo or "03",
            ))
        return compras
    finally:
        cn.close()


def liquidacion_xml(c: Compra, version="1.1.0"):
    fecha_parte   = c.fecha.split()[0]
    fecha_ddmmaaaa = fecha_parte[8:10] + fecha_parte[5:7] + fecha_parte[0:4]
    fecha_emision  = f"{fecha_parte[8:10]}/{fecha_parte[5:7]}/{fecha_parte[0:4]}"

    clave = generar_clave_acceso(
        fecha_ddmmaaaa, c.tipo_comprobante, c.empresa.ruc, AMBIENTE,
        c.establecimiento, c.punto_emision, c.secuencial,
        c.codigo_numerico, TIPO_EMISION,
    )

    root = etree.Element("liquidacionCompra", id="comprobante", version=version)

    # --- infoTributaria ---
    it = etree.SubElement(root, "infoTributaria")
    etree.SubElement(it, "ambiente").text = str(AMBIENTE)
    etree.SubElement(it, "tipoEmision").text = str(TIPO_EMISION)
    etree.SubElement(it, "razonSocial").text = c.empresa.nombre[:300]
    etree.SubElement(it, "ruc").text = c.empresa.ruc
    etree.SubElement(it, "claveAcceso").text = clave
    etree.SubElement(it, "codDoc").text = c.tipo_comprobante
    etree.SubElement(it, "estab").text = c.establecimiento
    etree.SubElement(it, "ptoEmi").text = c.punto_emision
    etree.SubElement(it, "secuencial").text = f"{c.secuencial:09d}"
    if c.empresa.direccion:
        etree.SubElement(it, "dirMatriz").text = c.empresa.direccion[:300]
    etree.SubElement(it, "contribuyenteRimpe").text = \
        "CONTRIBUYENTE NEGOCIO POPULAR - RÉGIMEN RIMPE"

    # --- infoLiquidacionCompra ---
    info = etree.SubElement(root, "infoLiquidacionCompra")
    etree.SubElement(info, "fechaEmision").text = fecha_emision
    if c.empresa.direccion:
        etree.SubElement(info, "dirEstablecimiento").text = c.empresa.direccion[:300]
    etree.SubElement(info, "obligadoContabilidad").text = c.empresa.obligado_contabilidad
    etree.SubElement(info, "tipoIdentificacionProveedor").text = c.proveedor.tipo_identificacion
    etree.SubElement(info, "razonSocialProveedor").text = c.proveedor.nombre[:300]
    etree.SubElement(info, "identificacionProveedor").text = c.proveedor.identificacion
    if c.proveedor.direccion:
        etree.SubElement(info, "direccionProveedor").text = c.proveedor.direccion[:300]

    total_sin = sum((it.cantidad * it.precio_unitario for it in c.items),
                    decimal.Decimal("0.00"))
    total_desc = sum((it.descuento for it in c.items), decimal.Decimal("0.00"))
    base = total_sin - total_desc
    etree.SubElement(info, "totalSinImpuestos").text = f"{total_sin:.2f}"
    etree.SubElement(info, "totalDescuento").text = f"{total_desc:.2f}"
    tci = etree.SubElement(info, "totalConImpuestos")
    ti = etree.SubElement(tci, "totalImpuesto")
    etree.SubElement(ti, "codigo").text = "2"
    etree.SubElement(ti, "codigoPorcentaje").text = "0"
    etree.SubElement(ti, "baseImponible").text = f"{base:.2f}"
    etree.SubElement(ti, "valor").text = "0.00"
    etree.SubElement(info, "importeTotal").text = f"{base:.2f}"
    etree.SubElement(info, "moneda").text = "DOLAR"
    pagos = etree.SubElement(info, "pagos")
    pago = etree.SubElement(pagos, "pago")
    etree.SubElement(pago, "formaPago").text = "01"
    etree.SubElement(pago, "total").text = f"{base:.2f}"

    # --- detalles ---
    detalles = etree.SubElement(root, "detalles")
    for it in c.items:
        d = etree.SubElement(detalles, "detalle")
        if it.codigo:
            etree.SubElement(d, "codigoPrincipal").text = it.codigo[:25]
        etree.SubElement(d, "descripcion").text = it.descripcion[:300]
        etree.SubElement(d, "cantidad").text = f"{it.cantidad:.2f}"
        etree.SubElement(d, "precioUnitario").text = f"{it.precio_unitario:.2f}"
        etree.SubElement(d, "descuento").text = f"{it.descuento:.2f}"
        pti = (it.cantidad * it.precio_unitario) - it.descuento
        etree.SubElement(d, "precioTotalSinImpuesto").text = f"{pti:.2f}"
        imps = etree.SubElement(d, "impuestos")
        imp = etree.SubElement(imps, "impuesto")
        etree.SubElement(imp, "codigo").text = "2"
        etree.SubElement(imp, "codigoPorcentaje").text = "0"
        etree.SubElement(imp, "tarifa").text = "0.0"
        etree.SubElement(imp, "baseImponible").text = f"{pti:.2f}"
        etree.SubElement(imp, "valor").text = "0.00"

    # --- infoAdicional ---
    ia = etree.SubElement(root, "infoAdicional")
    if c.proveedor.telefono:
        etree.SubElement(ia, "campoAdicional", nombre="Telefono").text = str(c.proveedor.telefono)[:300]
    if c.proveedor.correo:
        etree.SubElement(ia, "campoAdicional", nombre="Email").text = str(c.proveedor.correo)[:300]

    xml = etree.tostring(root, encoding="UTF-8", xml_declaration=True).decode("utf-8")
    return clave, xml


def main():
    logger.info("Iniciando emisión de liquidaciones de compra al SRI...")
    compras = fetch_compras_pendientes(LIMITE_ENVIO)
    if not compras:
        logger.info("No hay compras pendientes de emisión")
        return
    logger.info(f"Encontradas {len(compras)} compras")

    cn = db_conn()
    try:
        cur = cn.cursor()
        for c in compras:
            try:
                if not validar_certificado(c.empresa.cert_path, c.empresa.cert_password):
                    cur.execute("""
                        UPDATE compras SET estado_sri='ERROR', mensaje_sri=%s,
                            ultimo_intento_envio=NOW(),
                            intentos_envio=COALESCE(intentos_envio,0)+1
                        WHERE compra_id=%s
                    """, ("Certificado inválido", c.compra_id))
                    cn.commit(); continue

                clave, xml_sin = liquidacion_xml(c)
                xml_firmado = firmar_xml(xml_sin, c.empresa.cert_path, c.empresa.cert_password)

                with open(os.path.join(DIR_XML, f"{clave}.xml"), "w", encoding="utf-8") as f:
                    f.write(xml_firmado)

                estado, mensaje = enviar_comprobante_sri(xml_firmado)
                logger.info(f"Compra {c.compra_id}: {estado} - {mensaje}")

                cur.execute("""
                    UPDATE compras SET estado_sri=%s, mensaje_sri=%s,
                        clave_acceso=%s, ultimo_intento_envio=NOW(),
                        intentos_envio=COALESCE(intentos_envio,0)+1
                    WHERE compra_id=%s
                """, (estado, str(mensaje)[:500], clave, c.compra_id))
                cn.commit()
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error en compra {c.compra_id}: {e}")
                logger.error(traceback.format_exc())
                cur.execute("""
                    UPDATE compras SET estado_sri='ERROR', mensaje_sri=%s,
                        ultimo_intento_envio=NOW(),
                        intentos_envio=COALESCE(intentos_envio,0)+1
                    WHERE compra_id=%s
                """, (str(e)[:500], c.compra_id))
                cn.commit()
    finally:
        cn.close()
    logger.info("Emisión de liquidaciones completada")


if __name__ == "__main__":
    main()